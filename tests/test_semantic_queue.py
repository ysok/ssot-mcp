"""Tests for background semantic indexing queue."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from ssot_mcp.core.store import Store
from ssot_mcp.embeddings import semantic_queue


def test_run_semantic_job_no_repo_clears_active_id(store: Store, tmp_path: Path) -> None:
    """Missing DB row should not leave repo_id stuck in the dedupe set."""
    ghost = str(uuid.uuid4())
    semantic_queue._active_ids.add(ghost)
    semantic_queue._run_semantic_job(store.root.resolve(), ghost)
    assert ghost not in semantic_queue._active_ids


def _fake_reindex_ok(s: Store, repo_id: str, _mirror: object) -> str:
    s.set_semantic_chunk_count(repo_id, 5)
    return "ok"


@patch(
    "ssot_mcp.embeddings.semantic.reindex_repository_semantic",
    side_effect=_fake_reindex_ok,
)
def test_run_semantic_job_success_sets_ready(_mock_reindex: object, store: Store, tmp_path: Path) -> None:
    mp = tmp_path / "m"
    mp.mkdir()
    (mp / "a.py").write_text("x = 1\n")
    rid = store.add_repo("https://github.com/o/t.git", "t", mp)
    semantic_queue._run_semantic_job(store.root.resolve(), rid)
    row = store.list_repos()[0]
    assert row["semantic_status"] == "ready"
    assert int(row.get("semantic_chunk_count") or 0) == 5


@patch("ssot_mcp.embeddings.semantic.reindex_repository_semantic", side_effect=RuntimeError("embed fail"))
def test_run_semantic_job_failure_sets_failed(_mock_reindex: object, store: Store, tmp_path: Path) -> None:
    mp = tmp_path / "m"
    mp.mkdir()
    (mp / "a.py").write_text("x = 1\n")
    rid = store.add_repo("https://github.com/o/t.git", "t", mp)
    semantic_queue._run_semantic_job(store.root.resolve(), rid)
    row = store.list_repos()[0]
    assert row["semantic_status"] == "failed"
    assert "embed fail" in (row.get("semantic_error") or "")
    assert int(row.get("semantic_chunk_count") or 0) == 0
