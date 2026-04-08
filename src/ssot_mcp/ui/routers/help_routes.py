"""In-app help (e.g. Cursor MCP client setup)."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ssot_mcp.ui import authn

router = APIRouter(tags=["help"])
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _mcp_http_url() -> str:
    host = (os.environ.get("FASTMCP_HOST") or "0.0.0.0").strip()
    port = (os.environ.get("FASTMCP_PORT") or "8765").strip()
    if host in ("0.0.0.0", "::", "[::]"):
        display_host = "127.0.0.1"
    else:
        display_host = host
    return f"http://{display_host}:{port}/mcp"


@router.get("/help/cursor-mcp", response_class=HTMLResponse, response_model=None)
def cursor_mcp_help(request: Request) -> HTMLResponse | RedirectResponse:
    redir = authn.require_login(request)
    if redir:
        return redir
    mcp_url = _mcp_http_url()
    return templates.TemplateResponse(
        request,
        "cursor_mcp.html",
        {
            "title": "Cursor + MCP",
            "show_nav": True,
            "flash": authn.pop_flash(request),
            "mcp_url": mcp_url,
            "fastmcp_port": os.environ.get("FASTMCP_PORT", "8765"),
        },
    )
