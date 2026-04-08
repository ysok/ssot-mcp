# ssot-mcp — Application specification

This document describes **ssot-mcp** (“Single Source of Truth — MCP”) as implemented in this repository: features, behavior, data flow, UI, background work, indexing, and testing. It is intended as a **functional spec** for reimplementation or porting to another stack.

---

## 1. Purpose and scope

### 1.1 What the product does

- **Mirror** many Git remotes (HTTPS/SSH) into a single **data directory** using **shallow clones** (`git clone --depth 1`).
- **Register** each mirror in **SQLite** with a stable **repository id** (UUID), URL, display name, paths, and timestamps.
- **Keyword-index** text-like source files into **SQLite FTS5** for full-text search across one or all repos.
- **Optionally** build a **semantic (embedding) index**: chunk text, embed with **fastembed** (local ONNX, no cloud API keys), store vectors in **LanceDB** for natural-language / cross-repo search.
- **GitHub organizations**: list **public** repositories via the **GitHub API**; **add** (clone + FTS index) or **sync** (fetch + re-index) each URL; optionally queue semantic indexing in bulk.

### 1.2 User-facing surfaces

| Surface | Role |
|--------|------|
| **MCP server** (FastMCP) | Tools for IDEs (e.g. Cursor): add/remove/sync repos, org import, search, read files. |
| **CLI** (`ssot-mcp`) | Same operations from the shell; shares DB and mirrors with MCP. |
| **Admin web UI** (`ssot-mcp-ui`) | Browser UI for repos, org import with progress, GitHub token settings, help. |

### 1.3 Explicit out of scope (current code)

- GitLab (or non-GitHub) org import as a first-class feature.
- First-class “local path” remotes (workarounds: bare remote or `file://` where Git allows).

---

## 2. High-level architecture

```text
                    ┌─────────────────┐
                    │  SSOT_DATA_DIR  │
                    │  ssot.db (SQL)  │
                    │  mirrors/       │
                    │  semantic.lance │
                    └────────┬────────┘
                             │
     ┌───────────────────────┼───────────────────────┐
     │                       │                       │
┌────▼────┐            ┌─────▼─────┐          ┌─────▼─────┐
│   MCP   │            │    CLI    │          │  Web UI   │
│ server  │            │  ssot-mcp │          │ FastAPI   │
└────┬────┘            └─────┬─────┘          └─────┬─────┘
     │                       │                       │
     └───────────────────────┼───────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  services/repos │
                    │  core/store     │
                    │  git/git_ops    │
                    │  github/*       │
                    │  embeddings/*   │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
        ┌─────▼─────┐ ┌──────▼──────┐ ┌─────▼─────┐
        │ git clone │ │ GitHub API  │ │ Thread    │
        │ / fetch   │ │ list repos  │ │ workers   │
        └───────────┘ └─────────────┘ └───────────┘
```

- **Single source of truth** for registry and keyword index: **`ssot.db`**.
- **Mirrors** live under **`mirrors/<slug>/`** (slug derived from URL).
- **Semantic vectors** live under **`semantic.lance/`** (or env override path) as a LanceDB dataset.

---

## 3. Data directory layout

| Path | Purpose |
|------|---------|
| `SSOT_DATA_DIR/ssot.db` | SQLite: `repos` table, FTS5 `code_fts`, migrations / denormalized counters. |
| `SSOT_DATA_DIR/ssot.db-wal`, `-shm` | WAL journal files when WAL mode is enabled (normal after first open). |
| `SSOT_DATA_DIR/mirrors/<slug>/` | Shallow git working trees. |
| `SSOT_DATA_DIR/semantic.lance/` | LanceDB tables for embedding chunks (default). |
| `SSOT_DATA_DIR/.github_token` | Optional saved PAT for org API (mode `0600`); env vars override. |
| `SSOT_DATA_DIR/.ssot_ui/org_import.json` | Org import job state for the UI (JSON file, atomically replaced). |

---

## 4. SQLite data model

### 4.1 Table `repos`

Core columns (created at init):

- `id` (TEXT PK, UUID)
- `url` (TEXT, UNIQUE)
- `display_name` (TEXT)
- `mirror_path` (TEXT)
- `created_at`, `updated_at` (TEXT, ISO timestamps)

Migrated / additive columns:

