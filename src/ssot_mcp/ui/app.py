"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ssot_mcp.ui import config
from ssot_mcp.ui.routers import auth_routes, help_routes, repos_routes, settings_routes

UI_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    app = FastAPI(title="Single Source of Truth - MCP", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret(),
        session_cookie="ssot_ui_session",
        https_only=False,
        same_site="lax",
    )

    app.mount("/static", StaticFiles(directory=str(UI_DIR / "static")), name="static")

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse(url="/repos", status_code=302)

    app.include_router(auth_routes.router)
    app.include_router(repos_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(help_routes.router)

    return app
