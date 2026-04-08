"""SQLite registry + FTS5 code index."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

SKIP_DIR_NAMES = {
    ".git",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    "build",
    "dist",
    ".idea",
    ".vscode",
}

MAX_INDEX_BYTES = 256_000

_TEXTISH = re.compile(
    r"\.(py|pyi|toml|yaml|yml|json|md|rst|txt|sh|bash|zsh|"
    r"rs|go|java|kt|kts|c|h|cc|cpp|hpp|cs|rb|php|swift|scala|"
    r"js|jsx|ts|tsx|mjs|cjs|css|scss|html|htm|vue|svelte|"
    r"sql|graphql|dockerfile|containerfile|makefile|mk|cmake|gradle|"
    r"xml|plist|ini|cfg|conf|properties|env|gitignore|editorconfig)$",
    re.I,
)


def _is_probably_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\0" in sample[:8192]:
        return True
    return False


def _should_index_file(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        if _TEXTISH.search(name) or name in {".gitignore", ".dockerignore", ".env.example"}:
            return True
        return False
    return bool(_TEXTISH.search(name))


def _ensure_repos_semantic_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(repos)").fetchall()}
    if "semantic_status" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN semantic_status TEXT")
    if "semantic_error" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN semantic_error TEXT")
    if "semantic_chunk_count" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN semantic_chunk_count INTEGER")
    if "keyword_fts_file_count" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN keyword_fts_file_count INTEGER")
    if "semantic_indexing_started_at" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN semantic_indexing_started_at TEXT")
    if "semantic_indexing_heartbeat_at" not in cols:
        conn.execute("ALTER TABLE repos ADD COLUMN semantic_indexing_heartbeat_at TEXT")


def _semantic_indexing_stale_seconds() -> float:
    raw = (os.environ.get("SSOT_SEMANTIC_INDEXING_STALE_SEC") or "1800").strip()
    try:
        v = float(raw)
    except ValueError:
        v = 1800.0
    return max(60.0, min(v, 86400.0))


def _parse_iso_utc(s: str | None) -> datetime | None:
    if not s or not str(s).strip():
        return None
    try:
        t = str(s).strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _semantic_indexing_timestamps_stale(
    heartbeat_at: str | None,
    started_at: str | None,
    *,
    now: datetime,
    stale_sec: float,
) -> bool:
    """True if indexing appears dead (no recent heartbeat / start)."""
    hb_dt = _parse_iso_utc(heartbeat_at)
    st_dt = _parse_iso_utc(started_at)
    ref = hb_dt or st_dt
    if ref is None:
        return True
    return (now - ref).total_seconds() > stale_sec


_STALE_INDEXING_MSG = (
    "Indexing stalled or process stopped (no heartbeat). Use retry to re-queue."
)


def semantic_indexing_activity_hint(
    heartbeat_at: str | None,
    started_at: str | None,
) -> str:
    """Short UI tooltip: how recently the embedding worker reported progress."""
    now = datetime.now(UTC)
    hb = _parse_iso_utc(heartbeat_at)
    st = _parse_iso_utc(started_at)
    ref = hb or st
    if ref is None:
        return "Building embedding chunks."
    sec = max(0.0, (now - ref).total_seconds())
    if sec < 8:
        return "Building embedding chunks (active; heartbeat just now)."
    if sec < 120:
        return f"Building embedding chunks (last activity {int(sec)}s ago)."
    if sec < 3600:
        return f"Building embedding chunks (last activity {int(sec // 60)}m ago)."
    return f"Building embedding chunks (last activity {int(sec // 3600)}h ago)."


def _semantic_heartbeat_file_interval() -> int:
    try:
        v = int(os.environ.get("SSOT_SEMANTIC_HEARTBEAT_FILE_INTERVAL", "25"))
    except ValueError:
        v = 25
    return max(1, min(v, 500))


def _sqlite_busy_timeout_sec() -> float:
    raw = (os.environ.get("SSOT_SQLITE_BUSY_TIMEOUT") or "30").strip()
    try:
        t = float(raw)
    except ValueError:
        t = 30.0
    return max(1.0, min(t, 600.0))


def _configure_sqlite_connection(conn: sqlite3.Connection, *, timeout_sec: float) -> None:
    """WAL + busy wait: reduces 'database is locked' under concurrent HTTP + indexing."""
    ms = int(min(timeout_sec * 1000, 2_147_483_647))
    conn.execute(f"PRAGMA busy_timeout = {ms}")
    wal_on = (os.environ.get("SSOT_SQLITE_WAL") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    if wal_on:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass


class Store:
    def __init__(self, data_root: Path) -> None:
        self.root = data_root
        self.mirrors = data_root / "mirrors"
        self.db_path = data_root / "ssot.db"

    def connect(self) -> sqlite3.Connection:
        """Open a connection with lock timeout and WAL (see SSOT_SQLITE_BUSY_TIMEOUT, SSOT_SQLITE_WAL)."""
        t = _sqlite_busy_timeout_sec()
        conn = sqlite3.connect(str(self.db_path), timeout=t)
        _configure_sqlite_connection(conn, timeout_sec=t)
        return conn

    def init_db(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.mirrors.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS repos (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    mirror_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            _ensure_repos_semantic_columns(conn)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
                    repo_id UNINDEXED,
                    path UNINDEXED,
                    content,
                    tokenize = 'porter unicode61'
                )
            """)
            conn.commit()

    def set_semantic_status(self, repo_id: str, status: str | None, error: str | None) -> None:
        if status == "indexing":
            self.begin_semantic_indexing(repo_id)
            return
        self.init_db()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE repos SET semantic_status = ?, semantic_error = ?,
                semantic_indexing_started_at = NULL, semantic_indexing_heartbeat_at = NULL
                WHERE id = ?
                """,
                (status, error, repo_id),
            )
            conn.commit()

    def begin_semantic_indexing(self, repo_id: str) -> None:
        """Mark repo as indexing and set start + heartbeat (call when worker actually starts the job)."""
        self.init_db()
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE repos SET semantic_status = 'indexing', semantic_error = NULL,
                semantic_indexing_started_at = ?, semantic_indexing_heartbeat_at = ?
                WHERE id = ?
                """,
                (now, now, repo_id),
            )
            conn.commit()

    def touch_semantic_indexing_heartbeat(self, repo_id: str) -> None:
        """Refresh heartbeat while embedding (proves worker is alive)."""
        self.init_db()
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE repos SET semantic_indexing_heartbeat_at = ?
                WHERE id = ? AND semantic_status = 'indexing'
                """,
                (now, repo_id),
            )
            conn.commit()

    def reconcile_stale_semantic_indexing(self) -> int:
        """
        Mark indexing repos as failed when heartbeat is too old (crashed worker / stuck UI).
        Returns number of rows updated.
        """
        self.init_db()
        stale_sec = _semantic_indexing_stale_seconds()
        now = datetime.now(UTC)
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, semantic_indexing_heartbeat_at, semantic_indexing_started_at
                FROM repos WHERE semantic_status = 'indexing'
                """,
            ).fetchall()
        n = 0
        for row in rows:
            rid = str(row["id"])
            if not _semantic_indexing_timestamps_stale(
                row["semantic_indexing_heartbeat_at"],
                row["semantic_indexing_started_at"],
                now=now,
                stale_sec=stale_sec,
            ):
                continue
            self.set_semantic_status(rid, "failed", _STALE_INDEXING_MSG)
            self.set_semantic_chunk_count(rid, 0)
            n += 1
        return n

    def set_semantic_chunk_count(self, repo_id: str, n: int) -> None:
        """Persist LanceDB chunk count for UI (updated when indexing completes; reset when re-queued)."""
        self.init_db()
        with self.connect() as conn:
            conn.execute(
                "UPDATE repos SET semantic_chunk_count = ? WHERE id = ?",
                (int(n), repo_id),
            )
            conn.commit()

    def get_repo(self, conn: sqlite3.Connection, repo_id: str) -> tuple[str, str, str] | None:
        row = conn.execute(
            "SELECT id, url, mirror_path FROM repos WHERE id = ?",
            (repo_id,),
        ).fetchone()
        if not row:
            return None
        return str(row[0]), str(row[1]), str(row[2])

    def list_repos(self) -> list[dict[str, str]]:
        self.reconcile_stale_semantic_indexing()
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, url, display_name, mirror_path, created_at, updated_at,
                       semantic_status, semantic_error, semantic_chunk_count,
                       keyword_fts_file_count,
                       semantic_indexing_started_at, semantic_indexing_heartbeat_at
                FROM repos ORDER BY display_name
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def count_repos(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0])

    def list_repos_page(self, page: int, per_page: int = 50) -> tuple[list[dict[str, str]], int]:
        """Return (rows for page, total count)."""
        page = max(1, page)
        per_page = max(1, min(int(per_page), 100))
        offset = (page - 1) * per_page
        self.reconcile_stale_semantic_indexing()
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            total = int(conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0])
            rows = conn.execute(
                """
                SELECT id, url, display_name, mirror_path, created_at, updated_at,
                       semantic_status, semantic_error, semantic_chunk_count,
                       keyword_fts_file_count,
                       semantic_indexing_started_at, semantic_indexing_heartbeat_at
                FROM repos
                ORDER BY display_name COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                (per_page, offset),
            ).fetchall()
        return [dict(r) for r in rows], total

    def fts_file_counts_for_repo_ids(self, repo_ids: list[str]) -> dict[str, int]:
        """Number of FTS-indexed files per repo (cached in repos.keyword_fts_file_count)."""
        if not repo_ids:
            return {}
        self.init_db()
        placeholders = ",".join("?" * len(repo_ids))
        out: dict[str, int] = {rid: 0 for rid in repo_ids}
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, COALESCE(keyword_fts_file_count, 0)
                FROM repos WHERE id IN ({placeholders})
                """,
                repo_ids,
            ).fetchall()
        for rid, n in rows:
            out[str(rid)] = int(n)
        return out

    def backfill_keyword_fts_file_counts(self) -> None:
        """
        One-time migration: set keyword_fts_file_count from code_fts for rows where it is NULL.
        Avoids full-table FTS scans on every repo list (see index_mirror for ongoing updates).
        """
        self.init_db()
        with self.connect() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM repos WHERE keyword_fts_file_count IS NULL LIMIT 1",
                ).fetchone()
                is None
            ):
                return
            conn.execute("UPDATE repos SET keyword_fts_file_count = 0")
            rows = conn.execute(
                "SELECT repo_id, COUNT(*) FROM code_fts GROUP BY repo_id",
            ).fetchall()
            for rid, n in rows:
                conn.execute(
                    "UPDATE repos SET keyword_fts_file_count = ? WHERE id = ?",
                    (int(n), str(rid)),
                )
            conn.commit()

    def get_repo_detail(self, repo_id: str) -> dict[str, str] | None:
        self.reconcile_stale_semantic_indexing()
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, url, display_name, mirror_path, created_at, updated_at,
                       semantic_status, semantic_error, semantic_chunk_count,
                       keyword_fts_file_count,
                       semantic_indexing_started_at, semantic_indexing_heartbeat_at
                FROM repos WHERE id = ?
                """,
                (repo_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_repo_display_name(self, repo_id: str, display_name: str) -> bool:
        name = display_name.strip()
        if not name:
            return False
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE repos SET display_name = ?, updated_at = ? WHERE id = ?",
                (name, now, repo_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def add_repo(self, url: str, display_name: str, mirror_path: Path) -> str:
        repo_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO repos (
                    id, url, display_name, mirror_path, created_at, updated_at,
                    keyword_fts_file_count
                ) VALUES (?,?,?,?,?,?,0)
                """,
                (repo_id, url, display_name, str(mirror_path), now, now),
            )
            conn.commit()
        return repo_id

    def touch_repo(self, repo_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute("UPDATE repos SET updated_at = ? WHERE id = ?", (now, repo_id))
            conn.commit()

    def delete_repo_record(self, repo_id: str) -> tuple[str, Path] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT url, mirror_path FROM repos WHERE id = ?",
                (repo_id,),
            ).fetchone()
            if not row:
                return None
            url, mpath = str(row[0]), Path(row[1])
            conn.execute("DELETE FROM code_fts WHERE repo_id = ?", (repo_id,))
            conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
            conn.commit()
            return url, mpath

    def clear_fts_for_repo(self, repo_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM code_fts WHERE repo_id = ?", (repo_id,))
            conn.execute(
                "UPDATE repos SET keyword_fts_file_count = 0 WHERE id = ?",
                (repo_id,),
            )
            conn.commit()

    def iter_mirror_text_files(self, mirror: Path):
        """Yield (relative_path_posix, text) for each indexable file under mirror."""
        mirror = mirror.resolve()
        for p in mirror.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(mirror)
            except ValueError:
                continue
            parts = set(rel.parts)
            if parts & SKIP_DIR_NAMES:
                continue
            if any(part in SKIP_DIR_NAMES for part in rel.parts):
                continue
            if not _should_index_file(p):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > MAX_INDEX_BYTES:
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            if _is_probably_binary(raw):
                continue
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            yield str(rel).replace("\\", "/"), text

    def index_mirror(self, repo_id: str, mirror: Path) -> int:
        """Walk mirror and insert into FTS. Returns number of files indexed."""
        self.init_db()
        count = 0
        with self.connect() as conn:
            conn.execute("DELETE FROM code_fts WHERE repo_id = ?", (repo_id,))
            for rel, text in self.iter_mirror_text_files(mirror):
                conn.execute(
                    "INSERT INTO code_fts (repo_id, path, content) VALUES (?,?,?)",
                    (repo_id, rel, text),
                )
                count += 1
            conn.execute(
                "UPDATE repos SET keyword_fts_file_count = ? WHERE id = ?",
                (count, repo_id),
            )
            conn.commit()
        return count

    def search(self, query: str, repo_id: str | None = None, limit: int = 30) -> list[dict[str, str]]:
        q = query.strip()
        if not q:
            return []
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            try:
                if repo_id:
                    rows = conn.execute(
                        """
                        SELECT r.display_name, f.repo_id, f.path,
                               snippet(code_fts, 2, '[', ']', '…', 32) AS snippet
                        FROM code_fts f
                        JOIN repos r ON r.id = f.repo_id
                        WHERE code_fts MATCH ? AND f.repo_id = ?
                        LIMIT ?
                        """,
                        (q, repo_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT r.display_name, f.repo_id, f.path,
                               snippet(code_fts, 2, '[', ']', '…', 32) AS snippet
                        FROM code_fts f
                        JOIN repos r ON r.id = f.repo_id
                        WHERE code_fts MATCH ?
                        LIMIT ?
                        """,
                        (q, limit),
                    ).fetchall()
            except sqlite3.OperationalError:
                return []
            return [dict(r) for r in rows]

    def remove_mirror_dir(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            resolved.relative_to(self.mirrors.resolve())
        except ValueError:
            return
        if resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
