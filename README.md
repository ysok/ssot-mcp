# ssot-mcp

**Single Source of Truth — MCP** mirrors Git repositories under one data directory, indexes text-like source files in **SQLite FTS5**, and (optionally) stores **embedding chunks in LanceDB** for **semantic search across all registered repos**—a practical setup when an organization ships many services (for example a product spread across hundreds of GitHub repositories). A matching **ssot-mcp** CLI uses the same database, mirrors, and vector store as the server.

## What it does

- **Clone** remotes with shallow history (`git clone --depth 1`).
- **Register** each mirror in SQLite (`repos` table) with a stable **repo id** (UUID).
- **Index** eligible files for **keyword search** (FTS5; see [Indexing](#indexing)).
- **Optional semantic index**: chunk files, embed with **fastembed** (local ONNX, open-weight models, no cloud API keys), store vectors in LanceDB for **cross-repo questions** (see [Semantic search](#semantic-search)).
- **GitHub organizations**: list **public** repos via the GitHub API; **add** new mirrors (clone + index) or **sync** existing ones (fetch + re-index + semantic when enabled) (`add-org` / MCP `add_github_organization`).

Out of scope today: GitLab org import and first-class “local path” remotes (workaround: host a bare remote or use `file://` where git allows).

## Requirements

- **Python** 3.10 or newer (3.11+ used in the container image).
- **git** on `PATH` for clone/sync.
- Optional **GITHUB_TOKEN** or **GITHUB_PERSONAL_ACCESS_TOKEN** for higher GitHub API rate limits when importing orgs (`GITHUB_TOKEN` is checked first if both are set; recommended above ~60 API calls/hour without auth).
- For **semantic search**: install `**[semantic]`** (LanceDB + fastembed); no API keys (see [Semantic search](#semantic-search)).

## Install

```bash
git clone <this-repo>
cd ssot-mcp
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
# Optional: LanceDB + fastembed for semantic index/search (on-device, open-source models)
pip install -e '.[semantic]'
# Optional: Admin web UI (FastAPI, port 8081 by default)
pip install -e '.[ui]'
```

The **ssot-mcp** command is installed via `[project.scripts]` in `pyproject.toml`. The `**[semantic]`** extra pulls in **LanceDB** and **[fastembed](https://github.com/qdrant/fastembed)** (CPU ONNX embeddings; default model **BAAI/bge-small-en-v1.5**, Apache-2.0). The `**[ui]`** extra adds **FastAPI**, **Uvicorn**, and session auth for the browser admin (command `**ssot-mcp-ui`**).

## Running tests

From the repository root, with a virtual environment activated:

```bash
# Minimal: pytest + core/service tests
pip install -e '.[test]'

# Recommended: include UI tests (FastAPI) and semantic-related tests (LanceDB + fastembed)
pip install -e '.[test,ui,semantic]'

python -m pytest tests/
```

`pyproject.toml` sets `**testpaths = ["tests"]**` and `**pythonpath = ["src"]**`, so you can run `**pytest**` with no arguments from the repo root as well. Useful variants:

```bash
python -m pytest tests/ -q          # quiet (also the default addopts in pyproject)
python -m pytest tests/ -v          # verbose per test
python -m pytest tests/test_repos.py -q   # one file
python -m pytest tests/test_repos.py::test_add_repository_success -v   # one test
```

Without `**[ui]**` installed, `tests/test_ui_app.py` is skipped (it uses `pytest.importorskip("fastapi")`). Without `**[semantic]**`, a few semantic tests are skipped by markers.

**macOS / “Too many open files”:** The default per-process file descriptor limit is often low (`**ulimit -n`** around 256). A full run opens many short-lived HTTP test clients and DB handles; if pytest ends with `**OSError: [Errno 24] Too many open files**`, raise the limit for that shell and retry:

```bash
ulimit -n 4096
python -m pytest tests/
```

The `**client**` fixture in `tests/test_ui_app.py` also calls `**httpx.Client.close()**` after each UI test so transports are released promptly (Starlette’s `TestClient` context manager alone does not always close the underlying HTTP transport).

## Data directory

All state lives under **SSOT_DATA_DIR**. In the **container image** the default is `**/data`** (see `Containerfile`). On your laptop, set `**SSOT_DATA_DIR**` to any directory you want (for example `**$HOME/.local/share/ssot-mcp**`).


| Path                            | Purpose                                                                                                                                                      |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `SSOT_DATA_DIR/ssot.db`         | SQLite registry + FTS5 index                                                                                                                                 |
| `SSOT_DATA_DIR/mirrors/<slug>/` | Cloned working trees (e.g. message **Mirror: `/data/mirrors/ysok__article-summarizer`** means that path **inside** the container when `SSOT_DATA_DIR=/data`) |
| `SSOT_DATA_DIR/semantic.lance/` | LanceDB dataset for semantic (fastembed) chunks — override with **SSOT_SEMANTIC_LANCE_DIR** if needed                                                        |


The CLI accepts `**--data-dir`**; it overrides the env var for that process only (resolved path wins).

```bash
export SSOT_DATA_DIR="$HOME/.local/share/ssot-mcp"
ssot-mcp list
```

## MCP server

### Run (HTTP — for Cursor against a URL)

```bash
export SSOT_DATA_DIR=./data
export FASTMCP_HOST=127.0.0.1
export FASTMCP_PORT=8765
python -m ssot_mcp.mcp.server
```

Default transport is **Streamable HTTP**. Connect Cursor (or another MCP client) to:

```text
http://127.0.0.1:8765/mcp
```

### Cursor IDE

1. Keep the MCP process running with **streamable HTTP** (default): see [Run (HTTP)](#run-http--for-cursor-against-a-url) above. The endpoint path is `**/mcp`** on **FASTMCP_PORT** (default **8765**).
2. In **Cursor**, add an MCP server via **Settings → Tools & MCP** or by editing `**~/.cursor/mcp.json`** (user-wide) or `**.cursor/mcp.json**` (project). Use **streamable HTTP** transport with that URL, for example:

```json
{
  "mcpServers": {
    "ssot-mcp": {
      "type": "streamableHttp",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

1. Restart **Cursor** after changing MCP config. Optional: run with **SSOT_MCP_TRANSPORT=stdio** and a `**command` / `args`** entry in `mcp.json` instead if you want Cursor to spawn the server (see the in-app **Cursor MCP** help page in the admin UI).

With `**pip install -e '.[ui]'`**, the admin UI includes **Cursor MCP** in the nav (`/help/cursor-mcp`) with copy-paste examples and the effective MCP URL for your environment.

## Admin web UI (separate port)

Browser UI to **list**, **add**, **edit display name**, **delete** repos, and **import a GitHub org** (same logic as CLI/MCP). Use **GitHub token** in the nav to save a PAT for higher GitHub API rate limits on org import (stored as `**SSOT_DATA_DIR/.github_token`**; **GITHUB_TOKEN** or **GITHUB_PERSONAL_ACCESS_TOKEN** in the environment overrides the file when set—`GITHUB_TOKEN` wins if both are set). Runs on **SSOT_UI_PORT** (default **8081**), separate from MCP **8765**.

```bash
pip install -e '.[ui]'
export SSOT_DATA_DIR=./data
export SSOT_UI_SECRET="$(openssl rand -hex 32)"   # required for stable sessions in production
ssot-mcp-ui
# Open http://127.0.0.1:8081/ — sign in, then manage repos
```


| Variable            | Purpose                                                                        |
| ------------------- | ------------------------------------------------------------------------------ |
| `SSOT_UI_HOST`      | Bind address (default `0.0.0.0`)                                               |
| `SSOT_UI_PORT`      | Listen port (default `8081`)                                                   |
| `SSOT_UI_USER`      | Login username (default `admin`)                                               |
| `SSOT_UI_PASSWORD`  | Login password (default matches project template; **override in production**)  |
| `SSOT_UI_SECRET`    | Session signing secret (**set explicitly** so restarts don’t log everyone out) |
| `SSOT_UI_PAGE_SIZE` | Repositories per page (default **50**, max 100)                                |


**Security:** The default password is for **local/demo** use only. Use a strong `SSOT_UI_PASSWORD`, set `SSOT_UI_SECRET`, and put the UI behind HTTPS and a reverse proxy if exposed beyond localhost.

**Container:** The image runs **both** MCP and UI via `python -m ssot_mcp.runtime` (see `Containerfile`). Map **8765** and **8081**.

### Run (stdio)

```bash
export SSOT_MCP_TRANSPORT=stdio
python -m ssot_mcp.mcp.server
```

### Environment variables


| Variable                       | Purpose                                                                                                                     |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| `SSOT_DATA_DIR`                | Root for `ssot.db` and `mirrors/`                                                                                           |
| `FASTMCP_HOST`                 | Bind address (default `0.0.0.0` in code for containers; set `127.0.0.1` locally if needed)                                  |
| `FASTMCP_PORT`                 | Listen port (default `8765`)                                                                                                |
| `SSOT_MCP_TRANSPORT`           | `streamable-http` (default), `stdio`, or `sse`                                                                              |
| `GITHUB_TOKEN`                 | Optional; GitHub API auth for org import (**checked first**; **overrides** UI-saved token in `SSOT_DATA_DIR/.github_token`) |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | Optional; same as `GITHUB_TOKEN` if that variable is unset                                                                  |
| `SSOT_LOCAL_EMBEDDING_MODEL`   | Optional; fastembed model id (default **BAAI/bge-small-en-v1.5**, Apache-2.0). First run downloads model weights.           |
| `SSOT_SEMANTIC_LANCE_DIR`      | Optional; override Lance directory name under `SSOT_DATA_DIR` (default `**semantic.lance`**).                               |
| `SSOT_GIT_CLONE_TIMEOUT`       | Optional; `git clone` timeout in seconds (default **600**, min 60, max 86400). Raise for very large repos.                  |
| `SSOT_GIT_SYNC_TIMEOUT`        | Optional; main `git fetch` / `pull` timeout in **sync** (default **600**, same bounds).                                     |
| `SSOT_SEMANTIC_INDEX`          | Optional; default `1`. Set to `0` to skip embedding work on **add**/**sync** (e.g. bulk org import).                        |
| `SSOT_EMBEDDING_BATCH`         | Optional; batch size passed to fastembed per flush (default `48`; lower on slow CPUs).                                      |
| `SSOT_EMBEDDING_SLEEP_MS`      | Optional; milliseconds to sleep between batches (throttle CPU).                                                             |


### MCP tools


| Tool                      | Description                                                                                                                                                    |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `list_repositories`       | List registered repos (id, URL, mirror path, timestamps).                                                                                                      |
| `add_repository`          | Clone one URL (HTTPS/SSH), register, index.                                                                                                                    |
| `add_github_organization` | List public repos for `https://github.com/ORG` (or org login); clone + index new URLs, sync + re-index existing. Optional `exclude_forks`, `exclude_archived`. |
| `remove_repository`       | Remove registry row, FTS rows, and delete mirror directory.                                                                                                    |
| `sync_repository`         | `git fetch` / reset to default branch, then re-index.                                                                                                          |
| `search_code`             | FTS5 **keyword** search; optional `repo_id` and `limit`.                                                                                                       |
| `semantic_search`         | **Semantic** search; needs `**pip install 'ssot-mcp[semantic]'`** (LanceDB + fastembed, on-device).                                                            |
| `read_file`               | Read a file under a mirror by `repo_id` and repo-relative `path`.                                                                                              |


Duplicate URLs are rejected at add time (reported as already registered).

**Hybrid retrieval (recommended at scale):** use `**semantic_search`** to find candidate areas, `**search_code**` for exact symbols/strings, and `**read_file**` to ground answers in real source.

## CLI

```text
ssot-mcp [--data-dir PATH] <command> ...
```


| Command                   | Description                                                                                                                        |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `list`                    | List repositories (same info as MCP `list_repositories`).                                                                          |
| `add <url>`               | Clone, register, index one remote.                                                                                                 |
| `add-org <org-or-url>`    | Import all **public** GitHub repos for an org (clone + index new; sync + re-index existing). Flags: `--no-forks`, `--no-archived`. |
| `remove <repo_id>`        | Remove repo and mirror.                                                                                                            |
| `sync <repo_id>`          | Update from remote and re-index.                                                                                                   |
| `search <query>`          | FTS search; `--repo ID`, `--limit N`, `--plain` (TSV lines).                                                                       |
| `semantic-search <query>` | Embedding search; `--repo ID`, `--top-k N` (same requirements as MCP `semantic_search`).                                           |
| `cat <repo_id> <path>`    | Print file contents; `--max-bytes N`.                                                                                              |


Exit codes: **0** on success; **1** on errors (and for `search` with no matches in `--plain` mode, failed org import when nothing was added or updated, or failed **semantic-search**).

Examples:

```bash
export GITHUB_TOKEN=ghp_...              # or: export GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...
# pip install -e '.[semantic]' for semantic-search / MCP semantic_search
ssot-mcp add https://github.com/modelcontextprotocol/python-sdk.git
ssot-mcp add-org https://github.com/kubernetes --no-forks --no-archived
ssot-mcp search "FastMCP" --limit 10
ssot-mcp cat <repo-id> README.md
```

## GitHub org import

- Accepts `**https://github.com/ORG**`, `**github.com/ORG**`, or the **org login** only.
- Does **not** accept `https://github.com/owner/repo` — use `**add`** for a single repository.
- Uses `**GET /orgs/{org}/repos?type=public**` with pagination (stdlib HTTP, no extra dependencies).
- Without **GITHUB_TOKEN** or **GITHUB_PERSONAL_ACCESS_TOKEN**, GitHub’s unauthenticated rate limit applies; set one of them (or use the UI token file) for large organizations.

## Indexing

Implemented in `store.py`:

- **Skipped directories** include `.git`, `node_modules`, `__pycache__`, `.venv`, `target`, `build`, `dist`, `.idea`, `.vscode`, and similar.
- **File size** capped at **256 KiB** per indexed file.
- **Extensions** are allow-listed (code, configs, docs — e.g. `.py`, `.ts`, `.go`, `.md`, `.yaml`, `Dockerfile`, …). Dotfiles like `.gitignore` are included when matched.
- **Binary** detection skips files containing NUL in the first 8 KiB.

Re-run `**sync_repository`** (MCP or CLI) after remote changes to refresh the FTS index and (when configured) the semantic index.

## Semantic search

Designed for **many repositories** that together form a system (e.g. a large org or product line): you ask a **natural-language question** once; results can span **any** mirrored repo.

Embeddings run **on your machine** with **[fastembed](https://github.com/qdrant/fastembed)** (ONNX). Default model **BAAI/bge-small-en-v1.5** (Apache-2.0). **No cloud API keys** for ssot-mcp semantic indexing.

1. **Install:** `pip install -e '.[semantic]'` (LanceDB + fastembed). The container image uses `**[semantic,ui]`**.
2. **Add or sync** repos — vectors are stored under `**SSOT_DATA_DIR/semantic.lance/`** (unless **SSOT_SEMANTIC_LANCE_DIR** overrides the name).
3. **Query** via MCP `**semantic_search`** or `**ssot-mcp semantic-search "…"**`.
4. **Optional:** **SSOT_LOCAL_EMBEDDING_MODEL** for another fastembed-supported model. For large org imports on a laptop, consider **SSOT_EMBEDDING_BATCH=8** and **SSOT_EMBEDDING_SLEEP_MS** so embedding stays responsive.

**Migration from older ssot-mcp:** If you previously had a different embedding backend or path (`**semantic-local.lance`**, or OpenAI-sized `**semantic.lance**`), **delete** the old Lance directory under `**SSOT_DATA_DIR`** (or set `**SSOT_SEMANTIC_LANCE_DIR**` to the old folder name temporarily) and **sync** repos again so dimensions match the current fastembed model.

**Operational notes**

- **CPU & time:** importing **many** repos embeds every chunk locally; use **SSOT_SEMANTIC_INDEX=0** during a huge **add-org**, then **sync** per repo when you want vectors.
- **Quality:** default **bge-small** is a good balance of speed and quality on CPU; other fastembed models may change vector dimension (requires a fresh `**semantic.lance`**).
- **Scale:** LanceDB supports **vector indexes** (IVF/HNSW-style) for very large chunk counts; the default linear scan is fine for early deployments—consult LanceDB docs when `_distance` queries slow down.

## Container (CentOS Stream 9)

The **Containerfile** builds an image with Python 3.11, git, `pip install '.[semantic,ui]'` (LanceDB + fastembed + UI), and starts **MCP + admin UI** with `python -m ssot_mcp.runtime`. The container needs **SSOT_UI_SECRET** and **SSOT_UI_PASSWORD** for the web UI. **GITHUB_TOKEN** / **GITHUB_PERSONAL_ACCESS_TOKEN** are optional for org import. A plain `podman run` / `docker run` does **not** see your shell’s environment unless you forward it:

- **Per variable (portable):** `-e VAR` with **no** `=value` copies **VAR** from the host when it is set (works in Docker and Podman).
- **Many variables:** `--env-file /path/to/file` (Docker and Podman; file lines look like `NAME=value`).
- `**--env-host`:** some Podman builds offer this to copy the **entire** host environment. It is **not** in every Podman version (and **Docker** does not have it). If you get `unknown flag: --env-host`, use `-e VAR` lines or `--env-file` instead.

```bash
podman build -f Containerfile -t ssot-mcp .

# Host already has GITHUB_PERSONAL_ACCESS_TOKEN — no value after -e (copies from host if set):
podman run --rm -p 8765:8765 -p 8081:8081 -v ssot-data:/data \
  -e GITHUB_PERSONAL_ACCESS_TOKEN \
  -e SSOT_UI_PORT=8081 \
  -e SSOT_UI_SECRET="$(openssl rand -hex 32)" \
  -e SSOT_UI_PASSWORD='your-strong-password' \
  ssot-mcp

# Or pass GitHub token with a literal value (no host export needed):
podman run --rm -p 8765:8765 -p 8081:8081 -v ssot-data:/data \
  -e GITHUB_PERSONAL_ACCESS_TOKEN=... \
  -e SSOT_UI_PORT=8081 \
  -e SSOT_UI_SECRET="$(openssl rand -hex 32)" \
  -e SSOT_UI_PASSWORD='your-strong-password' \
  ssot-mcp
```

If logs show the admin UI on **8080** but you mapped **8081**, the image is probably **out of date** (old default) or **SSOT_UI_PORT** was set to 8080—add `**-e SSOT_UI_PORT=8081`** as above, or rebuild: `podman build -f Containerfile -t ssot-mcp .`

- MCP URL from the host: **[http://localhost:8765/mcp](http://localhost:8765/mcp)**
- Admin UI: **[http://localhost:8081/](http://localhost:8081/)**
- Run the CLI inside the same volume:  
`podman run --rm -v ssot-data:/data ssot-mcp ssot-mcp list`

SSH clone URLs inside the container require you to mount keys or use HTTPS remotes unless you extend the image.

### Where are `mirrors` on disk? (container vs host)

- `**/data/mirrors/...`** is a path **inside the container**. The app stores clones under `**$SSOT_DATA_DIR/mirrors/`**; with the default image env, that is `**/data/mirrors/<slug>/**`.
- `**-v ssot-data:/data**` attaches a **named volume** called `ssot-data` at `/data`. Data **persists**, but it lives in Podman/Docker’s volume storage (not automatically as a normal folder in your home directory). The mirrors are still “real” files—just inside that volume’s filesystem.
- **To use a folder on your Mac (or Linux host)** so you can open `mirrors` in Finder or back it up directly, bind-mount a host directory instead:

```bash
mkdir -p "$HOME/ssot-mcp-data"
podman run --rm -p 8765:8765 -p 8081:8081 \
  -v "$HOME/ssot-mcp-data:/data" \
  -e SSOT_UI_PORT=8081 \
  -e SSOT_UI_SECRET="$(openssl rand -hex 32)" \
  -e SSOT_UI_PASSWORD='your-strong-password' \
  ssot-mcp
```

Then the same repo appears on the host as `**$HOME/ssot-mcp-data/mirrors/ysok__article-summarizer/**` (alongside `**ssot.db**`, LanceDB dirs, etc.). Use the **same mount** for every `podman run` that should share that data.

**Embeddings:** The image includes **fastembed** (via `**[semantic]`**). Semantic indexing needs no API keys. Use the same **SSOT_DATA_DIR** (and optional **SSOT_SEMANTIC_LANCE_DIR**) for MCP, UI, and CLI so `**semantic.lance`** stays consistent.

## Project layout

```text
ssot-mcp/
  Containerfile
  pyproject.toml
  README.md
  src/ssot_mcp/
    __init__.py
    cli/main.py           # argparse entrypoint
    core/store.py         # SQLite + FTS5
    git/git_ops.py        # clone / sync
    github/github_org.py  # GitHub org listing
    services/repos.py     # Shared add/remove/sync/search/list logic
    embeddings/semantic.py  # LanceDB + fastembed ([semantic])
    mcp/server.py         # FastMCP server + tools
    runtime/              # MCP + UI process supervisor (container)
    ui/                   # FastAPI admin (templates, static, routers)
  tests/                  # pytest suite
```

## Tests

```bash
source .venv/bin/activate
pip install -e '.[test]'          # or '.[ui,semantic,test]' for full UI + semantic tests
ulimit -n 4096   # if you still see Errno 24 on a full run
pytest
```

Tests live under `**tests/**` and use **pytest** with `pythonpath = src` (see `pyproject.toml`). They cover **core.store**, **git.git_ops**, **github.github_org**, **services.repos**, **embeddings.semantic** (fastembed path mocked), **ui** (FastAPI **TestClient**, auth, pagination, mocked add/delete/org import), **cli.main**, and a small **mcp.server** smoke check. Network and real git remotes are **not** required: **git**, **GitHub HTTP**, **embeddings**, and heavy UI actions are **mocked** where needed.

## License

Add a `LICENSE` file to the repository when you publish it; until then, usage is at your discretion.