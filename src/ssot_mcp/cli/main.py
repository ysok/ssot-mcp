"""Command-line interface for ssot-mcp (same data dir + DB as the MCP server)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ssot_mcp.core.store import Store
from ssot_mcp.embeddings.semantic import semantic_search_formatted
from ssot_mcp.services.repos import (
    ActionResult,
    OrgImportSummary,
    add_github_organization,
    add_repository,
    list_formatted,
    read_mirror_file,
    remove_repository,
    search_formatted,
    sync_repository,
)


def _default_data_root() -> Path:
    return Path(os.environ.get("SSOT_DATA_DIR", "/data")).resolve()


def _store(data_dir: Path | None) -> Store:
    root = (data_dir or _default_data_root()).resolve()
    s = Store(root)
    s.init_db()
    return s


def _print_result(r: ActionResult, *, stream: bool = False) -> int:
    if not r.ok:
        print(r.message, file=sys.stderr)
        return 1
    if stream:
        sys.stdout.write(r.message)
        if not r.message.endswith("\n"):
            sys.stdout.write("\n")
    else:
        print(r.message)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(prog="ssot-mcp", description="Manage ssot-mcp mirrored repositories.")
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Data directory (mirrors + ssot.db). Default: SSOT_DATA_DIR or /data.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List registered repositories")
    p_list.set_defaults(func=_cmd_list)

    p_add = sub.add_parser("add", help="Clone a git URL, register it, and index files")
    p_add.add_argument("url", help="Git remote URL (https or ssh)")
    p_add.set_defaults(func=_cmd_add)

    p_org = sub.add_parser(
        "add-org",
        help="Register all public GitHub repos for an organization (API list; clone new, sync existing)",
    )
    p_org.add_argument(
        "org",
        help="Org profile URL (https://github.com/ORG) or org login name",
    )
    p_org.add_argument(
        "--no-forks",
        action="store_true",
        help="Skip fork repositories",
    )
    p_org.add_argument(
        "--no-archived",
        action="store_true",
        help="Skip archived repositories",
    )
    p_org.set_defaults(func=_cmd_add_org)

    p_rm = sub.add_parser("remove", help="Remove a repo from the registry and delete its mirror")
    p_rm.add_argument("repo_id", help="Repository id from list")
    p_rm.set_defaults(func=_cmd_remove)

    p_sync = sub.add_parser("sync", help="Fetch latest from remote and re-index")
    p_sync.add_argument("repo_id", help="Repository id from list")
    p_sync.set_defaults(func=_cmd_sync)

    p_search = sub.add_parser("search", help="Full-text search (SQLite FTS5)")
    p_search.add_argument("query", help="Search query (FTS syntax: keywords, AND, OR, …)")
    p_search.add_argument("--repo", dest="repo_id", default=None, help="Limit to one repo id")
    p_search.add_argument("--limit", type=int, default=25, help="Max hits (default 25)")
    p_search.add_argument("--plain", action="store_true", help="One hit per line: repo_id path snippet")
    p_search.set_defaults(func=_cmd_search)

    p_sem = sub.add_parser(
        "semantic-search",
        help="Cross-repo natural-language search (needs pip install 'ssot-mcp[semantic]', local fastembed)",
    )
    p_sem.add_argument("query", help="Question or phrase")
    p_sem.add_argument("--repo", dest="repo_id", default=None, help="Limit to one repo id")
    p_sem.add_argument("--top-k", type=int, default=15, help="Max results (default 15, max 50)")
    p_sem.set_defaults(func=_cmd_semantic_search)

    p_cat = sub.add_parser("cat", help="Print a file from a mirror (by repo id and relative path)")
    p_cat.add_argument("repo_id", help="Repository id from list")
    p_cat.add_argument("path", help="Path relative to repo root")
    p_cat.add_argument("--max-bytes", type=int, default=65536, help="Read cap (default 65536)")
    p_cat.set_defaults(func=_cmd_cat)

    args = p.parse_args(argv)
    return args.func(args)


def _cmd_list(args: argparse.Namespace) -> int:
    store = _store(args.data_dir)
    print(list_formatted(store))
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    store = _store(args.data_dir)
    return _print_result(add_repository(store, args.url))


def _cmd_add_org(args: argparse.Namespace) -> int:
    store = _store(args.data_dir)
    summary = add_github_organization(
        store,
        args.org,
        exclude_forks=args.no_forks,
        exclude_archived=args.no_archived,
    )
    print(summary.message)
    return _org_import_exit(summary)


def _org_import_exit(s: OrgImportSummary) -> int:
    if not s.ok:
        return 1
    if s.failed and not s.added and not s.updated:
        return 1
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    store = _store(args.data_dir)
    return _print_result(remove_repository(store, args.repo_id))


def _cmd_sync(args: argparse.Namespace) -> int:
    store = _store(args.data_dir)
    return _print_result(sync_repository(store, args.repo_id))


def _cmd_semantic_search(args: argparse.Namespace) -> int:
    store = _store(args.data_dir)
    text = semantic_search_formatted(
        store,
        args.query,
        repo_id=args.repo_id.strip() if args.repo_id else None,
        top_k=min(max(args.top_k, 1), 50),
    )
    print(text)
    low = text.lower()
    if any(
        m in low
        for m in (
            "no semantic matches",
            "semantic search requires",
            "needs fastembed",
            "semantic search error",
        )
    ):
        return 1
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    store = _store(args.data_dir)
    if args.plain:
        hits = store.search(
            args.query.strip(),
            repo_id=args.repo_id.strip() if args.repo_id else None,
            limit=min(max(args.limit, 1), 100),
        )
        if not hits:
            print("No matches.", file=sys.stderr)
            return 1
        for h in hits:
            snip = " ".join(h["snippet"].split())
            print(f"{h['repo_id']}\t{h['path']}\t{snip}")
        return 0
    text = search_formatted(store, args.query, repo_id=args.repo_id, limit=args.limit)
    print(text)
    return 0 if not text.startswith("No matches") else 1


def _cmd_cat(args: argparse.Namespace) -> int:
    store = _store(args.data_dir)
    return _print_result(read_mirror_file(store, args.repo_id, args.path, max_bytes=args.max_bytes), stream=True)


if __name__ == "__main__":
    raise SystemExit(main())
