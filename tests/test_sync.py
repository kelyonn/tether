"""Two 'Macs' = two databases sharing one sync folder, each with its own machine id."""

import json

import pytest

from tether.store import Store


@pytest.fixture
def macs(tmp_path):
    shared = tmp_path / "iCloud" / "Tether"
    a = Store(tmp_path / "a.db", sync_dir=shared, machine="mac-a")
    b = Store(tmp_path / "b.db", sync_dir=shared, machine="mac-b")
    yield a, b, shared
    a.close()
    b.close()


def sync(*stores):
    for _ in range(2):  # twice, so changes made while merging also travel
        for s in stores:
            s.flush()
            s.pull()


def test_everything_travels_with_the_same_ids(macs):
    a, b, _ = macs
    p = a.create_project("Query", "NL to SQL", "/Users/k/projects/query", "code")
    t = a.add_task(p["id"], "Add guard tests", "", "code", 1, "code")
    a.update_task(t["id"], status="doing", surface="code")
    d = a.add_decision(p["id"], "Read-only by default", "safety", "chat")
    a.add_decision(p["id"], "Allow writes with --write", "", "chat", supersedes=d["id"])
    a.add_memory("Reading app", "quotes", "reading", "chat", None, "idea", "swipe to save")
    a.add_handoff(p["id"], "code", "Table spec", "50 rows", [], [t["id"]], "design")
    sync(a, b)
    bp = b.find_project("query")
    assert bp["goal"] == "NL to SQL" and bp["root_path"] is None  # folder not on this Mac
    bt = b.task(t["id"])
    assert bt["title"] == "Add guard tests" and bt["status"] == "doing"
    assert [x["text"] for x in b.decisions(bp["id"])] == ["Allow writes with --write"]
    assert b.get(f"d{d['id']}")[1]["superseded_by"] == d["id"] + 1
    assert b.memories()[0]["summary"] == "swipe to save"
    assert json.loads(b.handoffs(bp["id"])[0]["task_ids"]) == [t["id"]]
    assert any("Add guard tests" in e["summary"] for e in b.events(bp["id"]))


def test_concurrent_edits_to_different_fields_both_survive(macs):
    a, b, _ = macs
    p = a.create_project("App")
    t = a.add_task(p["id"], "old title")
    sync(a, b)
    a.update_task(t["id"], status="done", note="shipped")   # Mac A, offline
    b.update_task(t["id"], title="new title")               # Mac B, offline
    sync(a, b)
    for s in (a, b):
        row = s.task(t["id"])
        assert (row["title"], row["status"], row["note"]) == ("new title", "done", "shipped")


def test_same_field_last_writer_wins_and_converges(macs):
    a, b, _ = macs
    p = a.create_project("App")
    sync(a, b)
    a.update_project(p["id"], status="from A")
    b.update_project(p["id"], status="from B")  # later clock
    sync(a, b)
    assert a.project(p["id"])["status"] == b.project(p["id"])["status"] == "from B"


def test_same_project_created_on_both_macs_merges(macs):
    a, b, _ = macs
    pa = a.create_project("Query")
    a.add_task(pa["id"], "task from A")
    pb = b.create_project("Query")
    b.add_task(pb["id"], "task from B")
    sync(a, b)
    for s in (a, b):
        assert [r["slug"] for r in s.projects()] == ["query"]
        titles = {t["title"] for t in s.tasks(s.find_project("query")["id"])}
        assert titles == {"task from A", "task from B"}


def test_id_clash_keeps_both_records(macs):
    a, b, _ = macs
    p = a.create_project("App")
    sync(a, b)
    a.add_task(p["id"], "A's task")
    b.add_task(p["id"], "B's task")  # same local id on both Macs
    sync(a, b)
    for s in (a, b):
        assert sorted(t["title"] for t in s.tasks(p["id"])) == ["A's task", "B's task"]


def test_imports_are_not_echoed(macs):
    a, b, shared = macs
    a.create_project("App")
    sync(a, b)
    assert not (shared / "journals" / "mac-b.jsonl").exists()


def test_half_synced_line_waits(macs, tmp_path):
    a, b, shared = macs
    a.create_project("App")
    journal = shared / "journals" / "mac-a.jsonl"
    full = journal.read_text()
    journal.write_text(full + '{"ts":"9999999999999.00000.mac-a","t":"projects","u":"zz"')
    b.pull()
    assert [r["slug"] for r in b.projects()] == ["app"]  # complete lines applied, tail ignored


