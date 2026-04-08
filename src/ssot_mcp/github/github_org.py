"""List public repositories for a GitHub organization (REST API)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ssot_mcp.github.credentials import effective_github_token


class GitHubApiError(Exception):
    pass


def parse_github_org(org_url_or_name: str) -> str:
    """
    Accepts:
      - https://github.com/orgname
      - github.com/orgname
      - orgname
    Rejects owner/repo URLs (use single-repo add instead).
    """
    raw = org_url_or_name.strip().rstrip("/")
    if not raw:
        raise GitHubApiError("Empty organization value.")
    if "github.com" in raw.lower():
        if not raw.lower().startswith(("http://", "https://")):
            raw = "https://" + raw
        from urllib.parse import urlparse

        u = urlparse(raw)
        path = u.path.strip("/")
        if not path:
            raise GitHubApiError("Could not parse organization from URL (empty path).")
        parts = path.split("/")
        if parts[0] == "orgs" and len(parts) >= 2:
            return parts[1]
        if len(parts) == 1:
            return parts[0]
        raise GitHubApiError(
            "URL looks like a repository (owner/repo). Use `add` for one repo, "
            "or pass only the org: https://github.com/ORG"
        )
    if "/" in raw:
        raise GitHubApiError(
            "Use `https://github.com/ORG` or the org login name only, not owner/repo."
        )
    if len(raw) > 39 or not re.match(r"^[\w.-]+$", raw):
        raise GitHubApiError(f"Invalid GitHub org login: {raw!r}")
    return raw


def list_public_clone_urls(
    org: str,
    *,
    token: str | None = None,
    exclude_forks: bool = False,
    exclude_archived: bool = False,
    data_root: Path | None = None,
) -> list[str]:
    """Return https clone URLs for all public repos in the org (paginated)."""
    if token is not None and str(token).strip():
        token = str(token).strip()
    else:
        token = effective_github_token(data_root)
    org_enc = quote(org, safe="")
    out: list[str] = []
    page = 1
    while True:
        qs = urlencode({"type": "public", "per_page": "100", "page": str(page), "sort": "full_name"})
        url = f"https://api.github.com/orgs/{org_enc}/repos?{qs}"
        req = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ssot-mcp",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
                payload = json.loads(err_body) if err_body else {}
                msg = payload.get("message", e.reason)
            except Exception:
                msg = e.reason or str(e.code)
            if e.code == 404:
                raise GitHubApiError(
                    f"Organization not found or not visible: {org!r}. "
                    "Check the name, or set GITHUB_TOKEN or GITHUB_PERSONAL_ACCESS_TOKEN if the org is only visible when authenticated."
                ) from e
            if e.code == 403:
                raise GitHubApiError(
                    f"GitHub API refused access (403): {msg}. "
                    "For higher rate limits set GITHUB_TOKEN or GITHUB_PERSONAL_ACCESS_TOKEN (classic PAT or fine-grained with read access to metadata)."
                ) from e
            raise GitHubApiError(f"GitHub API HTTP {e.code}: {msg}") from e
        except URLError as e:
            raise GitHubApiError(f"Network error calling GitHub API: {e.reason}") from e

        data = json.loads(body)
        if isinstance(data, dict) and "message" in data:
            raise GitHubApiError(str(data["message"]))

        if not isinstance(data, list):
            raise GitHubApiError("Unexpected GitHub API response shape.")

        if not data:
            break

        for repo in data:
            if repo.get("private"):
                continue
            if exclude_archived and repo.get("archived"):
                continue
            if exclude_forks and repo.get("fork"):
                continue
            clone = repo.get("clone_url") or ""
            if clone:
                out.append(clone)

        if len(data) < 100:
            break
        page += 1

    return out
