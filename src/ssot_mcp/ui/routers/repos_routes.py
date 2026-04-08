"""Repository management UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ssot_mcp.core.store import semantic_indexing_activity_hint
from ssot_mcp.embeddings.semantic import semantic_dependencies_installed
from ssot_mcp.services.repos import add_repository, remove_repository, retry_semantic_indexing
from ssot_mcp.ui import authn
from ssot_mcp.ui import config
from ssot_mcp.ui.deps import StoreDep
from ssot_mcp.ui.org_import_job import read_org_import_state, start_org_import_job

router = APIRouter(tags=["repos"])
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _guard(request: Request) -> RedirectResponse | None:
    return authn.require_login(request)


def _normalize_semantic_status(status: str | None) -> str | None:
    if status is None:
        return None
    s = str(status).strip()
    return s if s else None


def _semantic_ui_row(
    sem_map: dict[str, int] | None,
    status: str | None,
    error: str | None,
    repo_id: str,
    *,
    heartbeat_at: str | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Build template-friendly semantic column state (counts + status colors + retry)."""
    if sem_map is None:
        return {"kind": "na"}
    status = _normalize_semantic_status(status)
    chunks = int(sem_map.get(repo_id, 0))
    if status is None:
        if chunks > 0:
            return {"kind": "ready", "chunks": chunks}
        return {"kind": "legacy_zero", "chunks": 0}
    if status == "skipped":
        return {"kind": "skipped"}
    if status == "pending":
        return {"kind": "pending", "chunks": chunks}
    if status == "indexing":
        return {
            "kind": "indexing",
            "chunks": chunks,
            "indexing_hint": semantic_indexing_activity_hint(heartbeat_at, started_at),
        }
    if status == "failed":
        return {"kind": "failed", "chunks": chunks, "error": (error or "")[:400]}
    if status == "ready":
        return {"kind": "ready", "chunks": chunks}
    return {"kind": "legacy_zero", "chunks": chunks}


