import io
import json
import time

from tether import hooks
from tether.cli import main
from tether.ui import snapshot


def activity(store, pid):
    return [e["summary"] for e in store.events(pid, limit=50) if e["kind"] == "code-activity"]


def clock(monkeypatch, start=1_000_000.0):
    now = [start]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def test_first_edit_is_logged_at_once_with_a_project_relative_name(store, project):
    root = project["root_path"]
    assert store.record_code_edit(project["id"], "s1", f"{root}/assets/logo.svg", root) is True
    assert activity(store, project["id"]) == ["edited assets/logo.svg"]


def test_a_burst_of_edits_is_batched_not_spammed(store, project, monkeypatch):
    now = clock(monkeypatch)
    root = project["root_path"]
    pid = project["id"]
    store.record_code_edit(pid, "s1", f"{root}/a.svg", root)
    for i, name in enumerate(["b.svg", "c.svg", "b.svg", "d.svg", "e.svg"]):
        now[0] += 20
        assert store.record_code_edit(pid, "s1", f"{root}/{name}", root) is False   # within 5 minutes
    assert len(activity(store, pid)) == 1
    now[0] += 400
    assert store.record_code_edit(pid, "s1", f"{root}/f.svg", root) is True
    assert activity(store, pid)[0] == "edited 5 files: b.svg, c.svg, d.svg and 2 more"
    assert len(activity(store, pid)) == 2


def test_each_session_is_throttled_separately(store, project):
    root = project["root_path"]
    store.record_code_edit(project["id"], "s1", f"{root}/a.txt", root)
    assert store.record_code_edit(project["id"], "s2", f"{root}/b.txt", root) is True
    assert len(activity(store, project["id"])) == 2


def test_git_and_dependency_folders_are_ignored(store, project):
    root = project["root_path"]
    for p in (f"{root}/.git/index", f"{root}/node_modules/x/y.js"):
        assert store.record_code_edit(project["id"], "s1", p, root) is False
    assert activity(store, project["id"]) == []


def test_files_outside_the_project_are_recorded_by_name_only(store, project):
    store.record_code_edit(project["id"], "s1", "/Users/someone/secret-folder/notes.txt", project["root_path"])
    assert activity(store, project["id"]) == ["edited notes.txt"]


def test_recent_edits_mark_the_project_live_and_it_fades(store, project, monkeypatch):
    root = project["root_path"]
    store.record_code_edit(project["id"], "s1", f"{root}/a.txt", root)
    row = next(p for p in store.projects() if p["id"] == project["id"])
    assert row["code_active"] == 1 and snapshot(store)["projects"][0]["code_live"] is True
    monkeypatch.setattr(type(store), "LIVE_WINDOW", -1)           # as if the window had passed
    row = next(p for p in store.projects() if p["id"] == project["id"])
    assert row["code_active"] == 0 and snapshot(store)["projects"][0]["code_live"] is False


def run_hook(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return hooks.post_edit()


def test_hook_records_an_edit_in_a_bound_folder(store, project, monkeypatch):
    root = project["root_path"]
    assert run_hook(monkeypatch, {"tool_name": "Edit", "cwd": root, "session_id": "abc123",
                                  "tool_input": {"file_path": f"{root}/logo.svg"}}) == 0
    assert activity(store, project["id"]) == ["edited logo.svg"]


def test_hook_handles_every_edit_tool_and_notebooks(store, project, monkeypatch):
    root = project["root_path"]
    for i, (tool, key) in enumerate([("Write", "file_path"), ("MultiEdit", "file_path"),
                                     ("NotebookEdit", "notebook_path")]):
        run_hook(monkeypatch, {"tool_name": tool, "cwd": root, "session_id": f"s{i}",
                               "tool_input": {key: f"{root}/f{i}"}})
    assert len(activity(store, project["id"])) == 3


def test_hook_ignores_other_tools_and_unbound_folders(store, project, monkeypatch, tmp_path):
    root = project["root_path"]
    run_hook(monkeypatch, {"tool_name": "Bash", "cwd": root, "tool_input": {"command": "ls"}})
    run_hook(monkeypatch, {"tool_name": "Edit", "cwd": str(tmp_path / "elsewhere"),
                           "tool_input": {"file_path": "/x/y.txt"}})
    run_hook(monkeypatch, {"tool_name": "Edit", "cwd": root, "tool_input": {}})
    assert activity(store, project["id"]) == []


def test_hook_never_fails_on_bad_input(store, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hooks.post_edit() == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Edit", "cwd": 5,
                                                              "tool_input": {"file_path": "/a"}})))
    assert hooks.post_edit() == 0


def test_cli_hook_entry_point(store, project, monkeypatch):
    root = project["root_path"]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Write", "cwd": root, "session_id": "z", "tool_input": {"file_path": f"{root}/n.md"}})))
    assert main(["hook", "post-edit"]) == 0
    assert activity(store, project["id"]) == ["edited n.md"]
