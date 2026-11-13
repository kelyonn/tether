"""The live view's HTTP layer: what it serves, who may write, and what each edit does."""

import http.client
import json
import threading

import pytest

from tether.store import Store
from tether.ui import make_server


@pytest.fixture
def live(store):
    httpd, token = make_server(store, 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1], token, store
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body)
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, dict(r.getheaders()), raw


def act(port, token, body):
    return call(port, "POST", "/api/act", body, {"X-Tether-Token": token})


def test_page_has_token_and_security_headers(live):
    port, token, _ = live
    status, headers, raw = call(port, "GET", "/")
    assert status == 200
    assert token.encode() in raw and b"__TOKEN__" not in raw
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"


def test_foreign_host_header_is_refused(live):
    port, _, _ = live
    for path in ("/", "/api/state", "/api/marker"):
        status, _, _ = call(port, "GET", path, headers={"Host": "evil.example.com"})
        assert status == 403


def test_write_needs_the_token(live):
    port, token, store = live
    p = store.create_project("Shop", "sell mugs")
    body = {"action": "add_task", "project": "shop", "title": "ship it"}
    assert call(port, "POST", "/api/act", body)[0] == 403
    assert call(port, "POST", "/api/act", body, {"X-Tether-Token": "wrong"})[0] == 403
    assert call(port, "POST", "/api/act", body, {"X-Tether-Token": token,
                                                 "Host": "evil.example.com"})[0] == 403
    assert store.tasks(p["id"]) == []
    assert act(port, token, body)[0] == 200
    assert [t["title"] for t in store.tasks(p["id"])] == ["ship it"]


def test_state_lists_only_active_projects_and_marker_changes(live):
    port, token, store = live
    store.create_project("Alpha", "a")
    _, _, m1 = call(port, "GET", "/api/marker")
    status, _, raw = call(port, "GET", "/api/state")
    assert status == 200 and [p["slug"] for p in json.loads(raw)["projects"]] == ["alpha"]
    act(port, token, {"action": "add_task", "project": "alpha", "title": "x"})
    assert call(port, "GET", "/api/marker")[2] != m1


def test_edits_task_status_decision_status_and_goal(live):
    port, token, store = live
    p = store.create_project("Shop", "sell mugs")
    t = store.add_task(p["id"], "build page")
    assert act(port, token, {"action": "task_status", "id": t["id"], "status": "doing"})[0] == 200
    assert store.task(t["id"])["status"] == "doing"
    assert act(port, token, {"action": "decide", "project": "shop", "text": "Use Stripe",
                             "why": "fast"})[0] == 200
    assert [d["text"] for d in store.decisions(p["id"])] == ["Use Stripe"]
    assert act(port, token, {"action": "set_status", "project": "shop", "text": "page done"})[0] == 200
    assert act(port, token, {"action": "set_goal", "project": "shop", "text": "new goal"})[0] == 200
    row = store.project(p["id"])
    assert (row["status"], row["goal"]) == ("page done", "new goal")


def test_idea_capture_attach_edit_and_archive(live):
    port, token, store = live
    p = store.create_project("Shop", "sell mugs")
    assert act(port, token, {"action": "add_memory", "title": "Mug subscription"})[0] == 200
    m = store.memories()[0]
    assert m["project_id"] is None
    act(port, token, {"action": "attach", "id": m["id"], "project": "shop"})
    assert store.memory(m["id"])["project_id"] == p["id"]
    act(port, token, {"action": "edit_memory", "id": m["id"], "title": "Mug club", "body": "monthly"})
    assert store.memory(m["id"])["title"] == "Mug club"
    act(port, token, {"action": "archive_memory", "id": m["id"]})
    assert store.memories() == []
    act(port, token, {"action": "archive_memory", "id": m["id"], "archived": False})
    assert len(store.memories()) == 1


def test_bad_requests_return_400_not_a_crash(live):
    port, token, _ = live
    assert act(port, token, {"action": "nope"})[0] == 400
    assert act(port, token, {"action": "task_status"})[0] == 400          # missing keys
    assert act(port, token, {"action": "add_task", "project": "ghost", "title": "x"})[0] == 400
    assert act(port, token, {"action": "add_task", "project": "ghost", "title": "x"})[0] == 400
    assert call(port, "POST", "/api/other", {}, {"X-Tether-Token": token})[0] == 404
    assert call(port, "GET", "/missing")[0] == 404


def test_secrets_are_refused_through_the_page_too(live):
    port, token, store = live
    store.create_project("Shop", "x")
    status, _, raw = act(port, token, {"action": "set_status", "project": "shop",
                                       "text": "key sk-ant-api03-" + "a" * 40})
    assert status == 400 and b"secret" in raw.lower()


def test_search_endpoint(live):
    port, _, store = live
    store.add_memory("Sailing app", "tides and knots")
    status, _, raw = call(port, "GET", "/api/search?q=sailing")
    assert status == 200 and json.loads(raw)[0]["title"] == "Sailing app"
    assert json.loads(call(port, "GET", "/api/search?q=")[2]) == []


def test_already_running_detects_a_live_server(live):
    from tether.ui import already_running
    port, _, _ = live
    assert already_running(port) is True


def test_make_server_uses_the_store_it_is_given(tmp_path):
    s = Store(tmp_path / "other.db")
    httpd, _ = make_server(s, 0)
    assert httpd.server_address[0] == "127.0.0.1"
    httpd.server_close()
    s.close()