def test_existing_data_is_shared_on_setup(tmp_path):
    shared = tmp_path / "s"
    a = Store(tmp_path / "a.db")  # sync off
    a.create_project("Before sync")
    a.close()
    a = Store(tmp_path / "a.db", sync_dir=shared, machine="mac-a")
    a.syncer.queue_everything()
    a.flush()
    b = Store(tmp_path / "b.db", sync_dir=shared, machine="mac-b")
    assert b.find_project("before-sync")


def test_sync_failure_never_breaks_writes(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    s = Store(tmp_path / "a.db", sync_dir=blocker, machine="m")
    p = s.create_project("Still works")
    assert p["slug"] == "still-works" and "push failed" in (s.sync_error or "")


def test_reference_to_a_row_later_in_the_same_batch(macs):
    """Ops can reference a row whose own op comes later in the batch (e.g. a decision
    superseded by one exported after it). The reference must still resolve."""
    a, b, shared = macs
    p = a.create_project("X")
    d1 = a.add_decision(p["id"], "one")
    a.add_decision(p["id"], "two", supersedes=d1["id"])
    journal = shared / "journals" / "mac-a.jsonl"
    lines = journal.read_text().splitlines()
    # move the d2 insert after the d1 update, keeping clocks so d1's update sorts first
    ops = [json.loads(x) for x in lines]
    d2 = next(o for o in ops if o["t"] == "decisions" and o["f"].get("text") == "two")
    upd = next(o for o in ops if o["t"] == "decisions" and "superseded_by" in o["f"])
    d2["ts"], upd["ts"] = upd["ts"], d2["ts"]
    journal.write_text("".join(json.dumps(o) + "\n" for o in ops))
    b.pull()
    two = b.db.execute("SELECT id FROM decisions WHERE text='two'").fetchone()[0]
    assert b.get(f"d{d1['id']}")[1]["superseded_by"] == two


def test_events_remember_which_mac(macs):
    a, b, _ = macs
    p = a.create_project("App")
    sync(a, b)
    assert {e["machine"] for e in b.events(b.find_project("app")["id"])} == {"mac-a"}


def test_cli_sync_setup_and_status(tmp_path, capsys):
    from tether.cli import main
    assert main(["init", "demo", "--unbound"]) == 0
    assert main(["sync", "setup", "--dir", str(tmp_path / "shared")]) == 0
    out = capsys.readouterr().out
    assert "Sync on" in out and "Shared" in out
    assert list((tmp_path / "shared" / "journals").glob("*.jsonl"))
    assert main(["sync", "status"]) == 0
    assert "peer: none yet" in capsys.readouterr().out
    assert main(["sync", "off"]) == 0


def test_joining_mac_does_not_send_back_what_it_received(macs):
    a, b, shared = macs
    a.create_project("App")
    b.pull()
    b.syncer.queue_everything()
    assert b.flush() == 0


def test_folder_bindings_stay_per_mac(macs, tmp_path, monkeypatch):
    a, b, _ = macs
    real = tmp_path / "a-side" / "query"
    real.mkdir(parents=True)
    a.create_project("Query", "", str(real))
    a.create_project("Elsewhere", "", "/Volumes/not-here/elsewhere")
    sync(a, b)
    assert b.find_project("query")["root_path"] == str(real.resolve())   # exists here too
    assert b.find_project("elsewhere")["root_path"] is None              # doesn't exist here
    other = tmp_path / "b-side" / "elsewhere"
    other.mkdir(parents=True)
    b.update_project(b.find_project("elsewhere")["id"], root_path=str(other))
    sync(a, b)
    assert a.find_project("elsewhere")["root_path"] == "/Volumes/not-here/elsewhere"


def test_init_binds_synced_project_instead_of_duplicating(tmp_path, monkeypatch):
    from tether.cli import main
    from tether.store import Store
    s = Store()
    s.create_project("query")  # as if it arrived from the other Mac, unbound here
    s.close()
    folder = tmp_path / "code" / "query"
    folder.mkdir(parents=True)
    monkeypatch.chdir(folder)
    assert main(["init"]) == 0
    s = Store()
    assert [r["slug"] for r in s.projects()] == ["query"]
    assert s.project_for_path(folder)["slug"] == "query"
