"""Shared repository operations for MCP tools and CLI."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ssot_mcp.core.store import Store
from ssot_mcp.embeddings.semantic_queue import enqueue_semantic_index
from ssot_mcp.git import git_ops
from ssot_mcp.github.github_org import GitHubApiError, list_public_clone_urls, parse_github_org


def _remove_partial_mirror(store: Store, mirror_path: Path) -> None:
    """Best-effort cleanup after a failed or timed-out clone (avoids leaving broken trees on disk)."""
    if mirror_path.exists():
        try:
            store.remove_mirror_dir(mirror_path)
        except Exception:
            pass


def _semantic_features_wanted() -> bool:
    from ssot_mcp.embeddings.semantic import (
        semantic_api_configured,
        semantic_dependencies_installed,
        semantic_indexing_enabled,
    )

    return (
        semantic_indexing_enabled()
        and semantic_dependencies_installed()
        and semantic_api_configured()
    )


def _schedule_semantic_after_fts(store: Store, repo_id: str, *, defer_enqueue: bool) -> None:
    if not _semantic_features_wanted():
        store.set_semantic_status(repo_id, "skipped", None)
        store.set_semantic_chunk_count(repo_id, 0)
        return
    store.set_semantic_status(repo_id, "pending", None)
    store.set_semantic_chunk_count(repo_id, 0)
    if not defer_enqueue:
        enqueue_semantic_index(store.root, repo_id)


def enqueue_semantic_for_repo_urls(store: Store, urls: list[str]) -> None:
    """Queue semantic indexing for every registered repo whose `url` is in ``urls`` (order preserved)."""
    if not urls:
        return
    root = store.root.resolve()
    with store.connect() as conn:
        for u in urls:
            row = conn.execute("SELECT id FROM repos WHERE url = ?", (u,)).fetchone()
            if row:
                enqueue_semantic_index(root, str(row[0]))


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class OrgImportSummary:
    """Result of importing all public repos for a GitHub org."""

    ok: bool
    message: str
    added: int
    updated: int
    failed: int


def add_repository(store: Store, url: str, *, defer_semantic: bool = False) -> ActionResult:
    url = url.strip()
    if not url:
        return ActionResult(False, "Error: empty URL.")
    store.init_db()
    with store.connect() as conn:
        existing = conn.execute("SELECT id FROM repos WHERE url = ?", (url,)).fetchone()
    if existing:
        return ActionResult(True, f"This URL is already registered. repo_id=`{existing[0]}`")
    mirror_name = git_ops.slug_from_url(url)
    mirror_path = store.mirrors / mirror_name
    suffix = 0
    while mirror_path.exists():
        suffix += 1
        mirror_path = store.mirrors / f"{mirror_name}_{suffix}"
    display = git_ops.display_name_for_url(url)
    try:
        git_ops.clone(url, mirror_path)
    except subprocess.TimeoutExpired as e:
        _remove_partial_mirror(store, mirror_path)
        tmo = int(e.timeout) if e.timeout is not None else git_ops.clone_timeout_seconds()
        out = getattr(e, "output", None)
        if isinstance(out, (bytes, bytearray)):
            tail = out.decode(errors="replace")[:1500]
        elif isinstance(out, str):
            tail = out[:1500]
        elif out:
            tail = str(out)[:1500]
        else:
            tail = str(e)[:500]
        return ActionResult(
            False,
            "Error: git clone timed out after "
            f"{tmo}s (large repository or slow network). "
            "Set SSOT_GIT_CLONE_TIMEOUT to a higher value in seconds (max 86400) and retry.\n"
            f"{tail}",
        )
    except subprocess.CalledProcessError as e:
        _remove_partial_mirror(store, mirror_path)
        err = (e.stderr or e.stdout or str(e))[:2000]
        return ActionResult(False, f"Error: git clone failed.\n{err}")
    except FileExistsError as e:
        return ActionResult(False, f"Error: {e}")
    except Exception as e:
        _remove_partial_mirror(store, mirror_path)
        return ActionResult(False, f"Error: {e}")
    repo_id = store.add_repo(url, display, mirror_path)
    try:
        n = store.index_mirror(repo_id, mirror_path)
    except Exception as e:
        store.delete_repo_record(repo_id)
        store.remove_mirror_dir(mirror_path)
        return ActionResult(False, f"Error: indexing failed ({e}). Repository not registered.")
    msg = (
        f"Added repository **{display}** (`{repo_id}`).\n"
        f"Mirror: `{mirror_path}`\nIndexed **{n}** text files (see store limits / extensions)."
    )
    _schedule_semantic_after_fts(store, repo_id, defer_enqueue=defer_semantic)
    if _semantic_features_wanted() and not defer_semantic:
        msg += "\n\nSemantic embedding indexing has been **queued** (runs in the background)."
    return ActionResult(True, msg)


def add_or_sync_repository(
    store: Store, url: str, *, defer_semantic: bool = False
) -> tuple[ActionResult, Literal["added", "updated", "failed"]]:
    """
    Clone + FTS index (and queue semantic) if the URL is new; otherwise sync + re-index (and queue semantic).
    """
    url = url.strip()
    if not url:
        return ActionResult(False, "Error: empty URL."), "failed"
    store.init_db()
    with store.connect() as conn:
        row = conn.execute("SELECT id FROM repos WHERE url = ?", (url,)).fetchone()
    if row:
        rid = str(row[0])
        r = sync_repository(store, rid, defer_semantic=defer_semantic)
        return r, "updated" if r.ok else "failed"
    r = add_repository(store, url, defer_semantic=defer_semantic)
    return r, "added" if r.ok else "failed"


def add_github_organization(
    store: Store,
    org_url_or_name: str,
    *,
    exclude_forks: bool = False,
    exclude_archived: bool = False,
    token: str | None = None,
) -> OrgImportSummary:
    """
    List public repos for a GitHub org and register each via add_or_sync_repository.
    Clones and FTS-indexes each repo first, then queues semantic indexing for all successes.
    """
    store.init_db()
    try:
        org = parse_github_org(org_url_or_name)
        urls = list_public_clone_urls(
            org,
            token=token,
            exclude_forks=exclude_forks,
            exclude_archived=exclude_archived,
            data_root=store.root,
        )
    except GitHubApiError as e:
        return OrgImportSummary(False, str(e), 0, 0, 0)

    if not urls:
        return OrgImportSummary(
            True,
            f"No public repositories found for GitHub org `{org}` (after filters).",
            0,
            0,
            0,
        )

    added = updated = failed = 0
    detail_lines: list[str] = []
    successful_urls: list[str] = []
    for clone_url in urls:
        u = clone_url.strip()
        r, kind = add_or_sync_repository(store, u, defer_semantic=True)
        name = u.rstrip("/").removesuffix(".git").split("/")[-1]
        if not r.ok:
            failed += 1
            detail_lines.append(f"- **FAIL** `{name}`: {r.message.splitlines()[0][:500]}")
        elif kind == "updated":
            updated += 1
            successful_urls.append(u)
            detail_lines.append(f"- **updated** `{name}` (sync + re-index)")
        else:
            added += 1
            successful_urls.append(u)
            detail_lines.append(f"- **added** `{name}`")

    enqueue_semantic_for_repo_urls(store, successful_urls)

    msg_parts = [
        f"GitHub org `{org}`: discovered **{len(urls)}** public repo(s).",
        f"Added: **{added}**, updated (already mirrored): **{updated}**, failed: **{failed}**.",
        "",
        "Semantic embedding indexing has been **queued** for each successful mirror (background worker).",
        "",
        *detail_lines,
    ]
    if failed and not added and not updated:
        ok = False
    else:
        ok = True
    return OrgImportSummary(ok, "\n".join(msg_parts), added, updated, failed)


def remove_repository(store: Store, repo_id: str) -> ActionResult:
    store.init_db()
    rid = repo_id.strip()
    row = store.delete_repo_record(rid)
    if not row:
        return ActionResult(False, f"No repository with id `{repo_id}`.")
    _url, mpath = row
    try:
        from ssot_mcp.embeddings.semantic import delete_repository_vectors

        delete_repository_vectors(store.root, rid)
    except Exception:
        pass
    store.remove_mirror_dir(mpath)
    return ActionResult(True, f"Removed repository `{repo_id}` and mirror at `{mpath}`.")


def sync_repository(store: Store, repo_id: str, *, defer_semantic: bool = False) -> ActionResult:
    store.init_db()
    rid = repo_id.strip()
    with store.connect() as conn:
        info = store.get_repo(conn, rid)
    if not info:
        return ActionResult(False, f"No repository with id `{repo_id}`.")
    _i, _u, mpath_s = info
    mpath = Path(mpath_s)
    try:
        git_ops.sync(mpath)
    except subprocess.TimeoutExpired as e:
        tmo = int(e.timeout) if e.timeout is not None else git_ops.sync_timeout_seconds()
        out = getattr(e, "output", None)
        if isinstance(out, (bytes, bytearray)):
            tail = out.decode(errors="replace")[:1500]
        elif isinstance(out, str):
            tail = out[:1500]
        elif out:
            tail = str(out)[:1500]
        else:
            tail = str(e)[:500]
        return ActionResult(
            False,
            "Error: git sync timed out after "
            f"{tmo}s. Set SSOT_GIT_SYNC_TIMEOUT (seconds, max 86400) and retry.\n"
            f"{tail}",
        )
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or str(e))[:2000]
        return ActionResult(False, f"Error: git sync failed.\n{err}")
    except Exception as e:
        return ActionResult(False, f"Error: {e}")
    try:
        n = store.index_mirror(rid, mpath)
    except Exception as e:
        return ActionResult(True, f"Warning: synced but re-index failed: {e}")
    store.touch_repo(rid)
    msg = f"Synced and re-indexed **{n}** files for `{rid}`."
    _schedule_semantic_after_fts(store, rid, defer_enqueue=defer_semantic)
    if _semantic_features_wanted() and not defer_semantic:
        msg += "\n\nSemantic embedding indexing has been **queued** (runs in the background)."
    return ActionResult(True, msg)


def retry_semantic_indexing(store: Store, repo_id: str) -> ActionResult:
    store.init_db()
    rid = repo_id.strip()
    with store.connect() as conn:
        row = conn.execute("SELECT id FROM repos WHERE id = ?", (rid,)).fetchone()
    if not row:
        return ActionResult(False, "No repository with that id.")
    if not _semantic_features_wanted():
        store.set_semantic_status(rid, "skipped", None)
        store.set_semantic_chunk_count(rid, 0)
        return ActionResult(
            False,
            "Semantic indexing is disabled (SSOT_SEMANTIC_INDEX) or optional dependencies are not installed.",
        )
    store.set_semantic_status(rid, "pending", None)
    store.set_semantic_chunk_count(rid, 0)
    enqueue_semantic_index(store.root, rid)
    return ActionResult(True, "Semantic indexing queued.")


def read_mirror_file(store: Store, repo_id: str, path: str, max_bytes: int = 65536) -> ActionResult:
    """Read a file under a mirror; message is file contents on success."""
    store.init_db()
    rid = repo_id.strip()
    rel = path.strip().lstrip("/")
    if ".." in Path(rel).parts:
        return ActionResult(False, "Error: path must not contain '..'.")
    with store.connect() as conn:
        info = store.get_repo(conn, rid)
    if not info:
        return ActionResult(False, f"No repository with id `{repo_id}`.")
    _i, _u, mpath_s = info
    mirror = Path(mpath_s).resolve()
    target = (mirror / rel).resolve()
    try:
        target.relative_to(mirror)
    except ValueError:
        return ActionResult(False, "Error: path escapes repository root.")
    if not target.is_file():
        return ActionResult(False, f"Not a file: `{path}`")
    cap = min(max(max_bytes, 1024), 2_000_000)
    try:
        data = target.read_bytes()[:cap]
    except OSError as e:
        return ActionResult(False, f"Error reading file: {e}")
    text = data.decode("utf-8", errors="replace")
    if target.stat().st_size > cap:
        text += f"\n\n[truncated to {cap} bytes]\n"
    return ActionResult(True, text)


def search_formatted(store: Store, query: str, repo_id: str | None = None, limit: int = 25) -> str:
    store.init_db()
    rid = repo_id.strip() if repo_id else None
    hits = store.search(query.strip(), repo_id=rid, limit=min(max(limit, 1), 100))
    if not hits:
        return "No matches (try different keywords, or add/sync the repository)."
    lines = []
    for h in hits:
        lines.append(
            f"- **{h['display_name']}** `{h['path']}`\n  repo_id=`{h['repo_id']}`\n  {h['snippet']}"
        )
    return "\n\n".join(lines)


def list_formatted(store: Store) -> str:
    store.init_db()
    rows = store.list_repos()
    if not rows:
        return (
            "No repositories yet. Run `ssot-mcp add <git-url>` or `ssot-mcp add-org <github-org>`, "
            "or use MCP tools `add_repository` / `add_github_organization`."
        )
    lines = []
    for r in rows:
        lines.append(
            f"- **{r['display_name']}**  `id={r['id']}`\n  url: {r['url']}\n  mirror: `{r['mirror_path']}`\n"
            f"  created: {r['created_at']}  updated: {r['updated_at']}"
        )
    return "\n\n".join(lines)
