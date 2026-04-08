"""FastAPI dependencies."""

from __future__ import annotations

import threading
from typing import Annotated

from fastapi import Depends

from ssot_mcp.core.store import Store
from ssot_mcp.embeddings.semantic import backfill_semantic_chunk_counts
from ssot_mcp.ui import config

_backfill_lock = threading.Lock()
_backfilled_roots: set[str] = set()


def get_store() -> Store:
    s = Store(config.data_root())
    s.init_db()
    key = str(s.root.resolve())
    with _backfill_lock:
        if key not in _backfilled_roots:
            try:
                backfill_semantic_chunk_counts(s)
                s.backfill_keyword_fts_file_counts()
            finally:
                _backfilled_roots.add(key)
    return s


StoreDep = Annotated[Store, Depends(get_store)]
