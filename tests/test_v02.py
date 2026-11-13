import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from tether import apply as apply_mod
from tether import hooks, inboxfmt, install, ui
from tether.store import Store, TetherError


def run_hook(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return hooks.post_tool_use()


def test_post_tool_use_syncs_code_tasks(store, project, monkeypatch):
    base = {"cwd": project["root_path"], "session_id": "abcdef123456"}
    run_hook(monkeypatch, {**base, "tool_name": "TaskCreate",
                           "tool_input": {"subject": "Fix SQL guard", "description": "d"},
                           "tool_response": {"task": {"id": "1", "subject": "Fix SQL guard"}}})
    run_hook(monkeypatch, {**base, "tool_name": "TaskUpdate",
                           "tool_input": {"taskId": "1", "status": "in_progress"}})
    t = store.session_tasks(project["id"])
    assert len(t) == 1 and t[0]["status"] == "doing" and t[0]["kind"] == "session"
    assert store.tasks(project["id"]) == []  # not in the project list
    run_hook(monkeypatch, {**base, "tool_name": "TaskUpdate",
                           "tool_input": {"taskId": "1", "status": "completed"}})
    assert store.session_tasks(project["id"]) == []
    assert store.recent_session_tasks(project["id"])[0]["status"] == "done"


def test_hook_ignores_unbound_folders_and_other_tools(store, project, tmp_path, monkeypatch):
    run_hook(monkeypatch, {"cwd": str(tmp_path), "tool_name": "TaskCreate",
                           "tool_input": {"subject": "x"}, "tool_response": {"task": {"id": "1"}}})
    run_hook(monkeypatch, {"cwd": project["root_path"], "tool_name": "Bash", "tool_input": {}})
    assert store.recent_session_tasks(project["id"]) == []


def test_todowrite_replaces_list(store, project, monkeypatch):
    base = {"cwd": project["root_path"], "session_id": "s1", "tool_name": "TodoWrite"}
    run_hook(monkeypatch, {**base, "tool_input": {"todos": [
        {"content": "A", "status": "in_progress"}, {"content": "B", "status": "pending"}]}})
    run_hook(monkeypatch, {**base, "tool_input": {"todos": [
        {"content": "A", "status": "completed"}]}})
    by = {t["title"]: t["status"] for t in store.recent_session_tasks(project["id"])}
    assert by == {"A": "done", "B": "dropped"}


def test_pack_shows_code_tasks_not_their_events(store, project):
    store.sync_session_task(project["id"], "s", "1", "Wire the hook", None, "in_progress")
    from tether.render import context_pack
    pack = context_pack(store, project["id"], "code")
    assert "Claude Code's own unfinished tasks" in pack and "Wire the hook" in pack
    assert "Code task:" not in pack and "working on" not in pack.split("Open tasks")[0]


def test_promote(store, project):
    t = store.sync_session_task(project["id"], "s", "1", "Keep me", None, "pending")
    store.promote_task(t["id"])
    assert [x["title"] for x in store.tasks(project["id"])] == ["Keep me"]
    with pytest.raises(TetherError):
        store.promote_task(t["id"])


def test_install_adds_both_hooks_and_uninstalls(tmp_path):
    install.install_hook()
    hooks_cfg = json.loads(install.claude_settings_path().read_text())["hooks"]
    assert set(hooks_cfg) == {"SessionStart", "PostToolUse"}
    assert hooks_cfg["PostToolUse"][0]["matcher"] == "TaskCreate|TaskUpdate|TodoWrite"
    install.install_hook()  # idempotent
    assert len(json.loads(install.claude_settings_path().read_text())["hooks"]["PostToolUse"]) == 1
    install.uninstall_hook()
    assert "hooks" not in json.loads(install.claude_settings_path().read_text())


def test_secrets_blocked_in_store(store, project):
    key = "ghp_" + "a" * 36
    for call in (lambda: store.add_task(project["id"], f"use {key}"),
                 lambda: store.add_decision(project["id"], "x", f"token {key}"),
                 lambda: store.note(project["id"], "password=hunter2hunter2"),
                 lambda: store.add_memory("k", "-----BEGIN RSA PRIVATE KEY-----")):
        with pytest.raises(TetherError, match="never stores secrets"):
            call()
    store.add_task(project["id"], "rotate the API key")  # words alone are fine


def test_similar_and_suggest(store):
    p = store.create_project("Query", "Natural language SQL over local databases")
    m = store.add_memory("Query SQL guard ideas", "block destructive statements in generated SQL",
                         "sql,query")
    assert store.similar_memories("Query SQL guard", "destructive statements")[0][1]["id"] == m["id"]
    assert store.similar_memories("Gardening tips")  == []
    guess = store.suggest_project(m)
    assert guess and guess[0]["id"] == p["id"]


def test_inbox_import(store, tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    (d / "reading-app.md").write_text(
        "---\ntitle: Reading app\nkind: spec\ntags: reading, Mobile\nsummary: Quotes\n---\nswipe")
    (d / "loose.txt").write_text("# Pizza oven\nbuild one")
    (d / "skip.png").write_bytes(b"x")
    out = inboxfmt.import_folder(store, d)
    assert len(out) == 2 and (d / "imported" / "loose.txt").exists() and (d / "skip.png").exists()
    m = {r["title"]: r for r in store.memories()}
    assert m["Reading app"]["kind"] == "spec" and m["Reading app"]["tags"] == "reading,mobile"
    assert "Pizza oven" in m
    # same file again merges instead of duplicating
    (d / "again.md").write_text("---\ntitle: Reading app\n---\nmore")
    assert "merged" in inboxfmt.import_folder(store, d)[0]
    assert len(store.memories()) == 2


def test_unknown_project_guess_stays_unfiled(store):
    out = inboxfmt.save(store, inboxfmt.parse("---\ntitle: T\nproject: ghost\n---\nb"))
    assert "saved" in out and store.memory(1)["project_id"] is None


def test_apply_task_why_not_in_title(store, project):
    parsed = apply_mod.parse("TETHER UPDATE my-app\nTASK code: Add tests | why safety\nEND")
    apply_mod.apply(store, project["id"], parsed, "chat")
    t = store.tasks(project["id"])[0]
    assert t["title"] == "Add tests" and "safety" in t["detail"]


def test_v2_database_migrates_to_latest(tmp_path):
    from tether.store import MIGRATIONS, SCHEMA, SCHEMA_VERSION
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA + MIGRATIONS[0] + "; PRAGMA user_version=2;")
    con.execute("INSERT INTO projects (slug, name, created_at, updated_at) VALUES ('a','a','x','x')")
    con.commit()
    con.close()
    s = Store(db)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s.add_memory("a", kind="spec")["kind"] == "spec"
    assert s.project(1)["uid"]  # existing rows got a sync uid


def test_memory_inbox_sinks_stale(store):
    m = store.add_memory("old idea")
    store.db.execute("UPDATE memories SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                     (m["id"],))
    store.add_memory("fresh idea")
    box = store.inbox()
    assert [x["title"] for x in box["fresh"]] == ["fresh idea"]
    assert [x["title"] for x in box["older"]] == ["old idea"]


# -- live view -----------------------------------------------------------------------

def test_ui_snapshot_and_act(store, project):
    mem = store.add_memory("Idea", "b")
    store.sync_session_task(project["id"], "s", "1", "Code thing", None, "in_progress")
    snap = ui.snapshot(store)
    p = snap["projects"][0]
    assert p["code_doing"] == 1 and p["code_tasks"][0]["title"] == "Code thing"
    assert snap["inbox"][0]["id"] == mem["id"]
    t = store.add_task(project["id"], "do it")
    ui.act(store, {"action": "task_status", "id": t["id"], "status": "done"})
    ui.act(store, {"action": "attach", "id": mem["id"], "project": project["slug"]})
    ui.act(store, {"action": "decide", "project": project["slug"], "text": "Use X"})
    assert store.task(t["id"])["status"] == "done"
    assert store.memory(mem["id"])["project_id"] == project["id"]
    with pytest.raises(TetherError):
        ui.act(store, {"action": "nope"})
    assert "Tether" in ui.render_watch(ui.snapshot(store))


def test_ui_http_requires_token_and_local_host(tmp_path, monkeypatch):
    import http.server
    monkeypatch.setattr("webbrowser.open", lambda *_: None)
    holder = {}
    orig = http.server.ThreadingHTTPServer.serve_forever

    def capture(self, *a, **k):
        holder["srv"] = self
        orig(self, *a, **k)
    monkeypatch.setattr(http.server.ThreadingHTTPServer, "serve_forever", capture)
    port = 17878
    th = threading.Thread(target=ui.serve, args=(port, False), daemon=True)
    th.start()
    for _ in range(100):
        if "srv" in holder:
            break
        threading.Event().wait(0.05)
    base = f"http://127.0.0.1:{port}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        token = page.split('const TOKEN = "')[1].split('"')[0]
        assert len(token) > 20
        urllib.request.urlopen(base + "/api/state").read()

        def post(headers):
            req = urllib.request.Request(base + "/api/act", b'{"action":"nope"}', headers)
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code
        assert post({}) == 403
        assert post({"X-Tether-Token": "wrong"}) == 403
        assert post({"X-Tether-Token": token}) == 400  # authorised, but unknown action
        assert urllib.request.urlopen(base + "/api/marker").status == 200
        assert json.loads(urllib.request.urlopen(base + "/api/search?q=zzz").read()) == []
        req = urllib.request.Request(base + "/api/state", headers={"Host": "evil.example"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
    finally:
        holder["srv"].shutdown()


def test_service_plist_and_launchctl_calls(monkeypatch, tmp_path):
    import plistlib
    import sys as _sys

    from tether import service
    if _sys.platform != "darwin":
        pytest.skip("launchd is macOS only")
    calls = []

    class R:
        returncode, stdout, stderr = 0, "state = running", ""

    monkeypatch.setattr(service.subprocess, "run", lambda cmd, **k: calls.append(cmd) or R())
    path = service.install(7999)
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == service.LABEL and data["KeepAlive"] is True
    assert data["ProgramArguments"][-3:] == ["--no-open", "--port", "7999"]
    assert data["EnvironmentVariables"]["TETHER_HOME"]  # the test's isolated store
    assert ["bootstrap" in c for c in calls].count(True) == 1
    assert service.status() == "running"
    assert service.uninstall() and not path.exists()


def test_ui_already_running_detection():
    assert ui.already_running(1) is False
