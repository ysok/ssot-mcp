"""UI configuration from environment."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

# Default listen port when SSOT_UI_PORT is unset (local `ssot-mcp-ui`). Keep equal to
# Containerfile `ENV … SSOT_UI_PORT=…` so image and dev defaults match.
DEFAULT_UI_PORT = 8081


def data_root() -> Path:
    return Path(os.environ.get("SSOT_DATA_DIR", "/data")).resolve()


def session_secret() -> str:
    s = os.environ.get("SSOT_UI_SECRET", "").strip()
    if s:
        return s
    return "dev-insecure-change-SSOT_UI_SECRET-" + secrets.token_hex(8)


def ui_username() -> str:
    return os.environ.get("SSOT_UI_USER", "admin").strip()


def ui_password() -> str:
    return os.environ.get("SSOT_UI_PASSWORD", "P#9djrifjkf!dd")


def per_page() -> int:
    try:
        return max(1, min(100, int(os.environ.get("SSOT_UI_PAGE_SIZE", "50"))))
    except ValueError:
        return 50


def ui_port() -> int:
    try:
        return int(os.environ.get("SSOT_UI_PORT", str(DEFAULT_UI_PORT)))
    except ValueError:
        return DEFAULT_UI_PORT
