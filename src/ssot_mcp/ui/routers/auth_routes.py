"""Login / logout routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ssot_mcp.ui import authn

router = APIRouter(tags=["auth"])
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login_form(request: Request) -> HTMLResponse | RedirectResponse:
    if authn.is_logged_in(request):
        return RedirectResponse(url="/repos", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"title": "Sign in", "show_nav": False, "flash": authn.pop_flash(request), "error": None},
    )


@router.post("/login", response_class=HTMLResponse, response_model=None)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    if authn.credentials_ok(username.strip(), password):
        authn.set_logged_in(request)
        return RedirectResponse(url="/repos", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "title": "Sign in",
            "show_nav": False,
            "flash": None,
            "error": "Invalid username or password.",
        },
        status_code=401,
    )


@router.post("/logout")
def logout_post(request: Request) -> RedirectResponse:
    authn.logout(request)
    authn.set_flash(request, "You have been signed out.", kind="info")
    return RedirectResponse(url="/login", status_code=303)
