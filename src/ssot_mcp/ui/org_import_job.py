"""Background GitHub org import for the admin UI (progress file + worker thread)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ssot_mcp.core.store import Store
from ssot_mcp.github.github_org import GitHubApiError, list_public_clone_urls, parse_github_org
from ssot_mcp.services.repos import add_or_sync_repository, enqueue_semantic_for_repo_urls

_JOB_LOCK = threading.Lock()
_worker_running = False


def _state_path(data_root: Path) -> Path:
    d = data_root / ".ssot_ui"
    d.mkdir(parents=True, exist_ok=True)
    return d / "org_import.json"


def read_org_import_state(data_root: Path) -> dict[str, Any] | None:
    p = _state_path(data_root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_state(data_root: Path, data: dict[str, Any]) -> None:
    p = _state_path(data_root)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def is_org_import_busy() -> bool:
    with _JOB_LOCK:
        return _worker_running


def start_org_import_job(
    store: Store,
    org_url_or_name: str,
    *,
    exclude_forks: bool,
    exclude_archived: bool,
) -> str:
    """
    Start a background org import if idle.

    Returns ``\"started\"``, ``\"busy\"``, or ``\"error\"`` (invalid empty org).
    """
    global _worker_running
    org = org_url_or_name.strip()
    if not org:
        return "error"

    with _JOB_LOCK:
        if _worker_running:
            return "busy"
        _worker_running = True

    data_root = store.root
    now = datetime.now(UTC).isoformat()
    _write_state(
        data_root,
        {
            "status": "starting",
            "org_input": org,
            "exclude_forks": exclude_forks,
            "exclude_archived": exclude_archived,
            "total": 0,
            "completed": 0,
            "current": None,
            "added": 0,
            "updated": 0,
            "failed": 0,
            "fatal_error": None,
            "items": [],
            "started_at": now,
            "finished_at": None,
        },
    )

    def worker() -> None:
        global _worker_running
        try:
            _run_org_import_worker(
                data_root,
                org,
                exclude_forks=exclude_forks,
                exclude_archived=exclude_archived,
            )
        finally:
            with _JOB_LOCK:
                _worker_running = False

    threading.Thread(target=worker, daemon=True, name="ssot-org-import").start()
    return "started"


def _repo_short_name(clone_url: str) -> str:
    return clone_url.rstrip("/").removesuffix(".git").split("/")[-1]


def _run_org_import_worker(
    data_root: Path,
    org_input: str,
    *,
    exclude_forks: bool,
    exclude_archived: bool,
) -> None:
    store = Store(data_root)
    store.init_db()

    def load() -> dict[str, Any]:
        return dict(read_org_import_state(data_root) or {})

    try:
        org = parse_github_org(org_input)
        st = load()
        st["status"] = "listing"
        st["org"] = org
        st["current"] = "Listing repositories from GitHub…"
        _write_state(data_root, st)

        urls = list_public_clone_urls(
            org,
            exclude_forks=exclude_forks,
            exclude_archived=exclude_archived,
            data_root=data_root,
        )
    except GitHubApiError as e:
        st = load()
        st["status"] = "error"
        st["fatal_error"] = str(e)
        st["current"] = None
        st["finished_at"] = datetime.now(UTC).isoformat()
        _write_state(data_root, st)
        return

    if not urls:
        st = load()
        st["status"] = "complete"
        st["org"] = org
        st["total"] = 0
        st["completed"] = 0
        st["current"] = None
        st["items"] = []
        st["finished_at"] = datetime.now(UTC).isoformat()
        _write_state(data_root, st)
        return

    names = [_repo_short_name(u) for u in urls]
    items = [{"name": n, "status": "pending", "detail": ""} for n in names]
    st = load()
    st["status"] = "running"
    st["org"] = org
    st["total"] = len(urls)
    st["completed"] = 0
    st["current"] = None
    st["added"] = 0
    st["updated"] = 0
    st["failed"] = 0
    st["fatal_error"] = None
    st["items"] = items
    _write_state(data_root, st)

    added = updated = failed = 0
    successful_urls: list[str] = []

    for i, clone_url in enumerate(urls):
        name = names[i]
        u = clone_url.strip()
        st = load()
        st["current"] = name
        st["items"][i] = {"name": name, "status": "running", "detail": ""}
        _write_state(data_root, st)

        result, kind = add_or_sync_repository(store, u, defer_semantic=True)
        st = load()
        if not result.ok:
            failed += 1
            line = result.message.splitlines()[0][:500]
            st["items"][i] = {"name": name, "status": "fail", "detail": line}
        elif kind == "updated":
            updated += 1
            successful_urls.append(u)
            st["items"][i] = {"name": name, "status": "updated", "detail": "Sync + re-index"}
        else:
            added += 1
            successful_urls.append(u)
            st["items"][i] = {"name": name, "status": "added", "detail": "Cloned + indexed"}

        st["completed"] = i + 1
        st["added"] = added
        st["updated"] = updated
        st["failed"] = failed
        _write_state(data_root, st)

    st = load()
    st["current"] = "Queuing semantic indexing…"
    _write_state(data_root, st)
    enqueue_semantic_for_repo_urls(store, successful_urls)

    st = load()
    st["status"] = "complete"
    st["current"] = None
    st["finished_at"] = datetime.now(UTC).isoformat()
    _write_state(data_root, st)
