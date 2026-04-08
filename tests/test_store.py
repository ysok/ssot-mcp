"""Tests for Store (SQLite + FTS)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ssot_mcp.core.store import MAX_INDEX_BYTES, Store, _is_probably_binary, _should_index_file


def test_init_db_creates_paths(store: Store, data_root: Path) -> None:
    assert store.db_path.is_file()
    assert store.mirrors.is_dir()
    assert store.db_path.parent == data_root


def test_add_repo_and_list(store: Store, tmp_path: Path) -> None:
    mp = tmp_path / "mirror"
    mp.mkdir()
    rid = store.add_repo("https://github.com/a/b.git", "a/b", mp)
    rows = store.list_repos()
    assert len(rows) == 1
    assert rows[0]["id"] == rid
    assert rows[0]["url"] == "https://github.com/a/b.git"
    assert rows[0]["display_name"] == "a/b"


def test_iter_mirror_text_files_skips_node_modules(store: Store, tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "a.js").write_text("no")
    (root / "ok.py").write_text("yes\n")
    found = list(store.iter_mirror_text_files(root))
    assert found == [("ok.py", "yes\n")]


def test_iter_mirror_skips_binary(store: Store, tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    root.mkdir()
    (root / "bin.dat").write_bytes(b"\0\xff\xfe")
    (root / "ok.md").write_text("text")
    found = list(store.iter_mirror_text_files(root))
    assert [f[0] for f in found] == ["ok.md"]


def test_iter_mirror_skips_large_file(store: Store, tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    root.mkdir()
    huge = root / "huge.py"
    huge.write_bytes(b"x" * (MAX_INDEX_BYTES + 1))
    assert list(store.iter_mirror_text_files(root)) == []


def test_index_mirror_and_search(store: Store, tmp_path: Path) -> None:
    mirror = tmp_path / "m"
    mirror.mkdir()
    (mirror / "hello.py").write_text("def foo():\n    return bar\n")
    rid = store.add_repo("u", "n", mirror)
    n = store.index_mirror(rid, mirror)
    assert n == 1
    hits = store.search("foo", repo_id=rid, limit=10)
    assert len(hits) == 1
    assert hits[0]["path"] == "hello.py"
    assert "foo" in hits[0]["snippet"]


def test_fts_file_counts_for_repo_ids(store: Store, tmp_path: Path) -> None:
    m1 = tmp_path / "a"
    m1.mkdir()
    (m1 / "f.py").write_text("x = 1\n")
    m2 = tmp_path / "b"
    m2.mkdir()
    r1 = store.add_repo("https://a/a.git", "a", m1)
    r2 = store.add_repo("https://b/b.git", "b", m2)
    store.index_mirror(r1, m1)
    counts = store.fts_file_counts_for_repo_ids([r1, r2, "not-a-real-id"])
    assert counts[r1] == 1
    assert counts[r2] == 0
    assert counts["not-a-real-id"] == 0


def test_search_empty_query(store: Store) -> None:
    assert store.search("  ") == []


def test_search_invalid_fts_returns_empty(store: Store, tmp_path: Path) -> None:
    mirror = tmp_path / "m"
    mirror.mkdir()
    (mirror / "f.py").write_text("x")
    rid = store.add_repo("u", "n", mirror)
    store.index_mirror(rid, mirror)
    assert store.search('"unclosed quote') == []


def test_delete_repo_record_removes_fts(store: Store, tmp_path: Path) -> None:
    mirror = tmp_path / "m"
    mirror.mkdir()
    (mirror / "a.py").write_text("alpha")
    rid = store.add_repo("u", "n", mirror)
    store.index_mirror(rid, mirror)
    row = store.delete_repo_record(rid)
    assert row is not None
    assert store.list_repos() == []
    assert store.search("alpha") == []


def test_remove_mirror_dir_only_under_mirrors(store: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x").write_text("y")
    store.remove_mirror_dir(outside)
    assert (outside / "x").exists()

    inside = store.mirrors / "safe"
    inside.mkdir()
    (inside / "f").write_text("1")
    store.remove_mirror_dir(inside)
    assert not inside.exists()


def test_is_probably_binary() -> None:
    assert _is_probably_binary(b"hello") is False
    assert _is_probably_binary(b"a\0b") is True


def test_should_index_file() -> None:
    assert _should_index_file(Path("main.py")) is True
    assert _should_index_file(Path("foo.randomext")) is False
    assert _should_index_file(Path(".gitignore")) is True


def test_list_repos_page_and_count(store: Store, tmp_path: Path) -> None:
    for i in range(55):
        mp = tmp_path / f"m{i}"
        mp.mkdir()
        store.add_repo(f"https://example.com/r{i}.git", f"repo-{i:02d}", mp)
    assert store.count_repos() == 55
    p1, t = store.list_repos_page(1, per_page=50)
    assert t == 55
    assert len(p1) == 50
    p2, t2 = store.list_repos_page(2, per_page=50)
    assert t2 == 55
    assert len(p2) == 5


def test_reconcile_stale_semantic_indexing_flags_failed(
    monkeypatch: pytest.MonkeyPatch,
    store: Store,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SSOT_SEMANTIC_INDEXING_STALE_SEC", "1")
    mp = tmp_path / "m"
    mp.mkdir()
    (mp / "a.py").write_text("x = 1\n")
    rid = store.add_repo("https://example.com/r.git", "r", mp)
    store.begin_semantic_indexing(rid)
    with store.connect() as conn:
        old = "2020-01-01T00:00:00+00:00"
        conn.execute(
            """
            UPDATE repos SET semantic_indexing_heartbeat_at = ?,
            semantic_indexing_started_at = ? WHERE id = ?
            """,
            (old, old, rid),
        )
        conn.commit()
    assert store.reconcile_stale_semantic_indexing() == 1
    assert store.reconcile_stale_semantic_indexing() == 0
    rows = store.list_repos()
    assert rows[0]["semantic_status"] == "failed"
    assert "stalled" in (rows[0].get("semantic_error") or "").lower()


def test_get_repo_detail_and_update_display_name(store: Store, tmp_path: Path) -> None:
    mp = tmp_path / "m"
    mp.mkdir()
    rid = store.add_repo("https://x/y.git", "old", mp)
    row = store.get_repo_detail(rid)
    assert row is not None
    assert row["display_name"] == "old"
    assert store.update_repo_display_name(rid, "  new name  ") is True
    row2 = store.get_repo_detail(rid)
    assert row2["display_name"] == "new name"
    assert store.update_repo_display_name(rid, "   ") is False
    assert store.update_repo_display_name("missing-id", "x") is False
