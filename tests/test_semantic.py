"""Tests for semantic helpers and optional LanceDB paths."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ssot_mcp.embeddings.semantic import (
    chunk_text,
    local_embeddings_installed,
    semantic_api_configured,
    semantic_dependencies_installed,
    semantic_indexing_enabled,
    semantic_search,
    semantic_search_formatted,
)


def test_chunk_text_empty() -> None:
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_chunk_text_short() -> None:
    assert chunk_text("hello world") == ["hello world"]


def test_chunk_text_splits_long() -> None:
    body = "a" * 5000
    parts = chunk_text(body, max_chars=1000, overlap=50)
    assert len(parts) > 1
    assert all(len(p) <= 1000 for p in parts)


def test_semantic_indexing_enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSOT_SEMANTIC_INDEX", raising=False)
    assert semantic_indexing_enabled() is True
    monkeypatch.setenv("SSOT_SEMANTIC_INDEX", "0")
    assert semantic_indexing_enabled() is False


def test_semantic_api_configured_matches_fastembed() -> None:
    assert semantic_api_configured() is local_embeddings_installed()


def test_semantic_dependencies_installed() -> None:
    assert isinstance(semantic_dependencies_installed(), bool)


def test_semantic_search_no_lance_dir(store, monkeypatch: pytest.MonkeyPatch) -> None:
    if not semantic_dependencies_installed():
        pytest.skip("lancedb not installed")
    assert semantic_search(store, "hello") == []


@pytest.mark.skipif(not semantic_dependencies_installed(), reason="lancedb optional")
def test_semantic_roundtrip_with_mock_embed(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Embeddings are always local (fastembed); mock _embed_batch for unit test."""
    from ssot_mcp.embeddings.semantic import reindex_repository_semantic

    fake_vec = [0.05] * 384

    def fake_embed(texts: list[str]) -> list[list[float]]:
        return [list(fake_vec) for _ in texts]

    mp = Path(store.root) / "m_sem"
    mp.mkdir()
    (mp / "b.py").write_text("def unit_semantic_marker():\n    return 1\n")
    rid = store.add_repo("https://example.com/l.git", "l", mp)

    with patch("ssot_mcp.embeddings.semantic.local_embeddings_installed", return_value=True), patch(
        "ssot_mcp.embeddings.semantic._embed_batch", side_effect=fake_embed
    ):
        msg = reindex_repository_semantic(store, rid, mp)
    assert msg is not None
    assert "chunk" in msg.lower()

    with patch("ssot_mcp.embeddings.semantic.local_embeddings_installed", return_value=True), patch(
        "ssot_mcp.embeddings.semantic._embed_batch", side_effect=fake_embed
    ):
        hits = semantic_search(store, "marker function", top_k=5)
    assert len(hits) >= 1
    assert any("unit_semantic_marker" in h.get("text", "") for h in hits)


@pytest.mark.skipif(not semantic_dependencies_installed(), reason="lancedb optional")
def test_semantic_roundtrip_via_add_repository_mocked(store, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch as p

    from ssot_mcp.embeddings.semantic import reindex_repository_semantic
    from ssot_mcp.services.repos import add_repository

    fake_vec = [0.1] * 384

    def fake_embed(texts: list[str]) -> list[list[float]]:
        return [list(fake_vec) for _ in texts]

    with p("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index"), p(
        "ssot_mcp.services.repos.git_ops.clone"
    ) as mc:

        def clone(url, dest, timeout=600):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "a.py").write_text("def semantic_test_fn():\n    pass\n")

        mc.side_effect = clone
        r = add_repository(store, "https://github.com/o/r.git")
        assert r.ok

    row = store.list_repos()[0]
    rid = row["id"]
    mirror = Path(row["mirror_path"])

    with patch("ssot_mcp.embeddings.semantic.local_embeddings_installed", return_value=True), patch(
        "ssot_mcp.embeddings.semantic._embed_batch", side_effect=fake_embed
    ):
        msg = reindex_repository_semantic(store, rid, mirror)
    assert msg is not None
    assert "chunk" in msg.lower()

    with patch("ssot_mcp.embeddings.semantic.local_embeddings_installed", return_value=True), patch(
        "ssot_mcp.embeddings.semantic._embed_batch", side_effect=fake_embed
    ):
        hits = semantic_search(store, "test function", top_k=5)
    assert len(hits) >= 1
    assert any("semantic_test_fn" in h.get("text", "") for h in hits)


def test_semantic_search_formatted_runtime_error(store, monkeypatch: pytest.MonkeyPatch) -> None:
    if semantic_dependencies_installed():
        out = semantic_search_formatted(store, "q")
        assert "No semantic matches" in out or "fastembed" in out.lower()
    else:
        assert "pip install" in semantic_search_formatted(store, "q").lower()
