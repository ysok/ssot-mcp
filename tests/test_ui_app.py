"""Tests for the admin web UI."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient

from ssot_mcp.core.store import Store
from ssot_mcp.ui.app import create_app


@pytest.fixture()
def ui_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("SSOT_DATA_DIR", str(data))
    monkeypatch.setenv("SSOT_UI_SECRET", "unit-test-session-secret-key-32b")
    monkeypatch.setenv("SSOT_UI_USER", "admin")
    monkeypatch.setenv("SSOT_UI_PASSWORD", "test-ui-password")
    monkeypatch.setenv("SSOT_UI_PAGE_SIZE", "50")
    return data


@pytest.fixture()
def client(ui_env: Path) -> Iterator[TestClient]:
    # Starlette TestClient.__exit__ does not call httpx.Client transport cleanup; without
    # an explicit close(), repeated tests can exhaust file descriptors (macOS default ulimit).
    c = TestClient(create_app())
    try:
        with c:
            yield c
    finally:
        c.close()


def test_static_icon_served(client: TestClient) -> None:
    r = client.get("/static/ssot-mcp.png")
    assert r.status_code == 200
    assert "image/png" in r.headers.get("content-type", "")


def test_root_redirects_to_repos(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/repos"


def test_repos_requires_login(client: TestClient) -> None:
    r = client.get("/repos", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_failure(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_github_settings_requires_login(client: TestClient) -> None:
    r = client.get("/settings/github", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers.get("location", "")


def test_cursor_mcp_help_requires_login(client: TestClient) -> None:
    r = client.get("/help/cursor-mcp", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers.get("location", "")


def test_cursor_mcp_help_page(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FASTMCP_HOST", "127.0.0.1")
    monkeypatch.setenv("FASTMCP_PORT", "8765")
    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    r = client.get("/help/cursor-mcp")
    assert r.status_code == 200
    assert "Connect Cursor" in r.text
    assert "streamableHttp" in r.text
    assert "http://127.0.0.1:8765/mcp" in r.text
    assert "SSOT_MCP_TRANSPORT" in r.text
    assert "fastembed" in r.text.lower()


def test_github_settings_page_and_save_token(client: TestClient, ui_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    r = client.get("/settings/github")
    assert r.status_code == 200
    assert "GitHub API token" in r.text
    assert "Current source" in r.text

    r2 = client.post(
        "/settings/github",
        data={"github_token": "ghp_test_saved_token"},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    token_file = ui_env / ".github_token"
    assert token_file.is_file()
    assert token_file.read_text(encoding="utf-8").strip() == "ghp_test_saved_token"

    r3 = client.get("/settings/github")
    assert r3.status_code == 200
    assert "Saved token file" in r3.text


def test_github_settings_clear_token(client: TestClient, ui_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    ui_env.mkdir(parents=True, exist_ok=True)
    (ui_env / ".github_token").write_text("old-value\n", encoding="utf-8")
    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    r = client.post("/settings/github", data={"clear_saved_token": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert not (ui_env / ".github_token").exists()


def test_semantic_retry_post_requires_login(client: TestClient, ui_env: Path) -> None:
    r = client.post("/repos/00000000-0000-0000-0000-000000000001/semantic-retry", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_semantic_retry_post_logged_in_redirects_repos(client: TestClient, ui_env: Path) -> None:
    s = Store(ui_env)
    s.init_db()
    mp = ui_env / "mirror"
    mp.mkdir()
    rid = s.add_repo("https://github.com/o/x.git", "x", mp)
    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    with (
        patch("ssot_mcp.services.repos._semantic_features_wanted", return_value=True),
        patch("ssot_mcp.embeddings.semantic_queue.enqueue_semantic_index"),
    ):
        r = client.post(f"/repos/{rid}/semantic-retry", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/repos"


def test_repos_list_shows_keyword_and_semantic_columns(client: TestClient, ui_env: Path) -> None:
    s = Store(ui_env)
    s.init_db()
    mp = ui_env / "m"
    mp.mkdir()
    (mp / "a.py").write_text("v = 1\n")
    rid = s.add_repo("https://example.com/r.git", "r", mp)
    s.index_mirror(rid, mp)

    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    r = client.get("/repos")
    assert r.status_code == 200
    assert "Keywords" in r.text
    assert "Semantic" in r.text
    assert "Actions" in r.text
    assert "1 file" in r.text


def test_login_success_and_list(client: TestClient, ui_env: Path) -> None:
    r = client.post(
        "/login",
        data={"username": "admin", "password": "test-ui-password"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/repos"
    r2 = client.get("/repos")
    assert r2.status_code == 200
    assert "Repositories" in r2.text


def test_logout(client: TestClient) -> None:
    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    r2 = client.get("/repos", follow_redirects=False)
    assert r2.status_code == 303


def test_pagination_second_page(client: TestClient, ui_env: Path) -> None:
    s = Store(ui_env)
    s.init_db()
    for i in range(55):
        mp = ui_env / f"m{i}"
        mp.mkdir()
        s.add_repo(f"https://example.com/{i}.git", f"z-repo-{i:03d}", mp)

    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    r1 = client.get("/repos?page=1")
    assert r1.status_code == 200
    assert "Page 1 / 2" in r1.text or "page 1 of 2" in r1.text.lower()
    r2 = client.get("/repos?page=2")
    assert r2.status_code == 200
    assert "z-repo-054" in r2.text or "repo-054" in r2.text


def test_add_repo_post_mocked(client: TestClient, ui_env: Path) -> None:
    def fake_add(store, url: str):
        from ssot_mcp.services.repos import ActionResult

        return ActionResult(True, "Added ok")

    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    with patch("ssot_mcp.ui.routers.repos_routes.add_repository", side_effect=fake_add):
        r = client.post("/repos/new", data={"url": "https://github.com/a/b.git"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/repos"


def test_edit_display_name(client: TestClient, ui_env: Path) -> None:
    s = Store(ui_env)
    s.init_db()
    mp = ui_env / "m1"
    mp.mkdir()
    rid = s.add_repo("https://x/y.git", "orig", mp)

    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    r = client.post(f"/repos/{rid}/edit", data={"display_name": "renamed"}, follow_redirects=False)
    assert r.status_code == 303
    assert s.get_repo_detail(rid)["display_name"] == "renamed"


def test_delete_repo_mocked(client: TestClient, ui_env: Path) -> None:
    s = Store(ui_env)
    s.init_db()
    mp = ui_env / "m1"
    mp.mkdir()
    rid = s.add_repo("https://x/y.git", "n", mp)

    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    with patch("ssot_mcp.ui.routers.repos_routes.remove_repository") as rm:
        from ssot_mcp.services.repos import ActionResult

        rm.return_value = ActionResult(True, "gone")
        r = client.post(f"/repos/{rid}/delete", follow_redirects=False)
    assert r.status_code == 303
    rm.assert_called_once()


def test_import_org_post_redirects_to_progress(client: TestClient, ui_env: Path) -> None:
    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    with patch("ssot_mcp.ui.routers.repos_routes.start_org_import_job", return_value="started"):
        r = client.post(
            "/import-org",
            data={"org": "https://github.com/o", "no_forks": "1"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/import-org/progress"


def test_import_org_busy_redirects_to_progress(client: TestClient, ui_env: Path) -> None:
    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    with patch("ssot_mcp.ui.routers.repos_routes.start_org_import_job", return_value="busy"):
        r = client.post("/import-org", data={"org": "o"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/import-org/progress"


def test_import_org_progress_page_ok(client: TestClient, ui_env: Path) -> None:
    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    r = client.get("/import-org/progress")
    assert r.status_code == 200
    assert "Organization import" in r.text


def test_import_org_progress_json_requires_login(client: TestClient, ui_env: Path) -> None:
    r = client.get("/import-org/progress.json")
    assert r.status_code == 401


def test_import_org_progress_json_logged_in(client: TestClient, ui_env: Path) -> None:
    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    r = client.get("/import-org/progress.json")
    assert r.status_code == 200
    assert r.json().get("status") == "idle"


def test_add_repo_failure_redirects_to_form(client: TestClient, ui_env: Path) -> None:
    from ssot_mcp.services.repos import ActionResult

    client.post("/login", data={"username": "admin", "password": "test-ui-password"})
    with patch(
        "ssot_mcp.ui.routers.repos_routes.add_repository",
        return_value=ActionResult(False, "Error: git clone failed.\nstderr here"),
    ):
        r = client.post("/repos/new", data={"url": "https://github.com/bad/repo.git"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/repos/new"
