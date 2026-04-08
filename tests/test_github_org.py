"""Tests for github_org."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from ssot_mcp.github.github_org import GitHubApiError, list_public_clone_urls, parse_github_org


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://github.com/kubernetes", "kubernetes"),
        ("https://github.com/kubernetes/", "kubernetes"),
        ("github.com/foo", "foo"),
        ("https://github.com/orgs/acme/repositories", "acme"),
        ("my-org", "my-org"),
    ],
)
def test_parse_github_org_ok(raw: str, expected: str) -> None:
    assert parse_github_org(raw) == expected


def test_parse_github_org_empty() -> None:
    with pytest.raises(GitHubApiError, match="Empty"):
        parse_github_org("   ")


def test_parse_github_org_rejects_owner_repo() -> None:
    with pytest.raises(GitHubApiError, match="repository"):
        parse_github_org("https://github.com/foo/bar")


def test_parse_github_org_rejects_slash_name() -> None:
    with pytest.raises(GitHubApiError):
        parse_github_org("foo/bar")


@patch("ssot_mcp.github.github_org.urlopen")
def test_list_public_clone_urls_single_page(mock_urlopen: MagicMock) -> None:
    payload = [
        {"clone_url": "https://github.com/o/a.git", "private": False, "archived": False, "fork": False},
        {"clone_url": "https://github.com/o/b.git", "private": True},
        {"clone_url": "https://github.com/o/c.git", "private": False, "archived": True},
        {"clone_url": "https://github.com/o/d.git", "private": False, "fork": True},
    ]
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    all_urls = list_public_clone_urls("o", token=None, exclude_forks=False, exclude_archived=False)
    assert all_urls == ["https://github.com/o/a.git", "https://github.com/o/c.git", "https://github.com/o/d.git"]

    no_arch = list_public_clone_urls("o", token=None, exclude_archived=True)
    assert no_arch == ["https://github.com/o/a.git", "https://github.com/o/d.git"]

    no_fork = list_public_clone_urls("o", token=None, exclude_forks=True)
    assert no_fork == ["https://github.com/o/a.git", "https://github.com/o/c.git"]


@patch("ssot_mcp.github.github_org.urlopen")
def test_list_public_clone_urls_pagination(mock_urlopen: MagicMock) -> None:
    page1 = [{"clone_url": f"https://github.com/o/r{i}.git", "private": False} for i in range(100)]
    page2 = [{"clone_url": "https://github.com/o/last.git", "private": False}]

    responses = []
    for page in (page1, page2):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(page).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        responses.append(mock_resp)

    mock_urlopen.side_effect = responses

    urls = list_public_clone_urls("o", token=None)
    assert len(urls) == 101
    assert urls[-1] == "https://github.com/o/last.git"


@patch("ssot_mcp.github.github_org.urlopen")
def test_list_public_clone_urls_404(mock_urlopen: MagicMock) -> None:
    from urllib.error import HTTPError

    fp = BytesIO(b'{"message":"Not Found"}')
    err = HTTPError("https://api.github.com/orgs/missing-org/repos", 404, "Not Found", {}, fp)

    mock_urlopen.side_effect = err

    with pytest.raises(GitHubApiError, match="Organization not found"):
        list_public_clone_urls("missing-org", token=None)
