"""Sync under the faults a shared folder really produces: half-written files, damaged
lines, files that vanish or are not downloaded yet, and journals that arrive late."""

import os

from tests.test_sync import macs, sync  # noqa: F401  (the two-Mac fixture)

from tether.store import Store


def journal(shared, machine):
    return shared / "journals" / f"{machine}.jsonl"


def test_a_half_written_last_line_waits_and_is_then_applied_once(macs):  # noqa: F811
    a, b, shared = macs
    a.create_project("Alpha", "first")
    a.flush()
    a.add_memory("Second idea", "body")
    a.flush()
    path = journal(shared, "mac-a")
    full = path.read_bytes()
    lines = full.splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:-1]) + lines[-1][: len(lines[-1]) // 2])   # torn write
    b.pull()
    assert b.find_project("alpha") is not None
    assert b.memories() == []                       # the torn op is not applied...
    assert b.sync_error is None                      # ...and is not treated as an error
    path.write_bytes(full)                           # the rest of the file arrives
    b.pull()
    assert [m["title"] for m in b.memories()] == ["Second idea"]
    b.pull()
    assert len(b.memories()) == 1                    # and never twice


def test_a_damaged_line_in_the_middle_is_skipped_not_fatal(macs):  # noqa: F811
    a, b, shared = macs
    a.create_project("Alpha", "first")
    a.flush()
    a.add_memory("Before", "x")
    a.flush()
    with open(journal(shared, "mac-a"), "ab") as f:
        f.write(b"{this is not json\n")
    a.add_memory("After", "y")
    a.flush()
    b.pull()
    assert {m["title"] for m in b.memories()} == {"Before", "After"}
    assert b.sync_error is None


def test_a_journal_that_vanishes_or_cannot_be_read_does_not_break_reads(macs):  # noqa: F811
    a, b, shared = macs
    a.create_project("Alpha", "first")
    a.flush()
    b.pull()
    path = journal(shared, "mac-a")
    saved = path.read_bytes()
    path.unlink()                                    # iCloud evicted it
    assert b.pull() == 0
    assert b.projects()                              # local data is untouched
    path.write_bytes(saved)
    os.chmod(path, 0)                                # present but unreadable
    try:
        assert b.pull() == 0
    finally:
        os.chmod(path, 0o644)
    b.pull()
    assert b.sync_error is None


def test_a_clipped_own_journal_is_healed_and_can_be_rebuilt(macs, tmp_path):  # noqa: F811
    a, b, shared = macs
    a.create_project("Alpha", "first")
    for i in range(5):
        a.add_memory(f"Idea {i}", "x")
    a.flush()
    b.pull()
    path = journal(shared, "mac-a")
    path.write_bytes(path.read_bytes()[:20])         # the cloud clipped it mid-header
    assert a.syncer.check_own_journal() is not None
    a.add_memory("Idea 6", "x")                      # the next write must not be swallowed
    a.flush()
    b.pull()
    assert "Idea 6" in {m["title"] for m in b.memories(limit=50)}
    # old ops are gone from the file, but the database has them: rebuild from it
    a.syncer.compact()
    assert a.syncer.check_own_journal() is None
    c = Store(tmp_path / "c.db", sync_dir=shared, machine="mac-c")
    try:
        c.pull()
        assert len(c.memories(limit=50)) == 6
    finally:
        c.close()


def test_a_late_arriving_journal_catches_up_in_one_pull(macs):  # noqa: F811
    a, b, shared = macs
    a.create_project("Alpha", "first")
    a.flush()
    held = journal(shared, "mac-a")
    parked = held.read_bytes()
    held.unlink()                                    # not downloaded yet
    b.pull()
    assert b.find_project("alpha") is None
    held.write_bytes(parked)                         # arrives
    b.pull()
    assert b.find_project("alpha") is not None


def test_offline_edits_on_both_macs_converge_after_reconnecting(macs):  # noqa: F811
    a, b, _ = macs
    p = a.create_project("Alpha", "first")
    t = a.add_task(p["id"], "write docs")
    sync(a, b)
    a.update_task(t["id"], status="doing", surface="code")             # offline on A
    b.update_task(b.find_project("alpha") and t["id"], title="write the docs", surface="chat")
    sync(a, b)
    ta, tb = a.task(t["id"]), b.task(t["id"])
    assert (ta["status"], ta["title"]) == (tb["status"], tb["title"]) == ("doing", "write the docs")


def test_a_second_store_on_the_same_machine_id_keeps_working(tmp_path):
    shared = tmp_path / "shared"
    s1 = Store(tmp_path / "one.db", sync_dir=shared, machine="mac-a")
    s1.create_project("Alpha", "x")
    s1.flush()
    s2 = Store(tmp_path / "one.db", sync_dir=shared, machine="mac-a")      # CLI + server at once
    try:
        s2.add_memory("From the second process", "x")
        s2.flush()
        assert s1.find_project("alpha") is not None
    finally:
        s1.close()
        s2.close()