- `semantic_status` (TEXT): e.g. `pending`, `indexing`, `ready`, `failed`, `skipped`, or NULL (legacy).
- `semantic_error` (TEXT): short failure message when `failed`.
- `semantic_chunk_count` (INTEGER): cached LanceDB chunk count for UI/list performance; updated when indexing finishes; reset when re-queued / failure paths as implemented.
- `keyword_fts_file_count` (INTEGER): cached count of FTS rows (indexed files) per repo; updated on `index_mirror`; avoids full FTS scans on repo list.

### 4.2 FTS5 virtual table `code_fts`

- Columns: `repo_id` (UNINDEXED), `path` (UNINDEXED), `content` (indexed with `porter unicode61`).
- One row per indexed **file** (content truncated per `MAX_INDEX_BYTES` in code, default 256 KB).
- File types and skip rules: see `store.py` (`SKIP_DIR_NAMES`, `_TEXTISH`, binary heuristic).

### 4.3 Database access pattern

- All application code should open SQLite via **`Store.connect()`** (not raw `sqlite3.connect` to path only):
  - Configurable **busy timeout** (`SSOT_SQLITE_BUSY_TIMEOUT`, seconds).
  - **`PRAGMA busy_timeout`** (milliseconds) aligned with that.
  - **`PRAGMA journal_mode=WAL`** by default (`SSOT_SQLITE_WAL=0` to disable for unusual filesystems).
  - **`PRAGMA synchronous=NORMAL`** with WAL.

### 4.4 First-request maintenance (web UI dependency injection)

When the UI resolves `Store` via `get_store()`:

1. `init_db()` ensures schema.
2. **Once per process per data root** (guarded by a lock and set):
   - **`backfill_semantic_chunk_counts`**: if any `semantic_chunk_count` is NULL, fill from LanceDB (per-repo `count_rows`) or zero.
   - **`backfill_keyword_fts_file_counts`**: if any `keyword_fts_file_count` is NULL, one `GROUP BY` over `code_fts` then update `repos`.

New repos insert `keyword_fts_file_count = 0` so routine use does not retrigger the FTS backfill.

---

## 5. Keyword (FTS) indexing

### 5.1 When it runs

- **`add_repository`**: after successful clone, **`index_mirror(repo_id, mirror_path)`**.
- **`sync_repository`**: after successful `git fetch` (and related git steps), **`index_mirror`** again.
- **`add_or_sync_repository`**: used by org import; same indexing rules.
- **`remove_repository`**: deletes `code_fts` rows for that `repo_id` and removes the `repos` row; deletes mirror dir and Lance vectors (best effort).

### 5.2 What `index_mirror` does

1. Single transaction: `DELETE FROM code_fts WHERE repo_id = ?`.
2. Walk mirror with **`iter_mirror_text_files`**: skip dotfiles (except allowlisted names), skip `node_modules`, `.git`, build dirs, etc.; cap file size; skip binary heuristic.
3. `INSERT` each file’s relative path and text into `code_fts`.
4. `UPDATE repos SET keyword_fts_file_count = ?` to the number of files indexed.

### 5.3 Search

- **`store.search(query, repo_id?, limit)`**: FTS5 `MATCH` with optional `repo_id` filter; returns display name, path, snippet.
- **`search_formatted`** (services layer): markdown-style lines for MCP/CLI.

---

## 6. Semantic (embedding) indexing and search

### 6.1 Dependencies and toggles

- Optional install: **`[semantic]`** → LanceDB + fastembed.
- **`semantic_indexing_enabled()`**: `SSOT_SEMANTIC_INDEX` default on; `0`/`false`/`off` disables indexing (and treats as “skipped” where applicable).
- **`semantic_api_configured()`**: fastembed importable.
- Lance path: default `data_root / "semantic.lance"`; override with **`SSOT_SEMANTIC_LANCE_DIR`** (relative to data root).

### 6.2 Chunking and embedding

- Text split with **`chunk_text`**: max chars 2000, overlap 250, prefers breaking at newlines.
- Batches embedded via **`_embed_batch`**; batch size from **`SSOT_EMBEDDING_BATCH`** (default 48, clamped).
- Optional throttle: **`SSOT_EMBEDDING_SLEEP_MS`** between batch flushes in `reindex_repository_semantic`.
- Model: **`SSOT_LOCAL_EMBEDDING_MODEL`** or default **`BAAI/bge-small-en-v1.5`**.

### 6.3 `reindex_repository_semantic(store, repo_id, mirror)`

