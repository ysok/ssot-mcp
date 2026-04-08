"""Unit tests for UI auth helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")

from ssot_mcp.ui import authn


def test_credentials_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSOT_UI_USER", "admin")
    monkeypatch.setenv("SSOT_UI_PASSWORD", "secret123")
    assert authn.credentials_ok("admin", "secret123") is True
    assert authn.credentials_ok("admin", "wrong") is False
    assert authn.credentials_ok("root", "secret123") is False


def test_credentials_timing_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSOT_UI_USER", "admin")
    monkeypatch.setenv("SSOT_UI_PASSWORD", "x")
    assert authn.credentials_ok("admin", "y") is False


def test_flash_set_pop_with_kind() -> None:
    req = MagicMock()
    req.session = {}
    authn.set_flash(req, "hello", kind="error")
    assert authn.pop_flash(req) == {"text": "hello", "kind": "error"}
    assert authn.pop_flash(req) is None


def test_flash_pop_legacy_string() -> None:
    req = MagicMock()
    req.session = {authn.FLASH_KEY: "legacy"}
    assert authn.pop_flash(req) == {"text": "legacy", "kind": "info"}
