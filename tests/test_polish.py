import sqlite3

from tether import selftest
from tether.cli import main
from tether.store import Store


def age(store, table, rid, days=45):
    from datetime import datetime, timedelta, timezone
    t = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    store.db.execute(f"UPDATE {table} SET updated_at=?, created_at=? WHERE id=?", (t, t, rid))
    store.db.execute("DELETE FROM usage WHERE tbl=? AND rid=?", (table, rid))


def test_archived_lists_both_kinds_and_says_why(store, capsys):
    a = store.add_memory("Old thought", "x")
    b = store.add_memory("Dropped thought", "y")
    age(store, "memories", a["id"])
    store.archive_idle()
    store.archive_memory(b["id"])
    assert main(["archived"]) == 0
    out = capsys.readouterr().out
    assert "Old thought" in out and "(unused)" in out
    assert "Dropped thought" in out and "(by you)" in out


def test_archived_when_empty(store, capsys):
    main(["archived"])
    assert "Nothing is archived" in capsys.readouterr().out


def test_restore_memory_and_project(store, capsys):
    m = store.add_memory("Thought", "x")
    store.archive_memory(m["id"])
    p = store.create_project("Shelf", "g")
    store.update_project(p["id"], archived=True)
    assert main(["restore", f"m{m['id']}"]) == 0
    assert main(["restore", "shelf"]) == 0
    assert store.memory(m["id"])["archived"] == 0
    assert store.project(p["id"])["archived"] == 0
    assert main(["restore", "ghost"]) == 1
    assert "nothing called" in capsys.readouterr().err


def test_database_problems_are_explained_not_dumped(store, capsys, monkeypatch):
    def boom(_args):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr("tether.cli.cmd_archived", boom)
    assert main(["archived"]) == 1
    assert "busy" in capsys.readouterr().err

    def damaged(_args):
        raise sqlite3.DatabaseError("database disk image is malformed")
    monkeypatch.setattr("tether.cli.cmd_archived", damaged)
    assert main(["archived"]) == 1
    err = capsys.readouterr().err
    assert "damaged" in err and "tether doctor" in err and "Traceback" not in err


def test_debug_flag_lets_the_real_error_through(store, monkeypatch):
    import pytest

    def boom(_args):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr("tether.cli.cmd_archived", boom)
    monkeypatch.setenv("TETHER_DEBUG", "1")
    with pytest.raises(sqlite3.OperationalError):
        main(["archived"])


def test_install_ends_with_a_working_check_and_next_steps(monkeypatch, capsys):
    from tether import install
    ok = install.Result("Claude Code", "installed", "ok") if hasattr(install, "Result") else None
    monkeypatch.setattr(install, "install_code", lambda dry_run=False: ok)
    monkeypatch.setattr(install, "install_hook", lambda dry_run=False: ok)
    monkeypatch.setattr(install, "install_desktop", lambda dry_run=False: ok)
    monkeypatch.setattr(selftest, "run", lambda: [selftest.Check("server starts", True)])
    assert main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Checking that it works" in out and "All good" in out and "tether init" in out


def test_install_reports_a_failed_check(monkeypatch, capsys):
    from tether import install
    ok = install.Result("Claude Code", "installed", "ok") if hasattr(install, "Result") else None
    for name in ("install_code", "install_hook", "install_desktop"):
        monkeypatch.setattr(install, name, lambda dry_run=False: ok)
    monkeypatch.setattr(selftest, "run", lambda: [selftest.Check("server starts", False, "boom")])
    assert main(["install"]) == 1
    assert "doctor --deep" in capsys.readouterr().out


def test_ui_restore_actions(store):
    from tether.ui import act, snapshot
    m = store.add_memory("Thought", "x")
    p = store.create_project("Shelf", "g")
    store.archive_memory(m["id"])
    store.update_project(p["id"], archived=True)
    snap = snapshot(store)
    assert {a["type"] for a in snap["archived"]} == {"idea", "project"}
    act(store, {"action": "restore_memory", "id": m["id"]})
    act(store, {"action": "restore_project", "project": "shelf"})
    assert snapshot(store)["archived"] == []


def test_marker_changes_when_a_project_is_archived(store):
    p = store.create_project("Shelf", "g")
    before = store.change_marker()
    store.db.execute("UPDATE projects SET archived=2 WHERE id=?", (p["id"],))
    assert store.change_marker() != before


def test_restore_item_counts_as_use(tmp_path):
    s = Store(tmp_path / "x.db")
    try:
        m = s.add_memory("Thought", "x")
        age(s, "memories", m["id"])
        s.archive_idle()
        s.restore_item(f"m{m['id']}")
        assert s.archive_idle()["memories"] == []
    finally:
        s.close()
