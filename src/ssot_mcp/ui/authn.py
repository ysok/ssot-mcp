"""Session-backed login for the admin UI."""

from __future__ import annotations

import secrets

from fastapi import Request
from starlette.responses import RedirectResponse

from ssot_mcp.ui import config

SESSION_USER = "ssot_ui_authenticated"
FLASH_KEY = "ssot_ui_flash"


def credentials_ok(username: str, password: str) -> bool:
    u = config.ui_username()
    p = config.ui_password()
    if not u or not p:
        return False
    return secrets.compare_digest(username, u) and secrets.compare_digest(password, p)


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get(SESSION_USER))


def require_login(request: Request) -> RedirectResponse | None:
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=303)
    return None


def set_logged_in(request: Request) -> None:
    request.session[SESSION_USER] = True


def logout(request: Request) -> None:
    request.session.clear()


def set_flash(request: Request, message: str, *, kind: str = "info") -> None:
    """Store a one-time message. kind is one of: success, error, warning, info."""
    if kind not in ("success", "error", "warning", "info"):
        kind = "info"
    request.session[FLASH_KEY] = {"text": message, "kind": kind}


def pop_flash(request: Request) -> dict | None:
    """Return {\"text\": str, \"kind\": str} or None. Accepts legacy string-only session values."""
    raw = request.session.pop(FLASH_KEY, None)
    if raw is None:
        return None
    if isinstance(raw, str):
        return {"text": raw, "kind": "info"}
    if isinstance(raw, dict) and raw.get("text") is not None:
        k = raw.get("kind", "info")
        if k not in ("success", "error", "warning", "info"):
            k = "info"
        return {"text": str(raw["text"]), "kind": k}
    return {"text": str(raw), "kind": "info"}
