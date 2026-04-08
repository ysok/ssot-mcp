"""Unit tests for repository list semantic column mapping."""

from __future__ import annotations

from ssot_mcp.ui.routers.repos_routes import _semantic_ui_row


def test_semantic_ui_na_when_no_lancedb() -> None:
    assert _semantic_ui_row(None, "pending", None, "rid")["kind"] == "na"


def test_semantic_ui_legacy_from_chunks_only() -> None:
    rid = "r1"
    assert _semantic_ui_row({rid: 0}, None, None, rid) == {"kind": "legacy_zero", "chunks": 0}
    assert _semantic_ui_row({rid: 5}, None, None, rid) == {"kind": "ready", "chunks": 5}


def test_semantic_ui_tracked_statuses() -> None:
    rid = "r1"
    m = {rid: 0}
    assert _semantic_ui_row(m, "skipped", None, rid)["kind"] == "skipped"
    assert _semantic_ui_row(m, "pending", None, rid) == {"kind": "pending", "chunks": 0}
    idx = _semantic_ui_row(m, "indexing", None, rid)
    assert idx["kind"] == "indexing"
    assert idx["chunks"] == 0
    assert "indexing_hint" in idx
    assert _semantic_ui_row(m, "ready", None, rid) == {"kind": "ready", "chunks": 0}
    f = _semantic_ui_row(m, "failed", "boom", rid)
    assert f["kind"] == "failed"
    assert f["chunks"] == 0
    assert "boom" in f["error"]


def test_semantic_ui_unknown_status_falls_back() -> None:
    rid = "r1"
    out = _semantic_ui_row({rid: 0}, "weird", None, rid)
    assert out["kind"] == "legacy_zero"


def test_semantic_ui_strips_status_whitespace() -> None:
    rid = "r1"
    m = {rid: 0}
    assert _semantic_ui_row(m, "  pending  ", None, rid)["kind"] == "pending"
    assert _semantic_ui_row(m, "  ready ", None, rid)["kind"] == "ready"
