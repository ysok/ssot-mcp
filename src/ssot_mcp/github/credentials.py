"""Resolve GitHub API token: environment (see _github_token_from_env) overrides file under SSOT_DATA_DIR."""

from __future__ import annotations

import os
import stat
from pathlib import Path

# Checked in order; first non-empty wins.
_GITHUB_TOKEN_ENV_KEYS = ("GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")


def _github_token_from_env() -> str | None:
    for key in _GITHUB_TOKEN_ENV_KEYS:
        v = os.environ.get(key, "").strip()
        if v:
            return v
    return None


TOKEN_FILENAME = ".github_token"


def github_token_path(data_root: Path | None = None) -> Path:
    root = data_root if data_root is not None else Path(os.environ.get("SSOT_DATA_DIR", "/data")).resolve()
    return root / TOKEN_FILENAME


def read_saved_github_token(data_root: Path | None = None) -> str | None:
    p = github_token_path(data_root)
    if not p.is_file():
        return None
    try:
        t = p.read_text(encoding="utf-8").strip()
        return t or None
    except OSError:
        return None


def effective_github_token(data_root: Path | None = None) -> str | None:
    env = _github_token_from_env()
    if env:
        return env
    return read_saved_github_token(data_root)


def github_token_source(data_root: Path | None = None) -> str:
    """Return 'env', 'file', or 'none' for UI display (no secret material)."""
    if _github_token_from_env():
        return "env"
    if read_saved_github_token(data_root):
        return "file"
    return "none"


def save_github_token(token: str, data_root: Path | None = None) -> None:
    """Write token to data dir file (0600). Pass empty string to remove file."""
    root = data_root if data_root is not None else Path(os.environ.get("SSOT_DATA_DIR", "/data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    p = github_token_path(root)
    t = token.strip()
    if not t:
        p.unlink(missing_ok=True)
        return
    p.write_text(t + "\n", encoding="utf-8")
    try:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
