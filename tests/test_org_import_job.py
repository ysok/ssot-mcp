"""Background org import job (UI)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ssot_mcp.core.store import Store
from ssot_mcp.ui import org_import_job


def test_start_org_import_rejects_empty_org(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init_db()
    assert org_import_job.start_org_import_job(store, "  ", exclude_forks=False, exclude_archived=False) == "error"


def test_start_org_import_busy_second_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    barrier = threading.Event()
    done = threading.Event()

    def block_worker(*_a, **_kw) -> None:
        barrier.wait(timeout=5)
        done.set()

    monkeypatch.setattr(org_import_job, "_run_org_import_worker", block_worker)
    store = Store(tmp_path)
    store.init_db()
    assert org_import_job.start_org_import_job(store, "acme", exclude_forks=False, exclude_archived=False) == "started"
    try:
        assert org_import_job.start_org_import_job(store, "acme", exclude_forks=False, exclude_archived=False) == "busy"
    finally:
        barrier.set()
    done.wait(timeout=5)
    # allow thread to clear _worker_running
    for _ in range(50):
        if not org_import_job.is_org_import_busy():
            break
        threading.Event().wait(0.05)


def test_run_org_import_worker_api_error_writes_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ssot_mcp.github.github_org import GitHubApiError

    def boom(_org: str, **_kw: object) -> list[str]:
        raise GitHubApiError("org missing")

    monkeypatch.setattr(org_import_job, "list_public_clone_urls", boom)
    org_import_job._write_state(
        tmp_path,
        {
            "status": "starting",
            "org_input": "ghost",
            "exclude_forks": False,
            "exclude_archived": False,
            "total": 0,
            "completed": 0,
            "current": None,
            "added": 0,
            "updated": 0,
            "failed": 0,
            "fatal_error": None,
            "items": [],
            "started_at": "x",
            "finished_at": None,
        },
    )
    org_import_job._run_org_import_worker(tmp_path, "ghost", exclude_forks=False, exclude_archived=False)
    st = org_import_job.read_org_import_state(tmp_path)
    assert st is not None
    assert st["status"] == "error"
    assert "org missing" in st["fatal_error"]
