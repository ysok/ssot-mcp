"""Background semantic (embedding) indexing — one repo at a time per process."""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from ssot_mcp.core.store import Store

_task_q: queue.Queue[tuple[Path, str]] = queue.Queue()
_active_ids: set[str] = set()
_active_lock = threading.Lock()
_worker_started = False
_worker_lock = threading.Lock()


def enqueue_semantic_index(data_root: Path, repo_id: str) -> None:
    """Queue LanceDB embedding work for one repo (deduped while queued or running)."""
    with _active_lock:
        if repo_id in _active_ids:
            return
        _active_ids.add(repo_id)
    _task_q.put((data_root.resolve(), repo_id))
    _ensure_worker()


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker_loop, daemon=True, name="ssot-semantic-queue").start()


def _worker_loop() -> None:
    while True:
        data_root, repo_id = _task_q.get()
        try:
            _run_semantic_job(data_root, repo_id)
        finally:
            _task_q.task_done()


def _run_semantic_job(data_root: Path, repo_id: str) -> None:
    store = Store(data_root)
    store.init_db()
    with store.connect() as conn:
        row = conn.execute("SELECT mirror_path FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if not row:
        with _active_lock:
            _active_ids.discard(repo_id)
        return
    mirror = Path(str(row[0]))
    store.begin_semantic_indexing(repo_id)
    try:
        from ssot_mcp.embeddings.semantic import reindex_repository_semantic

        msg = reindex_repository_semantic(store, repo_id, mirror)
        if msg is None:
            store.set_semantic_status(repo_id, "skipped", None)
            store.set_semantic_chunk_count(repo_id, 0)
        else:
            store.set_semantic_status(repo_id, "ready", None)
    except Exception as e:
        store.set_semantic_status(repo_id, "failed", str(e)[:2000])
        store.set_semantic_chunk_count(repo_id, 0)
    finally:
        with _active_lock:
            _active_ids.discard(repo_id)
