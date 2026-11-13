import json
import os
import stat

import pytest

from tether import health, portable
from tether.cli import main
from tether.store import Store, TetherError


def seeded(store):
    p = store.create_project("Shop", "sell mugs", "/tmp/shop")
    t = store.add_task(p["id"], "build page", priority=1)
    d1 = store.add_decision(p["id"], "Use Postgres", "familiar")
    store.add_decision(p["id"], "Use SQLite", "local", supersedes=f"d{d1['id']}")
    store.add_handoff(p["id"], "design", "Mockup", "hero and gallery", task_ids=[t["id"]])
    store.note(p["id"], "shipped pricing page")
    store.add_memory("Mug club", "monthly", "mugs", "chat", p["id"])
    store.add_memory("Loose idea", "unfiled")
    return p


def names(findings):
    return {f.name: f for f in findings}


def test_a_healthy_store_has_no_findings(store):
    seeded(store)
    assert all(f.ok for f in health.check(store))


def test_a_stale_search_index_is_found_and_repaired(store):
    seeded(store)
    store.db.execute("DELETE FROM search_idx WHERE kind='memories'")
    f = names(health.check(store))["search index"]
    assert not f.ok and f.fix
    lines = health.repair(health.check(store))
    assert lines and "rebuilt" in lines[0]
    assert all(f.ok for f in health.check(store))
    assert store.recall("mug")


def test_a_bad_archive_flag_is_reported_but_never_auto_changed(store):
    m = store.add_memory("Idea", "x")
    store.db.execute("UPDATE memories SET archived=9 WHERE id=?", (m["id"],))
    f = names(health.check(store))["archive flags"]
    assert not f.ok and f.fix is None
    assert store.memory(m["id"])["archived"] == 9


def test_a_damaged_sync_journal_is_found_and_rebuilt(tmp_path):
    shared = tmp_path / "shared"
    s = Store(tmp_path / "a.db", sync_dir=shared, machine="mac-a")
    try:
        seeded(s)
        s.flush()
        path = shared / "journals" / "mac-a.jsonl"
        path.write_bytes(path.read_bytes() + b"{broken")
        f = names(health.check(s))["this Mac's sync journal"]
        assert not f.ok and "cut short" in f.detail and f.fix
        assert health.repair(health.check(s))
        assert names(health.check(s))["this Mac's sync journal"].ok
    finally:
        s.close()


def test_doctor_prints_health_and_fix_repairs(store, capsys):
    seeded(store)
    store.db.execute("DELETE FROM search_idx")
    main(["doctor", "--fix"])        # exit code also reflects the machine's client installs
    out = capsys.readouterr().out
    assert "ledger health" in out and "fixed search index" in out


# -- export / import ------------------------------------------------------------------

def test_export_then_import_restores_everything(store, tmp_path):
    seeded(store)
    path = tmp_path / "backup.json"
    counts = portable.write_export(store, path)
    assert counts["projects"] == 1 and counts["decisions"] == 2 and counts["memories"] == 2
    fresh = Store(tmp_path / "fresh.db")
    try:
        portable.import_data(fresh, portable.read_export(path))
        assert [p["slug"] for p in fresh.projects()] == ["shop"]
        pid = fresh.find_project("shop")["id"]
        assert [t["title"] for t in fresh.tasks(pid)] == ["build page"]
        assert [d["text"] for d in fresh.decisions(pid)] == ["Use SQLite"]      # supersede chain kept
        assert len(fresh.memories(limit=50)) == 2
        assert fresh.recall("pricing")                                           # search rebuilt
        assert health.check(fresh) and all(f.ok for f in health.check(fresh))
    finally:
        fresh.close()


def test_export_file_is_private(store, tmp_path):
    path = tmp_path / "b.json"
    portable.write_export(store, path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_import_refuses_a_store_that_has_data(store, tmp_path):
    seeded(store)
    path = tmp_path / "b.json"
    portable.write_export(store, path)
    with pytest.raises(TetherError, match="already has data"):
        portable.import_data(store, portable.read_export(path))


def test_import_rejects_files_that_are_not_exports(tmp_path):
    s = Store(tmp_path / "x.db")
    try:
        for bad in ({}, {"tether_export": 99}, []):
            with pytest.raises(TetherError):
                portable.import_data(s, bad)
        (tmp_path / "junk.json").write_text("not json")
        with pytest.raises(TetherError, match="cannot read"):
            portable.read_export(tmp_path / "junk.json")
        with pytest.raises(TetherError, match="cannot read"):
            portable.read_export(tmp_path / "missing.json")
    finally:
        s.close()


def test_import_with_dangling_references_changes_nothing(store, tmp_path):
    seeded(store)
    data = portable.export_data(store)
    data["tables"]["tasks"][0]["project_id"] = 999
    fresh = Store(tmp_path / "fresh.db")
    try:
        with pytest.raises(TetherError, match="missing records"):
            portable.import_data(fresh, data)
        assert fresh.projects() == [] and fresh.memories() == []
    finally:
        fresh.close()


def test_cli_export_and_import(store, tmp_path, capsys, monkeypatch):
    seeded(store)
    out = tmp_path / "cli.json"
    assert main(["export", str(out)]) == 0
    assert json.loads(out.read_text())["counts"]["projects"] == 1
    monkeypatch.setenv("TETHER_HOME", str(tmp_path / "other-home"))
    assert main(["import", str(out)]) == 0
    assert "Restored" in capsys.readouterr().out
