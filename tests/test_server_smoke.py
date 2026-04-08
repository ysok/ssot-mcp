"""Smoke tests for MCP server module."""

from __future__ import annotations

import os


def test_server_module_imports() -> None:
    from ssot_mcp.mcp import server as srv

    assert srv.mcp.name == "ssot-mcp"
    assert callable(srv.list_repositories)


def test_get_store_respects_ssot_data_dir(monkeypatch, tmp_path) -> None:
    from ssot_mcp.mcp import server as srv

    monkeypatch.setenv("SSOT_DATA_DIR", str(tmp_path / "d1"))
    srv._store = None
    s1 = srv.get_store()
    assert s1.root == (tmp_path / "d1").resolve()

    monkeypatch.setenv("SSOT_DATA_DIR", str(tmp_path / "d2"))
    srv._store = None
    s2 = srv.get_store()
    assert s2.root == (tmp_path / "d2").resolve()
