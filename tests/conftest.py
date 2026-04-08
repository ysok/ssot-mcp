"""Shared fixtures for ssot-mcp tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ssot_mcp.core.store import Store


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def store(data_root: Path) -> Store:
    s = Store(data_root)
    s.init_db()
    return s
