"""Tests for git_ops."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ssot_mcp.git import git_ops


@pytest.mark.parametrize(
    ("url", "expected_slug"),
    [
        ("https://github.com/foo/bar.git", "foo__bar"),
        ("https://github.com/foo/bar", "foo__bar"),
        # SCP-style URLs are not parsed as https; slug uses last two path segments.
        ("git@github.com:org/repo.git", "git@github.com:org__repo"),
        ("https://example.com/onlyone", "onlyone"),
    ],
)
def test_slug_from_url(url: str, expected_slug: str) -> None:
    assert git_ops.slug_from_url(url) == expected_slug


def test_display_name_for_url() -> None:
    assert git_ops.display_name_for_url("https://github.com/a/b.git") == "a/b"


def test_clone_raises_when_dest_exists(tmp_path: Path) -> None:
    dest = tmp_path / "exists"
    dest.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        git_ops.clone("https://example.com/r.git", dest)


@patch("ssot_mcp.git.git_ops.subprocess.run")
def test_clone_invokes_git(mock_run: MagicMock, tmp_path: Path) -> None:
    dest = tmp_path / "mirror"
    git_ops.clone("https://github.com/o/r.git", dest)
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0][:4] == ["git", "clone", "--depth", "1"]
    assert args[0][4] == "https://github.com/o/r.git"
    assert args[0][5] == str(dest)
    assert kwargs.get("check") is True


def test_sync_requires_git_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Not a git mirror"):
        git_ops.sync(tmp_path)


def test_clone_timeout_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSOT_GIT_CLONE_TIMEOUT", "1234")
    assert git_ops.clone_timeout_seconds() == 1234


def test_clone_timeout_invalid_env_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSOT_GIT_CLONE_TIMEOUT", "not-a-number")
    assert git_ops.clone_timeout_seconds() == 600


def test_clone_timeout_clamped_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSOT_GIT_CLONE_TIMEOUT", "30")
    assert git_ops.clone_timeout_seconds() == 60


@patch("ssot_mcp.git.git_ops.subprocess.run")
def test_sync_resets_on_first_successful_branch(mock_run: MagicMock, tmp_path: Path) -> None:
    mirror = tmp_path / "m"
    (mirror / ".git").mkdir(parents=True)

    def side_effect(cmd, **kwargs):
        m = MagicMock()
        if "symbolic-ref" in cmd:
            m.returncode = 0
            m.stdout = "origin/main\n"
        elif "reset" in cmd and "origin/main" in cmd:
            m.returncode = 0
        elif "fetch" in cmd:
            m.returncode = 0
        elif "set-head" in cmd:
            m.returncode = 0
        else:
            m.returncode = 1
        return m

    mock_run.side_effect = side_effect
    git_ops.sync(mirror)
    assert mock_run.call_count >= 2
