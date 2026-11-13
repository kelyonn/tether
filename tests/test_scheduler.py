import http.client
import json
import threading
import time

import pytest

from tether import config, github
from tether.scheduler import GithubScheduler
from tether.store import Store, TetherError
from tether.ui import make_server, snapshot


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def sched(sync, hours=1.0, **kw):
    return GithubScheduler(hours=lambda: hours, sync=sync, store_factory=lambda: Store(),
                           first_delay=kw.pop("first_delay", 0.01), retry_after=kw.pop("retry_after", 0.05), **kw)


# -- the setting ------------------------------------------------------------------------

def test_default_is_several_times_a_day():
    assert github.sync_hours() == 3.0


def test_the_interval_can_be_changed_clamped_or_turned_off():
    config.save_config({"github_sync_hours": 6})
    assert github.sync_hours() == 6.0
    config.save_config({"github_sync_hours": 0.01})
    assert github.sync_hours() == github.MIN_SYNC_HOURS
    config.save_config({"github_sync_hours": 0})
    assert github.sync_hours() == 0.0
    config.save_config({"github_sync_hours": "soon"})
    assert github.sync_hours() == 3.0


# -- recording what happened ------------------------------------------------------------

def test_a_real_sync_records_when_it_finished(store, tmp_path):
    ok = lambda args: (0, json.dumps({"data": {"viewer": {"login": "k"}}}))
    store.create_project("Plain", "g", str(tmp_path))
    with pytest.raises(TetherError):                       # no repo to sync: that is a failure
        github.sync(store, gh=ok)
    assert github.get_state(store, "github_error") and github.get_state(store, "github_running") == "0"
    assert github.get_state(store, "github_last_sync") is None


def test_success_records_the_time_and_clears_the_error(store, tmp_path, monkeypatch):
    monkeypatch.setattr(github, "_sync", lambda *a, **k: [])
    github.set_state(store, "github_error", "old failure")
    github.sync(store)
    assert github.get_state(store, "github_error") == ""
    assert abs(int(github.get_state(store, "github_last_sync")) - time.time()) < 5


def test_a_dry_run_records_nothing(store, monkeypatch):
    monkeypatch.setattr(github, "_sync", lambda *a, **k: [])
    github.sync(store, dry_run=True)
    assert github.get_state(store, "github_last_sync") is None


# -- the scheduler ----------------------------------------------------------------------

def test_runs_soon_after_start_then_repeats():
    calls = []
    s = sched(lambda st: calls.append(1), hours=0.0003)         # ~1 second
    s.start()
    try:
        assert wait_for(lambda: len(calls) >= 1)
    finally:
        s.stop()
    assert not s._thread.is_alive()


def test_does_not_run_automatically_when_turned_off_but_run_now_still_works():
    calls = []
    s = sched(lambda st: calls.append(1), hours=0.0)
    s.start()
    try:
        time.sleep(0.2)
        assert calls == []
        assert s.run_now() is True
        assert wait_for(lambda: calls == [1])
    finally:
        s.stop()


def test_a_failure_does_not_kill_the_thread_and_is_retried_sooner():
    calls = []

    def flaky(store):
        calls.append(1)
        if len(calls) < 3:
            raise TetherError("boom")
    s = sched(flaky, hours=1.0, retry_after=0.05)
    s.start()
    try:
        assert wait_for(lambda: len(calls) >= 3)               # retried every 0.05s, not every hour
    finally:
        s.stop()


def test_run_now_refuses_while_a_sync_is_running():
    gate = threading.Event()
    started = threading.Event()

    def slow(store):
        started.set()
        gate.wait(5)
    s = sched(slow, hours=1.0)
    s.start()
    try:
        assert started.wait(5)
        assert s.run_now() is False
    finally:
        gate.set()
        s.stop()


# -- the page and its API ---------------------------------------------------------------

@pytest.fixture
def server(store):
    class Fake:
        calls = 0

        def run_now(self):
            Fake.calls += 1
            return True
    httpd, token = make_server(store, 0, Fake())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], token, Fake
    httpd.shutdown()
    httpd.server_close()


def post(port, token, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/act", json.dumps(body), {"X-Tether-Token": token,
                                                      "Content-Type": "application/json"})
    r = c.getresponse()
    return r.status, json.loads(r.read())


def test_sync_now_button_reaches_the_scheduler(server):
    port, token, fake = server
    assert post(port, token, {"action": "github_sync"}) == (200, {"ok": True, "started": True})
    assert fake.calls == 1


def test_sync_now_without_a_scheduler_says_so(store):
    httpd, token = make_server(store, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body = post(httpd.server_address[1], token, {"action": "github_sync"})
        assert status == 400 and "not running" in body["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_snapshot_shows_sync_state_and_the_marker_follows_it(store):
    before = store.change_marker()
    assert snapshot(store)["github"] == {"last": None, "running": False, "error": None, "hours": 3.0}
    github.set_state(store, "github_last_sync", "1700000000")
    github.set_state(store, "github_error", "gh is not logged in")
    gh = snapshot(store)["github"]
    assert gh["last"] == 1700000000 and gh["error"] == "gh is not logged in"
    assert store.change_marker() != before
