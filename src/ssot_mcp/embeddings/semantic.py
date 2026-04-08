"""Optional semantic (embedding) index — LanceDB + local fastembed (open-source ONNX models, no API keys)."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from ssot_mcp.core.store import Store

_TABLE = "code_chunks"
# Default Lance dataset (fastembed vectors; e.g. 384-d for BAAI/bge-small-en-v1.5).
_DEFAULT_SEMANTIC_LANCE = "semantic.lance"

_local_embedder: tuple[str, object] | None = None


def _lance_table_names(db) -> list[str]:
    resp = db.list_tables()
    return list(getattr(resp, "tables", ()) or ())


def semantic_dependencies_installed() -> bool:
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return False
    return True


def local_embeddings_installed() -> bool:
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return True


def semantic_api_configured() -> bool:
    """True when fastembed is importable (same requirement for indexing and search)."""
    return local_embeddings_installed()


def semantic_indexing_enabled() -> bool:
    v = os.environ.get("SSOT_SEMANTIC_INDEX", "1").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def _lance_path(data_root: Path) -> Path:
    override = (os.environ.get("SSOT_SEMANTIC_LANCE_DIR") or "").strip()
    if override:
        return data_root / override
    return data_root / _DEFAULT_SEMANTIC_LANCE


def _get_local_text_embedding():
    global _local_embedder
    model_name = (os.environ.get("SSOT_LOCAL_EMBEDDING_MODEL") or "BAAI/bge-small-en-v1.5").strip()
    if not local_embeddings_installed():
        raise RuntimeError(
            "Semantic search needs fastembed. Install: pip install 'ssot-mcp[semantic]' "
            "(LanceDB + fastembed; open-weight ONNX models, no cloud API keys)."
        )
    if _local_embedder is None or _local_embedder[0] != model_name:
        from fastembed import TextEmbedding

        _local_embedder = (model_name, TextEmbedding(model_name=model_name))
    return _local_embedder[1]


def _embed_batch(texts: list[str]) -> list[list[float]]:
    model = _get_local_text_embedding()
    out: list[list[float]] = []
    for row in model.embed(texts):
        arr = row if hasattr(row, "tolist") else row
        out.append([float(x) for x in arr])
    return out


def chunk_text(text: str, max_chars: int = 2000, overlap: int = 250) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    i = 0
    while i < len(text):
        end = min(i + max_chars, len(text))
        piece = text[i:end]
        if end < len(text):
            nl = piece.rfind("\n")
            if nl > max_chars // 2:
                piece = piece[:nl]
                end = i + nl + 1
        piece = piece.strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        step = max(len(piece) - overlap, 1)
        i += step
    return chunks


def _batch_size() -> int:
    try:
        return max(1, min(100, int(os.environ.get("SSOT_EMBEDDING_BATCH", "48"))))
    except ValueError:
        return 48


def backfill_semantic_chunk_counts(store: Store) -> None:
    """
    Set repos.semantic_chunk_count from Lance when still NULL (e.g. after DB migration).
    Runs at most once per process per data root (see ui.deps). Uses one count_rows per repo.
    """
    if not semantic_dependencies_installed():
        return

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM repos WHERE semantic_chunk_count IS NULL",
        ).fetchall()
    ids = [str(r[0]) for r in rows]
    if not ids:
        return
    p = _lance_path(store.root)
    if not p.exists():
        for rid in ids:
            store.set_semantic_chunk_count(rid, 0)
        return
    import lancedb

    try:
        db = lancedb.connect(str(p))
        if _TABLE not in _lance_table_names(db):
            for rid in ids:
                store.set_semantic_chunk_count(rid, 0)
            return
        tbl = db.open_table(_TABLE)
    except Exception:
        for rid in ids:
            store.set_semantic_chunk_count(rid, 0)
        return
    for rid in ids:
        esc = rid.replace("'", "''")
        try:
            n = int(tbl.count_rows(f"repo_id = '{esc}'"))
        except Exception:
            n = 0
        store.set_semantic_chunk_count(rid, n)


def delete_repository_vectors(data_root: Path, repo_id: str) -> None:
    if not semantic_dependencies_installed():
        return
    import lancedb

    p = _lance_path(data_root)
    if not p.exists():
        return
    db = lancedb.connect(str(p))
    if _TABLE not in _lance_table_names(db):
        return
    tbl = db.open_table(_TABLE)
    rid = repo_id.replace("'", "''")
    tbl.delete(f"repo_id = '{rid}'")


def reindex_repository_semantic(store: Store, repo_id: str, mirror: Path) -> str | None:
    """
    Build embedding chunks for one mirror. Returns None if skipped, else a short status line.
    Requires: pip install 'ssot-mcp[semantic]', SSOT_SEMANTIC_INDEX enabled, and fastembed available.
    """
    if not semantic_indexing_enabled():
        return None
    if not semantic_api_configured():
        return None
    if not semantic_dependencies_installed():
        return None

    import lancedb

    data_root = store.root
    _lance_path(data_root).parent.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(_lance_path(data_root)))

    tbl = db.open_table(_TABLE) if _TABLE in _lance_table_names(db) else None
    rid_sql = repo_id.replace("'", "''")
    if tbl is not None:
        tbl.delete(f"repo_id = '{rid_sql}'")

    batch_max = _batch_size()
    pending: list[tuple[str, int, str]] = []
    total_chunks = 0
    tbl_holder: list = [tbl]

    def flush() -> None:
        nonlocal total_chunks
        if not pending:
            return
        texts = [t[2] for t in pending]
        vecs = _embed_batch(texts)
        if not vecs:
            return
        records = []
        for (path, idx, chunk), vec in zip(pending, vecs, strict=True):
            records.append(
                {
                    "id": str(uuid.uuid4()),
                    "repo_id": repo_id,
                    "path": path,
                    "chunk_idx": idx,
                    "text": chunk,
                    "vector": vec,
                }
            )
        if tbl_holder[0] is None:
            tbl_holder[0] = db.create_table(_TABLE, data=records)
        else:
            tbl_holder[0].add(records)
        total_chunks += len(records)
        pending.clear()
        time.sleep(float(os.environ.get("SSOT_EMBEDDING_SLEEP_MS", "0") or 0) / 1000.0)

    for path, text in store.iter_mirror_text_files(mirror):
        for idx, piece in enumerate(chunk_text(text)):
            pending.append((path, idx, piece))
            if len(pending) >= batch_max:
                flush()
    flush()

    if total_chunks == 0:
        store.set_semantic_chunk_count(repo_id, 0)
        return f"Semantic index: no chunks for repo `{repo_id}` (empty or unindexed files)."
    store.set_semantic_chunk_count(repo_id, total_chunks)
    return f"Semantic index: stored **{total_chunks}** chunk(s) for repo `{repo_id}`."


def semantic_search(
    store: Store,
    query: str,
    *,
    repo_id: str | None = None,
    top_k: int = 15,
) -> list[dict]:
    """Return dicts: display_name, repo_id, path, score, text (chunk)."""
    if not semantic_dependencies_installed():
        raise RuntimeError(
            "Semantic search requires LanceDB: pip install 'ssot-mcp[semantic]'"
        )

    import numpy as np
    import lancedb

    q = query.strip()
    if not q:
        return []

    p = _lance_path(store.root)
    if not p.exists():
        return []
    db = lancedb.connect(str(p))
    if _TABLE not in _lance_table_names(db):
        return []

    if not semantic_api_configured():
        raise RuntimeError(
            "Semantic search needs fastembed. Install: pip install 'ssot-mcp[semantic]'"
        )

    tbl = db.open_table(_TABLE)
    qvec = np.array(_embed_batch([q])[0], dtype=np.float32)
    search = tbl.search(qvec).limit(int(top_k))

    if repo_id:
        rid = repo_id.replace("'", "''")
        search = search.where(f"repo_id = '{rid}'", prefilter=True)

    raw = search.to_list()
    display_cache: dict[str, str] = {}

    def display_name_for(rid: str) -> str:
        if rid in display_cache:
            return display_cache[rid]
        with store.connect() as conn:
            row = conn.execute("SELECT display_name FROM repos WHERE id = ?", (rid,)).fetchone()
        name = str(row[0]) if row else rid
        display_cache[rid] = name
        return name

    out = []
    for row in raw:
        rid = row.get("repo_id") or ""
        out.append(
            {
                "display_name": display_name_for(rid),
                "repo_id": rid,
                "path": row.get("path") or "",
                "distance": float(row.get("_distance", row.get("score", 0.0))),
                "text": (row.get("text") or "")[:2000],
            }
        )
    return out


def semantic_search_formatted(store: Store, query: str, *, repo_id: str | None = None, top_k: int = 15) -> str:
    try:
        hits = semantic_search(store, query, repo_id=repo_id, top_k=top_k)
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Semantic search error: {e}"
    if not hits:
        return (
            "No semantic matches (install pip install 'ssot-mcp[semantic]', index repos on add/sync, "
            "or try keyword search_code)."
        )
    lines = []
    for h in hits:
        snip = " ".join(h["text"].split())[:600]
        lines.append(
            f"- **{h['display_name']}** `{h['path']}`  (_distance={h['distance']:.4f}, lower is closer)\n"
            f"  repo_id=`{h['repo_id']}`\n  {snip}"
        )
    return "\n\n".join(lines)
