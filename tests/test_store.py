import pytest

from tether import config
from tether.store import TetherError, parse_ref


def test_slugs_are_unique(store):
    a = store.create_project("My App")
    b = store.create_project("my app!")
    assert (a["slug"], b["slug"]) == ("my-app", "my-app-2")


def test_resolve_by_nested_path_prefers_deepest(store, tmp_path):
    outer = tmp_path / "mono"
    inner = outer / "packages" / "web"
    inner.mkdir(parents=True)
    store.create_project("mono", root_path=str(outer))
    store.create_project("web", root_path=str(inner))
    assert store.resolve_project(cwd=str(inner / "src"))["slug"] == "web"
    assert store.resolve_project(cwd=str(outer / "docs"))["slug"] == "mono"


def test_path_prefix_does_not_match_sibling(store, tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app-old").mkdir()
    store.create_project("app", root_path=str(tmp_path / "app"))
    assert store.project_for_path(tmp_path / "app-old") is None


def test_resolve_order(store, tmp_path, monkeypatch):
    with pytest.raises(TetherError, match="no projects"):
        store.resolve_project()
    a = store.create_project("alpha")
    assert store.resolve_project()["id"] == a["id"]  # the only project
    store.create_project("beta")
    with pytest.raises(TetherError, match="which project"):
        store.resolve_project()
    config.save_config({"current_project": "beta"})
    assert store.resolve_project()["slug"] == "beta"
    monkeypatch.setenv("TETHER_PROJECT", "alpha")
    assert store.resolve_project()["slug"] == "alpha"
    assert store.resolve_project("beta")["slug"] == "beta"
    with pytest.raises(TetherError, match="no project named"):
        store.resolve_project("gamma")


def test_supersede_decision(store, project):
    pid = project["id"]
    d1 = store.add_decision(pid, "Use Postgres", "familiar")
    d2 = store.add_decision(pid, "Use SQLite", "local-first", supersedes="d%d" % d1["id"])
    active = store.decisions(pid)
    assert [d["id"] for d in active] == [d2["id"]]
    old = store.decisions(pid, include_superseded=True)[-1]
    assert old["status"] == "superseded" and old["superseded_by"] == d2["id"]


def test_supersede_rejects_other_projects_decision(store, project):
    other = store.create_project("other")
    d = store.add_decision(other["id"], "x")
    with pytest.raises(TetherError):
        store.add_decision(project["id"], "y", supersedes=d["id"])


def test_task_lifecycle_and_ordering(store, project):
    pid = project["id"]
    low = store.add_task(pid, "polish", priority=3)
    high = store.add_task(pid, "auth", priority=1, for_surface="code")
    doing = store.add_task(pid, "landing")
    store.update_task(doing["id"], status="doing")
    assert [t["id"] for t in store.tasks(pid)] == [doing["id"], high["id"], low["id"]]
    done = store.update_task(high["id"], status="done", note="merged")
    assert done["closed_at"] and done["note"] == "merged"
    assert high["id"] not in [t["id"] for t in store.tasks(pid)]
    assert store.task_counts(pid)["done"] == 1
    reopened = store.update_task(high["id"], status="todo")
    assert reopened["closed_at"] is None


def test_bad_inputs(store, project):
    pid = project["id"]
    with pytest.raises(TetherError):
        store.add_task(pid, "  ")
    with pytest.raises(TetherError):
        store.add_task(pid, "x", for_surface="slack")
    t = store.add_task(pid, "x")
    with pytest.raises(TetherError):
        store.update_task(t["id"], status="finished")
    with pytest.raises(TetherError):
        parse_ref("x12")


def test_handoff_flow(store, project):
    pid = project["id"]
    t = store.add_task(pid, "build page", for_surface="code")
    h = store.add_handoff(pid, "code", "Page mockup", "see link", ["https://x"], [f"t{t['id']}"],
                          surface="design")
    assert [x["id"] for x in store.handoffs(pid, to_surface="code")] == [h["id"]]
    assert store.handoffs(pid, to_surface="design") == []
    store.update_handoff(h["id"], "done", "implemented", surface="code")
    assert store.handoffs(pid) == []


def test_handoff_rejects_foreign_task(store, project):
    other = store.create_project("other")
    t = store.add_task(other["id"], "x")
    with pytest.raises(TetherError, match="another project"):
        store.add_handoff(project["id"], "code", "x", task_ids=[t["id"]])


def test_search_covers_history(store, project):
    pid = project["id"]
    t = store.add_task(pid, "Add Stripe checkout")
    store.update_task(t["id"], status="done")
    d = store.add_decision(pid, "Use Stripe", "fees ok")
    store.add_decision(pid, "Use Paddle", "handles VAT", supersedes=d["id"])
    store.note(pid, "Stripe account approved")
    kinds = sorted(k for k, _ in store.search(pid, "stripe"))
    assert kinds == ["decisions", "events", "tasks"]
    assert store.search(pid, "stripe checkout")[0][1]["id"] == t["id"]


def test_seen_marker(store, project):
    pid = project["id"]
    assert store.last_seen(pid, "code") is None
    store.mark_seen(pid, "code")
    first = store.last_seen(pid, "code")
    store.add_task(pid, "x")
    assert store.events(pid, after_id=first)[0]["summary"] == "added task: x"


def test_change_listener(store, project):
    seen = []
    store.on_change(seen.append)
    store.add_task(project["id"], "x")
    assert seen == [project["id"]]
