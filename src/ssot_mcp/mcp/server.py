"""MCP server: register git URLs, clone, FTS index, search and read files."""

from __future__ import annotations

import os
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ssot_mcp.core.store import Store
from ssot_mcp.embeddings.semantic import semantic_search_formatted
from ssot_mcp.services.repos import (
    add_github_organization as import_github_organization,
    add_repository as repo_add,
    list_formatted,
    read_mirror_file,
    remove_repository as repo_remove,
    search_formatted,
    sync_repository as repo_sync,
)

_store: Store | None = None


def _data_root() -> Path:
    return Path(os.environ.get("SSOT_DATA_DIR", "/data")).resolve()


def get_store() -> Store:
    global _store
    root = _data_root()
    if _store is None or _store.root != root:
        _store = Store(root)
        _store.init_db()
    return _store


mcp = FastMCP(
    "ssot-mcp",
    instructions=(
        "Single Source of Truth for codebases. Use add_repository for one git URL, or add_github_organization "
        "with https://github.com/ORG to clone all public repos in that org. "
        "Optional: set GITHUB_TOKEN or GITHUB_PERSONAL_ACCESS_TOKEN for higher GitHub API rate limits. "
        "Optional semantic search: pip install 'ssot-mcp[semantic]' (LanceDB + fastembed, on-device ONNX embeddings, no API keys). "
        "Keep using search_code for keywords and read_file for exact file reads. list_repositories shows ids."
    ),
    host=os.environ.get("FASTMCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("FASTMCP_PORT", "8765")),
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
def list_repositories() -> str:
    """List registered repositories (id, url, name, paths, timestamps)."""
    return list_formatted(get_store())


@mcp.tool()
def add_repository(url: str) -> str:
    """Clone a git repository (HTTPS or SSH URL), index text files (FTS), queue semantic embeddings in the background when enabled, and return the new repo id."""
    return repo_add(get_store(), url).message


@mcp.tool()
def add_github_organization(
    org_url: str,
    exclude_forks: bool = False,
    exclude_archived: bool = False,
) -> str:
    """List all public repositories for a GitHub org (e.g. https://github.com/kubernetes or org login). Clones and FTS-indexes new repos; syncs and re-indexes existing mirrors. Semantic embeddings are queued in the background after all mirrors succeed (not during clone). Uses GITHUB_TOKEN or GITHUB_PERSONAL_ACCESS_TOKEN if set."""
    s = import_github_organization(
        get_store(),
        org_url,
        exclude_forks=exclude_forks,
        exclude_archived=exclude_archived,
    )
    return s.message


@mcp.tool()
def remove_repository(repo_id: str) -> str:
    """Remove a repository from the registry, delete its search index, and remove the cloned files."""
    return repo_remove(get_store(), repo_id).message


@mcp.tool()
def sync_repository(repo_id: str) -> str:
    """Run git fetch for a registered mirror, refresh the FTS index, and queue semantic re-indexing in the background when enabled."""
    return repo_sync(get_store(), repo_id).message


@mcp.tool()
def search_code(query: str, repo_id: str | None = None, limit: int = 25) -> str:
    """Full-text search across indexed files (SQLite FTS5). Use keywords; combine with AND / OR. Optional repo_id scopes one repo."""
    rid = repo_id.strip() if repo_id else None
    return search_formatted(get_store(), query, repo_id=rid, limit=limit)


@mcp.tool()
def semantic_search(query: str, repo_id: str | None = None, top_k: int = 15) -> str:
    """Natural-language / cross-repo search using local embeddings (LanceDB + fastembed). Requires pip install 'ssot-mcp[semantic]'. Indexes on add/sync. Optional repo_id scopes one mirror. Use with search_code for hybrid retrieval."""
    rid = repo_id.strip() if repo_id else None
    return semantic_search_formatted(
        get_store(),
        query,
        repo_id=rid,
        top_k=min(max(top_k, 1), 50),
    )


@mcp.tool()
def read_file(repo_id: str, path: str, max_bytes: int = 65536) -> str:
    """Read a file from a cloned repository by repo id and path relative to repo root."""
    r = read_mirror_file(get_store(), repo_id, path, max_bytes=max_bytes)
    if not r.ok:
        return r.message
    text = r.message
    note = ""
    m = re.search(r"\n\n\[truncated to (\d+) bytes\]\n$", text)
    if m:
        text = text[: m.start()]
        note = f"\n\n_(truncated to {m.group(1)} bytes)_"
    return f"### `{path}`\n\n```\n{text}\n```{note}"


def main() -> None:
    transport = os.environ.get("SSOT_MCP_TRANSPORT", "streamable-http")
    if transport not in ("streamable-http", "stdio", "sse"):
        transport = "streamable-http"
    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
