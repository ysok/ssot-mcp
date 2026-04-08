"""Clone and update git mirrors."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

_TIMEOUT_MIN = 60
_TIMEOUT_MAX = 86400  # 24 hours (large org imports / huge shallow clones)


def _timeout_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(_TIMEOUT_MIN, min(v, _TIMEOUT_MAX))


def clone_timeout_seconds() -> int:
    """Max seconds for `git clone` (env SSOT_GIT_CLONE_TIMEOUT, default 600)."""
    return _timeout_from_env("SSOT_GIT_CLONE_TIMEOUT", 600)


def sync_timeout_seconds() -> int:
    """Max seconds for main fetch/pull steps in `git sync` (SSOT_GIT_SYNC_TIMEOUT, default 600)."""
    return _timeout_from_env("SSOT_GIT_SYNC_TIMEOUT", 600)


def slug_from_url(url: str) -> str:
    u = url.rstrip("/").removesuffix(".git")
    parsed = urlparse(u)
    path = parsed.path or u
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}__{parts[-1]}"
    if parts:
        return parts[-1]
    return re.sub(r"[^\w.-]+", "_", u)[:80] or "repo"


def clone(url: str, dest: Path, timeout: int | None = None) -> None:
    if timeout is None:
        timeout = clone_timeout_seconds()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"Mirror path already exists: {dest}")
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def sync(mirror: Path, timeout: int | None = None) -> None:
    if timeout is None:
        timeout = sync_timeout_seconds()
    if not (mirror / ".git").is_dir():
        raise FileNotFoundError(f"Not a git mirror: {mirror}")
    subprocess.run(
        ["git", "-C", str(mirror), "fetch", "--depth", "1", "origin"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    # Prefer default branch from remote
    subprocess.run(
        ["git", "-C", str(mirror), "remote", "set-head", "origin", "-a"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    head = subprocess.run(
        ["git", "-C", str(mirror), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    branch = "main"
    if head.returncode == 0 and head.stdout.strip():
        branch = head.stdout.strip().removeprefix("origin/")
    for b in (branch, "main", "master"):
        rr = subprocess.run(
            ["git", "-C", str(mirror), "reset", "--hard", f"origin/{b}"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if rr.returncode == 0:
            return
    subprocess.run(
        ["git", "-C", str(mirror), "pull", "--ff-only"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def display_name_for_url(url: str) -> str:
    return slug_from_url(url).replace("__", "/")