1. No-op (returns `None`) if semantic disabled, fastembed missing, or LanceDB missing.
2. Connect LanceDB; **delete** existing rows for `repo_id` in table **`code_chunks`**.
3. Stream same text files as FTS (via `iter_mirror_text_files`), chunk, embed, **`add`** or **`create_table`** records with: `id`, `repo_id`, `path`, `chunk_idx`, `text`, `vector`.
4. **`store.set_semantic_chunk_count(repo_id, total_chunks)`** (including 0 for “no chunks” message path).

### 6.4 Background queue (`embeddings/semantic_queue.py`)

- **`enqueue_semantic_index(data_root, repo_id)`**: thread-safe dedupe (`_active_ids`); pushes `(data_root, repo_id)` on a **`queue.Queue`**; starts daemon thread **`ssot-semantic-queue`** once.
- Worker loop:
  1. `Store(data_root).init_db()`
  2. Load `mirror_path` from DB; if missing, release id and return.
  3. **`set_semantic_status(repo_id, "indexing", None)`**
  4. Call **`reindex_repository_semantic`**
  5. If return is `None`: **`skipped`** + chunk count `0`
  6. Else: **`ready`** (chunk count already set inside reindex)
  7. On exception: **`failed`** + error text truncated + chunk count `0`
  8. Always remove `repo_id` from `_active_ids` in `finally`.

**Ordering**: one repo job at a time per process (single worker loop); queue can hold multiple pending ids.

### 6.5 When semantic work is scheduled

- **`_schedule_semantic_after_fts`**: if features wanted → `semantic_status = pending`, **`semantic_chunk_count = 0`**, then **`enqueue_semantic_index`** unless `defer_enqueue`.
- **`add_repository`**: calls `_schedule_semantic_after_fts` with `defer_semantic` flag (org batch uses defer).
- **`sync_repository`**: same.
- **`retry_semantic_indexing`**: if features wanted → pending + chunk `0` + enqueue; if not → skipped + chunk `0`.
- **`enqueue_semantic_for_repo_urls`**: for each URL in order, lookup `id`, enqueue semantic (used after UI org import completes).

### 6.6 Semantic search

- **`semantic_search` / `semantic_search_formatted`**: embed query, LanceDB vector search, optional `repo_id` prefilter, join display names from SQLite, return ranked chunks with distance and text snippet.

### 6.7 Vector deletion

- **`delete_repository_vectors`**: Lance delete by `repo_id` when removing a repo.

### 6.8 Backfill

- **`backfill_semantic_chunk_counts`**: used on UI first store resolution to populate NULL counts from Lance (or zero if no Lance/table).

---

## 7. Git operations (`git/git_ops.py`)

- **`clone`**: `git clone --depth 1` into destination; timeout **`SSOT_GIT_CLONE_TIMEOUT`** (default 600 s, clamped 60–86400).
- **`sync`**: `git fetch --depth 1`, `remote set-head`, then reset hard to `origin/HEAD` (see full file for exact sequence).
- **`slug_from_url`**: derives mirror directory name under `mirrors/`; collision handling in `add_repository` appends numeric suffix.
- **`display_name_for_url`**: human-readable default display name.

---

## 8. GitHub organization import

### 8.1 API layer (`github/github_org.py`)

- **`parse_github_org`**: accepts org URL or bare org login.
- **`list_public_clone_urls`**: paginated GitHub API; filters forks/archived per flags; uses **`effective_github_token`** from `github/credentials.py` (env first, then file **`SSOT_DATA_DIR/.github_token`**).

### 8.2 Synchronous import (`services/repos.add_github_organization`)

- Lists URLs, loops **`add_or_sync_repository(..., defer_semantic=True)`** per URL, aggregates **`OrgImportSummary`** (added/updated/failed counts and markdown message).
- Then **`enqueue_semantic_for_repo_urls`** for all successful URLs.

### 8.3 UI background import (`ui/org_import_job.py`)

- **State file**: `SSOT_DATA_DIR/.ssot_ui/org_import.json` (write via temp file + `replace`).
- **`start_org_import_job`**: if worker already running → `"busy"`; else sets `_worker_running`, writes initial JSON (`status: starting`), starts daemon thread **`ssot-org-import`** → returns `"started"`.
- Worker phases:
  1. **listing**: GitHub API list URLs (or fatal `GitHubApiError` → `status: error`, `fatal_error`).
  2. **running**: for each repo, update `current`, per-item `pending` → `running` → `added` / `updated` / `fail` with detail line; counters `added`, `updated`, `failed`, `completed`.
  3. **complete**: set `current` to queuing message, **`enqueue_semantic_for_repo_urls`**, then `status: complete`, `finished_at`.

