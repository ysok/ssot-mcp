"""Tests for repos (orchestration)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ssot_mcp.core.store import Store
from ssot_mcp.github.github_org import GitHubApiError
from ssot_mcp.services.repos import (
    ActionResult,
    OrgImportSummary,
    add_github_organization,
    add_or_sync_repository,
    add_repository,
    enqueue_semantic_for_repo_urls,
    list_formatted,
    read_mirror_file,
    remove_repository,
    retry_semantic_indexing,
    search_formatted,
    sync_repository,
)


def _fake_clone_factory():
    def _clone(url: str, dest: Path, timeout: int | None = None) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "main.py").write_text(f"# {url}\nanswer = 42\n")

    return _clone


@patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index")
@patch("ssot_mcp.services.repos.git_ops.clone", side_effect=_fake_clone_factory())
def test_add_repository_success(_mock_clone, _mock_enqueue, store: Store) -> None:
    r = add_repository(store, "https://github.com/o/r.git")
    assert r.ok
    assert "Indexed **1**" in r.message
    assert store.list_repos()


@patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index")
@patch("ssot_mcp.services.repos.git_ops.clone", side_effect=_fake_clone_factory())
def test_add_repository_duplicate_url(_mock_clone, _mock_enqueue, store: Store) -> None:
    add_repository(store, "https://github.com/o/r.git")
    r2 = add_repository(store, "https://github.com/o/r.git")
    assert r2.ok
    assert "already registered" in r2.message


def test_add_repository_empty_url(store: Store) -> None:
    r = add_repository(store, "  ")
    assert not r.ok


@patch("ssot_mcp.services.repos.git_ops.clone", side_effect=__import__("subprocess").CalledProcessError(1, "git"))
def test_add_repository_clone_fails(mock_clone, store: Store) -> None:
    r = add_repository(store, "https://github.com/o/x.git")
    assert not r.ok
    assert "git clone failed" in r.message


@patch("ssot_mcp.services.repos.git_ops.clone", side_effect=subprocess.TimeoutExpired(cmd=["git", "clone"], timeout=30))
def test_add_repository_clone_timeout(mock_clone, store: Store) -> None:
    r = add_repository(store, "https://github.com/o/slow.git")
    assert not r.ok
    assert "timed out" in r.message.lower()
    assert "SSOT_GIT_CLONE_TIMEOUT" in r.message


@patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index")
@patch("ssot_mcp.services.repos.git_ops.clone")
def test_add_repository_timeout_removes_partial_mirror(mock_clone, _mock_enqueue, store: Store) -> None:
    def partial_then_timeout(url: str, dest: Path, timeout: int | None = None) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial.txt").write_text("wip")
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=99)

    mock_clone.side_effect = partial_then_timeout
    r = add_repository(store, "https://github.com/o/huge.git")
    assert not r.ok
    assert not (store.mirrors / "o__huge").exists()


@patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index")
@patch("ssot_mcp.services.repos.git_ops.clone", side_effect=_fake_clone_factory())
def test_remove_repository(_mock_clone, _mock_enqueue, store: Store) -> None:
    add_repository(store, "https://github.com/o/r.git")
    rid = store.list_repos()[0]["id"]
    mirror = Path(store.list_repos()[0]["mirror_path"])
    assert mirror.is_dir()
    r = remove_repository(store, rid)
    assert r.ok
    assert not store.list_repos()
    assert not mirror.exists()


@patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index")
@patch("ssot_mcp.services.repos.git_ops.sync")
@patch("ssot_mcp.services.repos.git_ops.clone", side_effect=_fake_clone_factory())
def test_sync_repository(_mock_clone, mock_sync, _mock_enqueue, store: Store) -> None:
    add_repository(store, "https://github.com/o/r.git")
    rid = store.list_repos()[0]["id"]
    mirror = Path(store.list_repos()[0]["mirror_path"])
    (mirror / "main.py").write_text("x = 1\n")
    r = sync_repository(store, rid)
    assert r.ok
    mock_sync.assert_called_once()
    assert "re-indexed" in r.message


def test_remove_unknown_repo(store: Store) -> None:
    r = remove_repository(store, "not-a-uuid")
    assert not r.ok


@patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index")
@patch("ssot_mcp.services.repos.list_public_clone_urls")
@patch("ssot_mcp.services.repos.add_or_sync_repository")
def test_add_github_organization(mock_aos, mock_list, _mock_enq, store: Store) -> None:
    mock_list.return_value = ["https://github.com/o/a.git", "https://github.com/o/b.git"]

    def aos_side_effect(st, url, defer_semantic=False):
        if url.endswith("a.git"):
            return ActionResult(True, "ok a"), "added"
        return ActionResult(True, "ok b"), "added"

    mock_aos.side_effect = aos_side_effect
    s = add_github_organization(store, "https://github.com/o")
    assert s.ok
    assert s.added == 2
    assert s.updated == 0
    assert mock_aos.call_count == 2
    for call in mock_aos.call_args_list:
        assert call.kwargs.get("defer_semantic") is True


@patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index")
@patch("ssot_mcp.services.repos.list_public_clone_urls")
@patch("ssot_mcp.services.repos.add_or_sync_repository")
def test_add_github_organization_counts_updated(mock_aos, mock_list, _mock_enq, store: Store) -> None:
    mock_list.return_value = ["https://github.com/o/a.git"]

    def aos_side_effect(st, url, defer_semantic=False):
        return ActionResult(True, "Synced and re-indexed **1** files"), "updated"

    mock_aos.side_effect = aos_side_effect
    s = add_github_organization(store, "https://github.com/o")
    assert s.ok
    assert s.added == 0
    assert s.updated == 1
    assert mock_aos.call_args.kwargs.get("defer_semantic") is True


@patch("ssot_mcp.services.repos.parse_github_org", side_effect=GitHubApiError("bad"))
def test_add_github_organization_api_error(_mock_parse, store: Store) -> None:
    s = add_github_organization(store, "x")
    assert not s.ok
    assert s.added == 0
    assert s.updated == 0


@patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index")
@patch("ssot_mcp.services.repos.git_ops.sync")
@patch("ssot_mcp.services.repos.git_ops.clone", side_effect=_fake_clone_factory())
def test_add_or_sync_new_clones(_mock_clone, mock_sync, _mock_enqueue, store: Store) -> None:
    r, kind = add_or_sync_repository(store, "https://github.com/o/r.git")
    assert r.ok
    assert kind == "added"
    mock_sync.assert_not_called()


@patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index")
@patch("ssot_mcp.services.repos.git_ops.sync")
@patch("ssot_mcp.services.repos.git_ops.clone", side_effect=_fake_clone_factory())
def test_add_or_sync_existing_syncs(_mock_clone, mock_sync, _mock_enqueue, store: Store) -> None:
    add_or_sync_repository(store, "https://github.com/o/r.git")
    mock_sync.reset_mock()
    r, kind = add_or_sync_repository(store, "https://github.com/o/r.git")
    assert r.ok
    assert kind == "updated"
    mock_sync.assert_called_once()


def test_read_mirror_file_ok(store: Store, tmp_path: Path) -> None:
    mirror = tmp_path / "m"
    mirror.mkdir()
    (mirror / "README.md").write_text("# Title\n")
    rid = store.add_repo("u", "n", mirror)
    r = read_mirror_file(store, rid, "README.md")
    assert r.ok
    assert "# Title" in r.message


def test_read_mirror_file_path_traversal(store: Store, tmp_path: Path) -> None:
    mirror = tmp_path / "m"
    mirror.mkdir()
    rid = store.add_repo("u", "n", mirror)
    r = read_mirror_file(store, rid, "../../etc/passwd")
    assert not r.ok


def test_search_formatted(store: Store, tmp_path: Path) -> None:
    mirror = tmp_path / "m"
    mirror.mkdir()
    (mirror / "a.py").write_text("unique_token_xyz = 1\n")
    rid = store.add_repo("u", "n", mirror)
    store.index_mirror(rid, mirror)
    out = search_formatted(store, "unique_token_xyz")
    assert "unique_token_xyz" in out


def test_list_formatted_empty(store: Store) -> None:
    assert "No repositories" in list_formatted(store)


@patch("ssot_mcp.services.repos.enqueue_semantic_index")
def test_enqueue_semantic_for_repo_urls(mock_enq: MagicMock, store: Store, tmp_path: Path) -> None:
    a = tmp_path / "ma"
    b = tmp_path / "mb"
    a.mkdir()
    b.mkdir()
    store.add_repo("https://github.com/o/a.git", "a", a)
    store.add_repo("https://github.com/o/b.git", "b", b)
    enqueue_semantic_for_repo_urls(
        store,
        ["https://github.com/o/b.git", "https://github.com/o/a.git"],
    )
    assert mock_enq.call_count == 2


@patch("ssot_mcp.services.repos.enqueue_semantic_index")
@patch("ssot_mcp.services.repos._semantic_features_wanted", return_value=True)
def test_retry_semantic_indexing_queues(
    _wanted: MagicMock, mock_enq: MagicMock, store: Store, tmp_path: Path
) -> None:
    mp = tmp_path / "m"
    mp.mkdir()
    rid = store.add_repo("https://github.com/o/z.git", "z", mp)
    store.set_semantic_status(rid, "failed", "old error")
    store.set_semantic_chunk_count(rid, 99)
    r = retry_semantic_indexing(store, rid)
    assert r.ok
    mock_enq.assert_called_once_with(store.root, rid)
    row = store.list_repos()[0]
    assert row["semantic_status"] == "pending"
    assert int(row.get("semantic_chunk_count") or 0) == 0


@patch("ssot_mcp.services.repos._semantic_features_wanted", return_value=False)
def test_retry_semantic_indexing_disabled(_w: MagicMock, store: Store, tmp_path: Path) -> None:
    mp = tmp_path / "m"
    mp.mkdir()
    rid = store.add_repo("https://github.com/o/z.git", "z", mp)
    r = retry_semantic_indexing(store, rid)
    assert not r.ok
    row = store.list_repos()[0]
    assert row["semantic_status"] == "skipped"
    assert int(row.get("semantic_chunk_count") or 0) == 0


def test_retry_semantic_indexing_unknown_repo(store: Store) -> None:
    r = retry_semantic_indexing(store, "00000000-0000-0000-0000-000000000099")
    assert not r.ok


@patch("ssot_mcp.services.repos._semantic_features_wanted", return_value=True)
@patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index")
@patch("ssot_mcp.services.repos.git_ops.clone", side_effect=_fake_clone_factory())
def test_add_repository_defer_semantic_skips_enqueue(
    _mock_clone: MagicMock, mock_enq: MagicMock, _w: MagicMock, store: Store
) -> None:
    r = add_repository(store, "https://github.com/o/r.git", defer_semantic=True)
    assert r.ok
    mock_enq.assert_not_called()
    assert store.list_repos()[0]["semantic_status"] == "pending"


@patch("ssot_mcp.services.repos._semantic_features_wanted", return_value=False)
@patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index")
@patch("ssot_mcp.services.repos.git_ops.clone", side_effect=_fake_clone_factory())
def test_add_repository_semantic_off_sets_skipped(
    _mock_clone: MagicMock, mock_enq: MagicMock, _w: MagicMock, store: Store
) -> None:
    r = add_repository(store, "https://github.com/o/r.git")
    assert r.ok
    mock_enq.assert_not_called()
    assert store.list_repos()[0]["semantic_status"] == "skipped"
