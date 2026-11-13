from datetime import datetime, timedelta, timezone

from tether.store import Store


def ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def age_memory(store, mid, days):
    store.db.execute("UPDATE memories SET updated_at=?, created_at=? WHERE id=?",
                     (ago(days), ago(days), mid))
    store.db.execute("DELETE FROM usage WHERE tbl='memories' AND rid=?", (mid,))


def age_project(store, pid, days):
    t = ago(days)
    store.db.execute("UPDATE projects SET updated_at=?, created_at=? WHERE id=?", (t, t, pid))
    store.db.execute("UPDATE events SET ts=? WHERE project_id=?", (t, pid))
    store.db.execute("DELETE FROM usage WHERE tbl='projects' AND rid=?", (pid,))


def test_idle_memory_is_archived_and_fresh_one_is_not(store):
    old = store.add_memory("Old sailing idea", "knots")
    new = store.add_memory("Fresh idea", "today")
    age_memory(store, old["id"], 31)
    found = store.archive_idle()
    assert found["memories"] == [old["id"]]
    assert store.memory(old["id"])["archived"] == 2
    assert store.memory(new["id"])["archived"] == 0
    assert old["id"] not in [m["id"] for m in store.memories()]


def test_29_days_idle_is_kept(store):
    m = store.add_memory("Almost old", "x")
    age_memory(store, m["id"], 29)
    assert store.archive_idle()["memories"] == []


def test_recall_restores_archived_memory_then_rearchives_after_a_week(store):
    m = store.add_memory("Sailing app", "tides and knots")
    age_memory(store, m["id"], 45)
    store.archive_idle()
    hits = store.recall("sailing")
    assert [r["id"] for kind, r, _ in hits if kind == "memories"] == [m["id"]]
    assert store.memory(m["id"])["archived"] == 0
    # used just now, so a sweep leaves it alone
    assert store.archive_idle()["memories"] == []
    # a week and a day with no use: archived again
    store.db.execute("UPDATE usage SET last_used=? WHERE tbl='memories'", (ago(8),))
    assert store.archive_idle()["memories"] == [m["id"]]
    assert store.memory(m["id"])["archived"] == 2


def test_restored_item_not_rearchived_before_a_week(store):
    m = store.add_memory("Sailing app", "tides")
    age_memory(store, m["id"], 45)
    store.archive_idle()
    store.recall("sailing")
    store.db.execute("UPDATE usage SET last_used=? WHERE tbl='memories'", (ago(5),))
    assert store.archive_idle()["memories"] == []


def test_manual_archive_is_never_restored_by_lookup(store):
    m = store.add_memory("Dropped idea", "nope")
    store.archive_memory(m["id"])
    assert store.memory(m["id"])["archived"] == 1
    assert store.recall("dropped") == []
    assert store.memory(m["id"])["archived"] == 1


def test_get_restores_archived_memory(store):
    m = store.add_memory("Sailing app", "tides")
    age_memory(store, m["id"], 40)
    store.archive_idle()
    _, row = store.get(f"m{m['id']}")
    assert row["archived"] == 0


def test_idle_project_archived_and_restored_by_name(store, tmp_path):
    p = store.create_project("Old Thing", "goal")
    age_project(store, p["id"], 40)
    assert store.archive_idle()["projects"] == [p["id"]]
    assert store.projects() == []
    row = store.resolve_project("old-thing")
    assert row["archived"] == 0
    assert [r["slug"] for r in store.projects()] == ["old-thing"]


def test_project_with_recent_task_is_not_archived(store):
    p = store.create_project("Busy", "goal")
    store.add_task(p["id"], "do it")
    t = ago(40)
    store.db.execute("UPDATE projects SET updated_at=?, created_at=? WHERE id=?", (t, t, p["id"]))
    store.db.execute("UPDATE events SET ts=?", (t,))
    assert store.archive_idle()["projects"] == []


def test_idle_project_restored_by_working_in_its_folder(store, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    p = store.create_project("Folder App", "goal", str(root))
    age_project(store, p["id"], 40)
    store.archive_idle()
    assert store.project_for_path(root)["archived"] == 0


def test_recall_finds_records_inside_archived_project(store):
    p = store.create_project("Boat", "goal")
    store.add_decision(p["id"], "Use cedar planks", "light and rot resistant")
    age_project(store, p["id"], 40)
    store.archive_idle()
    hits = store.recall("cedar")
    assert any(k == "decisions" for k, _, _ in hits)
    assert store.project(p["id"])["archived"] == 0


def test_manually_archived_project_stays_archived(store):
    p = store.create_project("Shelved", "goal")
    store.update_project(p["id"], archived=True)
    assert store.recall("shelved") == []
    assert store.project(p["id"])["archived"] == 1


def test_disabled_by_config(store):
    from tether import config
    config.save_config({"archive_after_days": 0})
    m = store.add_memory("Ancient", "x")
    age_memory(store, m["id"], 400)
    assert store.archive_idle()["memories"] == []


def test_dry_run_changes_nothing(store):
    m = store.add_memory("Ancient", "x")
    age_memory(store, m["id"], 400)
    assert store.archive_idle(dry_run=True)["memories"] == [m["id"]]
    assert store.memory(m["id"])["archived"] == 0


def test_sweep_runs_when_store_opens_once_per_half_day(store):
    m = store.add_memory("Ancient", "x")
    age_memory(store, m["id"], 400)
    store.db.execute("DELETE FROM local_state")
    s2 = Store(store.path)
    try:
        assert s2.memory(m["id"])["archived"] == 2
        assert s2.maybe_sweep() is None     # swept just now
    finally:
        s2.close()


def test_watch_header_says_whether_it_is_focused(store):
    from tether.ui import render_watch, snapshot
    store.create_project("Alpha", "a")
    store.create_project("Beta", "b")
    snap = snapshot(store)
    assert "all active projects" in render_watch(snap)
    out = render_watch(snap, "alpha")
    assert "focused on alpha" in out and "beta" not in out.lower().replace("focused", "")


def test_snapshot_lists_only_active_projects(store):
    from tether.ui import snapshot
    a = store.create_project("Alpha", "a")
    store.create_project("Beta", "b")
    age_project(store, a["id"], 40)
    store.archive_idle()
    assert [p["slug"] for p in snapshot(store)["projects"]] == ["beta"]