@router.get("/repos", response_class=HTMLResponse, response_model=None)
def list_repos(request: Request, store: StoreDep, page: int = 1) -> HTMLResponse | RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    per = config.per_page()
    page = max(1, page)
    rows, total = store.list_repos_page(page, per)
    total_pages = max(1, (total + per - 1) // per) if total else 1
    stats_notes: list[str] = []
    if semantic_dependencies_installed():
        sem_map = {str(r["id"]): int(r["semantic_chunk_count"] or 0) for r in rows}
    else:
        sem_map = None
    enriched: list[dict] = []
    for r in rows:
        d = dict(r)
        rid = d["id"]
        d["fts_files"] = int(d.get("keyword_fts_file_count") or 0)
        d["semantic_chunks"] = None if sem_map is None else sem_map.get(rid, 0)
        d["semantic_ui"] = _semantic_ui_row(
            sem_map,
            d.get("semantic_status"),
            d.get("semantic_error"),
            rid,
            heartbeat_at=d.get("semantic_indexing_heartbeat_at"),
            started_at=d.get("semantic_indexing_started_at"),
        )
        enriched.append(d)
    page_warning = " ".join(stats_notes) if stats_notes else None
    return templates.TemplateResponse(
        request,
        "repos_list.html",
        {
            "title": "Repositories",
            "show_nav": True,
            "repos": enriched,
            "page": page,
            "per_page": per,
            "total": total,
            "total_pages": total_pages,
            "flash": authn.pop_flash(request),
            "page_warning": page_warning,
        },
    )


@router.get("/repos/new", response_class=HTMLResponse, response_model=None)
def new_repo_form(request: Request) -> HTMLResponse | RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    return templates.TemplateResponse(
        request,
        "repo_add.html",
        {
            "title": "Add repository",
            "show_nav": True,
            "flash": authn.pop_flash(request),
            "error": None,
        },
    )


@router.post("/repos/new")
def new_repo_submit(
    request: Request,
    store: StoreDep,
    url: str = Form(...),
) -> RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    try:
        r = add_repository(store, url.strip())
    except Exception as e:
        authn.set_flash(
            request,
            f"Unexpected error while adding repository: {e}",
            kind="error",
        )
        return RedirectResponse(url="/repos/new", status_code=303)
    if r.ok:
        authn.set_flash(
            request,
            r.message.replace("\n\n", " — ").replace("\n", " ")[:4000],
            kind="success",
        )
        return RedirectResponse(url="/repos", status_code=303)
    authn.set_flash(request, r.message[:8000], kind="error")
    return RedirectResponse(url="/repos/new", status_code=303)


@router.get("/repos/{repo_id}/edit", response_class=HTMLResponse, response_model=None)
def edit_repo_form(request: Request, store: StoreDep, repo_id: str) -> HTMLResponse | RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    row = store.get_repo_detail(repo_id)
    if not row:
        authn.set_flash(request, "Repository not found.", kind="error")
        return RedirectResponse(url="/repos", status_code=303)
    return templates.TemplateResponse(
        request,
        "repo_edit.html",
        {
            "title": "Edit repository",
            "show_nav": True,
            "repo": row,
            "flash": authn.pop_flash(request),
            "error": None,
        },
    )


@router.post("/repos/{repo_id}/edit")
def edit_repo_submit(
    request: Request,
    store: StoreDep,
    repo_id: str,
    display_name: str = Form(...),
) -> RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    if store.update_repo_display_name(repo_id, display_name):
        authn.set_flash(request, "Display name updated.", kind="success")
    else:
        authn.set_flash(request, "Could not update (empty name or unknown id).", kind="error")
    return RedirectResponse(url="/repos", status_code=303)


@router.post("/repos/{repo_id}/delete")
def delete_repo(
    request: Request,
    store: StoreDep,
    repo_id: str,
) -> RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    try:
        r = remove_repository(store, repo_id)
    except Exception as e:
        authn.set_flash(request, f"Unexpected error while removing repository: {e}", kind="error")
        return RedirectResponse(url="/repos", status_code=303)
    authn.set_flash(request, r.message[:4000], kind="success" if r.ok else "error")
    return RedirectResponse(url="/repos", status_code=303)


@router.post("/repos/{repo_id}/semantic-retry")
def semantic_retry(
    request: Request,
    store: StoreDep,
    repo_id: str,
) -> RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    try:
        r = retry_semantic_indexing(store, repo_id)
    except Exception as e:
        authn.set_flash(request, f"Could not queue semantic retry: {e}", kind="error")
        return RedirectResponse(url="/repos", status_code=303)
    authn.set_flash(
        request,
        r.message[:2000],
        kind="success" if r.ok else "error",
    )
    return RedirectResponse(url="/repos", status_code=303)


@router.get("/import-org", response_class=HTMLResponse, response_model=None)
def import_org_form(request: Request) -> HTMLResponse | RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    return templates.TemplateResponse(
        request,
        "org_import.html",
        {
            "title": "Import GitHub organization",
            "show_nav": True,
            "flash": authn.pop_flash(request),
        },
    )


@router.post("/import-org")
def import_org_submit(
    request: Request,
    store: StoreDep,
    org: str = Form(...),
    no_forks: str | None = Form(None),
    no_archived: str | None = Form(None),
) -> RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    try:
        started = start_org_import_job(
            store,
            org.strip(),
            exclude_forks=bool(no_forks),
            exclude_archived=bool(no_archived),
        )
    except Exception as e:
        authn.set_flash(
            request,
            f"Could not start organization import: {e}",
            kind="error",
        )
        return RedirectResponse(url="/import-org", status_code=303)
    if started == "error":
        authn.set_flash(request, "Organization value is empty.", kind="error")
        return RedirectResponse(url="/import-org", status_code=303)
    if started == "busy":
        authn.set_flash(
            request,
            "An organization import is already running. Open import progress or wait until it finishes.",
            kind="warning",
        )
        return RedirectResponse(url="/import-org/progress", status_code=303)
    authn.set_flash(
        request,
        "Import started in the background. You can open Repositories or stay on this page while it runs.",
        kind="success",
    )
    return RedirectResponse(url="/import-org/progress", status_code=303)


@router.get("/import-org/progress", response_class=HTMLResponse, response_model=None)
def import_org_progress(request: Request, store: StoreDep) -> HTMLResponse | RedirectResponse:
    redir = _guard(request)
    if redir:
        return redir
    state = read_org_import_state(store.root)
    return templates.TemplateResponse(
        request,
        "org_import_progress.html",
        {
            "title": "Organization import progress",
            "show_nav": True,
            "flash": authn.pop_flash(request),
            "org_import_state": state,
        },
    )


@router.get("/import-org/progress.json")
def import_org_progress_json(request: Request, store: StoreDep) -> JSONResponse:
    if not authn.is_logged_in(request):
        return JSONResponse({"status": "idle", "error": "unauthorized"}, status_code=401)
    state = read_org_import_state(store.root)
    return JSONResponse(state if state is not None else {"status": "idle"})
