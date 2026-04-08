"""Run the admin UI with Uvicorn (default port from ssot_mcp.ui.config.DEFAULT_UI_PORT)."""

from __future__ import annotations

import os

import uvicorn

from ssot_mcp.ui import config
from ssot_mcp.ui.app import create_app


def run() -> None:
    host = os.environ.get("SSOT_UI_HOST", "0.0.0.0")
    port = config.ui_port()
    uvicorn.run(create_app(), host=host, port=port, log_level=os.environ.get("SSOT_UI_LOG_LEVEL", "info"))


if __name__ == "__main__":
    run()
