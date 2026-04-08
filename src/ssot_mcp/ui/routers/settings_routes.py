"""Admin settings (GitHub API token for org import rate limits)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ssot_mcp.github.credentials import github_token_source, save_github_token
from ssot_mcp.ui import authn, config

router = APIRouter(tags=["settings"])
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _guard(request: Request) -> RedirectResponse | None:
    return authn.require_login(request)


@router.get("/settings/github", response_class=HTMLResponse, response_model=None)
def github_settings_form(request: Request) -> HTMLResponse | RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    root = config.data_root()
    return templates.TemplateResponse(
        request,
        "github_settings.html",
        {
            "title": "GitHub API token",
            "show_nav": True,
            "flash": authn.pop_flash(request),
            "token_source": github_token_source(root),
            "data_dir_hint": str(root),
        },
    )


@router.post("/settings/github")
def github_settings_save(
    request: Request,
    github_token: str = Form(""),
    clear_saved_token: str | None = Form(None),
) -> RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    root = config.data_root()
    if clear_saved_token:
        try:
            save_github_token("", root)
        except OSError as e:
            authn.set_flash(request, f"Could not remove saved token file: {e}", kind="error")
            return RedirectResponse(url="/settings/github", status_code=303)
        authn.set_flash(
            request,
            "Saved token file removed. Org import uses GITHUB_TOKEN or GITHUB_PERSONAL_ACCESS_TOKEN when set; otherwise unauthenticated API limits apply.",
            kind="success",
        )
    elif (github_token or "").strip():
        try:
            save_github_token(github_token, root)
        except OSError as e:
            authn.set_flash(request, f"Could not save token file: {e}", kind="error")
            return RedirectResponse(url="/settings/github", status_code=303)
        authn.set_flash(
            request,
            "GitHub token saved on the server. It is used when neither GITHUB_TOKEN nor GITHUB_PERSONAL_ACCESS_TOKEN is set in the environment.",
            kind="success",
        )
    else:
        authn.set_flash(
            request,
            "No change: enter a new token or check “Remove saved token”.",
            kind="warning",
        )
    return RedirectResponse(url="/settings/github", status_code=303)