- **`read_org_import_state`**: used by HTML progress page and **`GET /import-org/progress.json`** (JSON for polling).

---

## 9. MCP server (`mcp/server.py`)

### 9.1 Process singleton `Store`

- **`get_store()`**: lazy global `Store` for `SSOT_DATA_DIR`; **`init_db()`** on first use (note: MCP path does **not** run UI backfills unless code is shared elsewhere).

### 9.2 FastMCP instance

- Name **`ssot-mcp`**; host/port from **`FASTMCP_HOST`**, **`FASTMCP_PORT`** (default 8765).
- **`stateless_http=True`**, **`json_response=True`**.
- Transport: **`SSOT_MCP_TRANSPORT`**: `streamable-http` (default), `stdio`, or `sse`.

### 9.3 Tools (exact names and intent)

| Tool | Behavior |
|------|----------|
| **`list_repositories`** | `list_formatted` — markdown list of ids, urls, mirror paths, timestamps. |
| **`add_repository`** | Clone + FTS + schedule semantic (not deferred). |
| **`add_github_organization`** | Sync org import path + message string from `OrgImportSummary`. |
| **`remove_repository`** | Delete registry, FTS rows, Lance vectors, mirror directory. |
| **`sync_repository`** | Fetch + re-index FTS + schedule semantic. |
| **`search_code`** | FTS search; optional `repo_id`; limit clamped in tool. |
| **`semantic_search`** | Vector search; optional `repo_id`; `top_k` clamped 1–50. |
| **`read_file`** | Safe path read under mirror; max bytes cap; markdown code fence output. |

---

## 10. CLI (`cli/main.py`)

Subcommands (all respect **`--data-dir`** or **`SSOT_DATA_DIR`**):

- **`list`**
- **`add <url>`**
- **`add-org <org>`** with **`--no-forks`**, **`--no-archived`**
- **`remove <repo_id>`**
- **`sync <repo_id>`**
- **`search <query>`** with **`--repo`**, **`--limit`**, **`--plain`**
- **`semantic-search <query>`** with **`--repo`**, **`--top-k`**
- **`cat <repo_id> <path>`** with **`--max-bytes`**

Exit codes: failures return non-zero; org import uses **`_org_import_exit`** on summary.

---

## 11. Web UI — overview

### 11.1 Stack

- **FastAPI** app factory **`create_app()`** (`ui/app.py`).
- **SessionMiddleware** (Starlette): cookie **`ssot_ui_session`**, secret **`SSOT_UI_SECRET`** (or insecure dev default with random suffix).
- Static files under **`/static`** (CSS, PNG, etc.).
- **Jinja2** templates in `ui/templates/`.
- **No OpenAPI docs** in UI app (`docs_url=None`).

### 11.2 Entry and process model

- **`ssot-mcp-ui`**: Uvicorn, host **`SSOT_UI_HOST`**, port **`SSOT_UI_PORT`** (default **8081**).
- **Container** (`runtime/launcher.py`): spawns **two** subprocesses — **`python -m ssot_mcp.mcp.server`** and **`python -m ssot_mcp.ui.main`**; if either exits, both are terminated.

### 11.3 Authentication (`ui/authn.py`, `ui/routers/auth_routes.py`)

- **Single user** (or single credential pair): **`SSOT_UI_USER`**, **`SSOT_UI_PASSWORD`** (compared with **`secrets.compare_digest`**).
- Session flag **`ssot_ui_authenticated`** when logged in.
- **`require_login`**: 303 redirect to **`/login`** if missing.
- **Flash messages**: one-shot **`set_flash` / `pop_flash`** with **`kind`**: `success`, `error`, `warning`, `info` (shown in `base.html` with `aria-live`).

Routes:

- **`GET /login`**: form; if already logged in → redirect `/repos`.
- **`POST /login`**: form fields `username`, `password`; success → 303 `/repos`; failure → 401 + error message.
- **`POST /logout`**: clear session, flash “signed out”, 303 `/login`.

### 11.4 Repositories (`ui/routers/repos_routes.py`)

Protected routes (guard at top of handler):

