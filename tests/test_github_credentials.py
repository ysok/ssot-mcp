"""Tests for GitHub token resolution (env vs data-dir file)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ssot_mcp.github.credentials import (
    effective_github_token,
    github_token_source,
    read_saved_github_token,
    save_github_token,
)
from ssot_mcp.github.github_org import list_public_clone_urls


def _unset_github_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)


def test_effective_github_token_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _unset_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    save_github_token("file-token", tmp_path)
    assert effective_github_token(tmp_path) == "env-token"


def test_effective_github_personal_access_token_when_token_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _unset_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "pat-only")
    assert effective_github_token(tmp_path) == "pat-only"


def test_github_token_precedence_over_personal_access_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _unset_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "primary")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "secondary")
    assert effective_github_token(tmp_path) == "primary"


def test_effective_github_token_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _unset_github_env(monkeypatch)
    save_github_token("  pat-from-file  ", tmp_path)
    assert effective_github_token(tmp_path) == "pat-from-file"


def test_effective_github_token_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _unset_github_env(monkeypatch)
    assert effective_github_token(tmp_path) is None


def test_github_token_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _unset_github_env(monkeypatch)
    assert github_token_source(tmp_path) == "none"
    save_github_token("x", tmp_path)
    assert github_token_source(tmp_path) == "file"
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "from-pat-env")
    assert github_token_source(tmp_path) == "env"
    monkeypatch.setenv("GITHUB_TOKEN", "e")
    assert github_token_source(tmp_path) == "env"


def test_save_github_token_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _unset_github_env(monkeypatch)
    save_github_token("abc", tmp_path)
    assert read_saved_github_token(tmp_path) == "abc"
    save_github_token("", tmp_path)
    assert read_saved_github_token(tmp_path) is None


@patch("ssot_mcp.github.github_org.urlopen")
def test_list_public_clone_urls_sends_saved_token(
    mock_urlopen: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unset_github_env(monkeypatch)
    save_github_token("ghp_from_file_test", tmp_path)
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"[]"
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    list_public_clone_urls("someorg", token=None, data_root=tmp_path)
    req = mock_urlopen.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer ghp_from_file_test"
