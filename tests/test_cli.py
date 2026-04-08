"""Tests for CLI entrypoint."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ssot_mcp.cli.main import main


def test_cli_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_cli_list_empty(store, monkeypatch: pytest.MonkeyPatch, data_root) -> None:
    monkeypatch.setenv("SSOT_DATA_DIR", str(data_root))
    code = main(["--data-dir", str(data_root), "list"])
    assert code == 0


@patch("ssot_mcp.cli.main.add_repository")
def test_cli_add_calls_core(mock_add: MagicMock, data_root, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSOT_DATA_DIR", str(data_root))
    mock_add.return_value = __import__("ssot_mcp.services.repos", fromlist=["ActionResult"]).ActionResult(True, "done")
    code = main(["--data-dir", str(data_root), "add", "https://github.com/o/r.git"])
    assert code == 0
    mock_add.assert_called_once()


@patch("ssot_mcp.cli.main.semantic_search_formatted", return_value="hit")
def test_cli_semantic_search_ok(_mock_sem: MagicMock, data_root, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSOT_DATA_DIR", str(data_root))
    code = main(["--data-dir", str(data_root), "semantic-search", "hello"])
    assert code == 0


@patch("ssot_mcp.cli.main.semantic_search_formatted", return_value="No semantic matches x")
def test_cli_semantic_search_fail(_mock_sem: MagicMock, data_root, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSOT_DATA_DIR", str(data_root))
    code = main(["--data-dir", str(data_root), "semantic-search", "hello"])
    assert code == 1