| Method | Path | Action |
|--------|------|--------|
| GET | `/repos` | Paginated table: name, URL, keyword file count, semantic pill + optional retry, updated, actions. Uses cached columns; no per-request FTS/Lance aggregation. |
| GET | `/repos/new` | Form to add URL. |
| POST | `/repos/new` | `add_repository`; flash + redirect `/repos` or `/repos/new` on error. |
| GET | `/repos/{id}/edit` | Edit display name form. |
| POST | `/repos/{id}/edit` | Update display name. |
| POST | `/repos/{id}/delete` | `remove_repository`. |
| POST | `/repos/{id}/semantic-retry` | `retry_semantic_indexing`. |
| GET | `/import-org` | Org import form (org name, exclude forks/archived checkboxes). |
| POST | `/import-org` | `start_org_import_job`; flash + redirect progress or back to form if busy/error. |
| GET | `/import-org/progress` | HTML progress from `read_org_import_state`. |
| GET | `/import-org/progress.json` | JSON state; **401** if not logged in. |

**Semantic column UI logic** (`_semantic_ui_row`): combines **`semantic_status`**, **`semantic_error`**, cached **`semantic_chunk_count`** (as `sem_map` when Lance deps installed), and drives template “kind”: `na`, `skipped`, `pending`, `indexing`, `failed`, `ready`, `legacy_zero`. Status strings are **normalized** (strip whitespace).

**Live updates**: template sets **`data-semantic-status`** on semantic cells; if any row is `pending` or `indexing`, a script in **`extra_body`** reloads the page every **4s** when the tab is **visible**, and shows a hint line.

**Root `GET /`**: 302 → `/repos`.

### 11.5 Settings (`ui/routers/settings_routes.py`)

- **`GET /settings/github`**: shows whether token comes from **`env`**, **`file`**, or **`none`** (no secret displayed).
- **`POST /settings/github`**: save new token to **`.github_token`** or remove file if “clear” checked; flash result.

### 11.6 Help (`ui/routers/help_routes.py`)

- **`GET /help/cursor-mcp`**: static instructions; **`mcp_url`** derived from **`FASTMCP_HOST`/`FASTMCP_PORT`** (maps `0.0.0.0` to `127.0.0.1` for display).

### 11.7 Navigation (`base.html`)

When `show_nav`: Repositories, Add, Import org, GitHub token, Cursor MCP, Sign out (POST logout).

### 11.8 UI configuration summary (`ui/config.py`)

| Variable | Role |
|----------|------|
| `SSOT_DATA_DIR` | Data root (default `/data`). |
| `SSOT_UI_SECRET` | Session signing secret. |
| `SSOT_UI_USER` / `SSOT_UI_PASSWORD` | Basic admin credentials. |
| `SSOT_UI_PAGE_SIZE` | Repo list pagination (1–100, default 50). |
| `SSOT_UI_PORT` | Listen port (default 8081). |

---

## 12. Background jobs summary

| Job | Trigger | Mechanism | Shared state |
|-----|---------|-----------|--------------|
| **Semantic indexing** | add/sync/retry/org batch enqueue | Daemon thread + `queue.Queue`; one job at a time | SQLite `semantic_*`, LanceDB |
| **Org import (UI)** | POST `/import-org` | Daemon thread; exclusive flag `_worker_running` | `.ssot_ui/org_import.json` |
| **MCP/CLI org import** | Tool / `add-org` | Same-process loop (blocking until done) | SQLite + Lance queue at end |

---

## 13. Environment variables (reference)

| Variable | Area | Notes |
|----------|------|--------|
| `SSOT_DATA_DIR` | All | Root for DB, mirrors, Lance, UI token file, org import state. |
| `FASTMCP_HOST`, `FASTMCP_PORT` | MCP | Default `0.0.0.0:8765`. |
| `SSOT_MCP_TRANSPORT` | MCP | `streamable-http`, `stdio`, `sse`. |
| `GITHUB_TOKEN` or `GITHUB_PERSONAL_ACCESS_TOKEN` | GitHub API | Unauthenticated rate limits otherwise. |
| `SSOT_GIT_CLONE_TIMEOUT`, `SSOT_GIT_SYNC_TIMEOUT` | Git | Seconds, clamped. |
| `SSOT_SEMANTIC_INDEX` | Semantic | Disable with `0`/`false`/`off`. |
| `SSOT_SEMANTIC_LANCE_DIR` | Semantic | Relative Lance dir under data root. |
| `SSOT_LOCAL_EMBEDDING_MODEL` | Semantic | fastembed model id. |
| `SSOT_EMBEDDING_BATCH` | Semantic | Batch size cap. |
| `SSOT_EMBEDDING_SLEEP_MS` | Semantic | Sleep between batch writes. |
| `SSOT_SQLITE_BUSY_TIMEOUT` | SQLite | Seconds (1–600). |
| `SSOT_SQLITE_WAL` | SQLite | `0`/`false`/`off` disables WAL. |
| `SSOT_UI_*` | UI | See §11.8. |

---

## 14. Testing

### 14.1 Layout

Tests live in **`tests/`**; **`pyproject.toml`** sets `testpaths` and `pythonpath = ["src"]`.

### 14.2 Extras

- **`[test]`**: pytest.
- **`[ui]`**: required for **`test_ui_app.py`** (skipped via `importorskip("fastapi")` if missing).
- **`[semantic]`**: Lance + fastembed for semantic tests (some may skip via markers).

### 14.3 Main test files (by concern)

| File | Focus |
|------|--------|
| `test_store.py` | DB schema, FTS indexing, search, denormalized counts, pagination. |
| `test_repos.py` | add/remove/sync, org summary, semantic scheduling, retry, defer semantic. |
| `test_git_ops.py` | Slug, clone/sync behavior (mocked subprocess where used). |
| `test_github_org.py` | Org URL parsing, API error handling. |
| `test_github_credentials.py` | Token resolution order, save/clear file. |
| `test_semantic.py` | Embedding / reindex paths (mocked or optional deps). |
| `test_semantic_queue.py` | Worker success/failure, active id cleanup. |
| `test_semantic_ui_row.py` | Template helper mapping for semantic column. |
| `test_ui_app.py` | HTTP routes, login, repos, pagination, org import wiring, static. |
| `test_ui_authn.py` | Credential comparison edge cases. |
| `test_org_import_job.py` | State machine / worker behavior (with mocks). |
| `test_cli.py` | CLI entry and result printing. |
| `test_server_smoke.py` | MCP module surface smoke check. |

### 14.4 Running

```bash
pip install -e '.[test,ui,semantic]'
pytest
```

On macOS, low `ulimit -n` may require raising file descriptor limits for full UI test runs.

---

## 15. Security and operational notes

- **UI credentials** are env-based; default password in code is for dev only — **change in production**.
- **Session secret** must be set for production (`SSOT_UI_SECRET`).
- **GitHub token** file is user-read/write only; env overrides file.
- **`read_file` / `read_mirror_file`**: path traversal blocked (`..` rejected; resolved path must stay under mirror root).
- **Large DB**: FTS table dominates disk; WAL + busy timeout reduce **“database is locked”** under concurrent UI polling and long `index_mirror` transactions.
- **Backups**: include `ssot.db` and, if using WAL, **`-wal`/`-shm`** or checkpoint consistently; include `mirrors/` and `semantic.lance/` for full restore.

---

## 16. Feature checklist (for a greenfield reimplementation)

Use this as a parity list:

- [ ] Data dir: DB, mirrors, optional Lance, optional `.github_token`, UI org import JSON state.
- [ ] Repo registry with UUID, unique URL, display name, mirror path, timestamps.
- [ ] Shallow clone; sync with fetch + branch alignment; slug + collision suffix; configurable timeouts.
- [ ] FTS5 index with same or documented file rules and size cap; search with optional repo scope; delete index rows on repo remove.
- [ ] Denormalized **`keyword_fts_file_count`** on repo rows; maintain on index; backfill NULLs once.
- [ ] Optional semantic: chunking, local embeddings, Lance table, delete-by-repo, search with optional repo scope.
- [ ] Semantic status + error + **cached chunk count**; queue worker; retry resets count and re-queues; skipped when deps off or disabled.
- [ ] GitHub org: list public URLs, filters, token precedence; synchronous org import + deferred semantic batch enqueue.
- [ ] UI org import: background worker, JSON state, HTML + JSON progress endpoints, busy guard.
- [ ] MCP tools matching semantics above; CLI parity; Streamable HTTP default.
- [ ] Web UI: session login, flash, repo CRUD table with pagination, semantic pills + auto-refresh when pending/indexing, GitHub settings page, Cursor help page.
- [ ] SQLite: WAL + busy timeout + single connection helper pattern.
- [ ] Container: dual process MCP + UI or document separate deployments.

---

*This spec reflects the ssot-mcp codebase as of the repository version that contains this file. For install and quickstart commands, see `README.md`.*
