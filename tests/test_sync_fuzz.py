"""Seeded randomized test: 2-3 Macs share a journal folder, make random edits, sync in
random order, compact at random, then sync to quiescence and must hold identical data."""

import random
import time

import pytest

from tether import sync as sync_mod
from tether.store import Store

NAMES = ["Alpha", "Beta", "Gamma", "Delta", "Query App"]  # small pool: same-slug merges happen
STATUSES = ["todo", "doing", "done", "blocked"]


def snapshot(store):
    """Synced content keyed by uid, local ids and per-Mac root_path removed. Projects are
    keyed by slug (two Macs creating the same project merge into one row on each side,
    keeping different uids), so project references use the slug too."""
    sy = store.syncer
    slug = {r["id"]: r["slug"] for r in store.db.execute("SELECT id, slug FROM projects")}
    out = {}
    for tbl in sync_mod.TABLES:
        rows = {}
        for row in store.db.execute(f"SELECT * FROM {tbl}").fetchall():
            d = sy._export(tbl, row)
            d.pop("root_path", None)
            if "project_id" in d:
                d["project_id"] = slug.get(row["project_id"])
            key = row["slug"] if tbl == "projects" else row["uid"]
            rows[key] = d
        out[tbl] = rows
    return out


def do_op(rng, s, n):
    projects = s.db.execute("SELECT id FROM projects").fetchall()
    pids = [r[0] for r in projects]
    tasks = [r[0] for r in s.db.execute("SELECT id FROM tasks")]
    mems = [r[0] for r in s.db.execute("SELECT id FROM memories")]
    decs = [r[0] for r in s.db.execute("SELECT id FROM decisions")]
    op = rng.choice(["project", "task", "task", "memory", "decision", "status", "title",
                     "attach", "archive", "mem_edit"])
    if op == "project" or not pids:
        s.create_project(rng.choice(NAMES), f"goal {n}")
    elif op == "task":
        s.add_task(rng.choice(pids), f"task {n}", f"detail {n}")
    elif op == "memory":
        s.add_memory(f"mem {n}", f"body {n}", "x", pid=rng.choice([None, rng.choice(pids)]))
    elif op == "decision":
        sup = rng.choice(decs) if decs and rng.random() < 0.3 else None
        pid = rng.choice(pids)
        if sup is not None:
            pid = s.db.execute("SELECT project_id FROM decisions WHERE id=?", (sup,)).fetchone()[0]
        s.add_decision(pid, f"decision {n}", "why", supersedes=sup)
    elif op == "status" and tasks:
        s.update_task(rng.choice(tasks), status=rng.choice(STATUSES), note=f"note {n}")
    elif op == "title" and tasks:
        s.update_task(rng.choice(tasks), title=f"renamed {n}")
    elif op == "attach" and mems:
        s.attach_memory(rng.choice(mems), rng.choice(pids))
    elif op == "mem_edit" and mems:
        s.update_memory(rng.choice(mems), body=f"edit {n}")
    elif op == "archive":
        if mems and rng.random() < 0.5:
            s.archive_memory(rng.choice(mems), rng.random() < 0.7)
        else:
            s.update_project(rng.choice(pids), archived=rng.random() < 0.7)


class FakeTime:
    """Deterministic wall clock for sync.py: advances 0-3 ms per read, so same-millisecond
    ties (broken by the logical counter and machine id) are exercised on every run."""

    def __init__(self, rng):
        self.rng, self.now = rng, 1_800_000_000.0

    def time(self):
        self.now += self.rng.randint(0, 3) / 1000
        return self.now

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def run(tmp_path, seed, n_macs, monkeypatch):
    rng = random.Random(seed)
    monkeypatch.setattr(sync_mod, "time", FakeTime(random.Random(seed + 1)))
    shared = tmp_path / "shared"
    macs = [Store(tmp_path / f"{i}.db", sync_dir=shared, machine=f"mac-{i}")
            for i in range(n_macs)]
    try:
        for n in range(rng.randint(15, 40)):
            s = rng.choice(macs)
            roll = rng.random()
            if roll < 0.6:
                do_op(rng, s, n)
            elif roll < 0.8:
                s.flush()
                s.pull()
            elif roll < 0.9:
                s.pull()
            elif roll < 0.95:
                s.flush()
            else:
                s.syncer.compact()
            if rng.random() < 0.03:
                s.syncer.compact()
        for _ in range(4):  # quiescence
            for s in rng.sample(macs, len(macs)):
                s.flush()
                s.pull()
        snaps = [snapshot(s) for s in macs]
        for other in snaps[1:]:
            assert other == snaps[0], f"seed {seed}: Macs diverged"
        assert all(s.syncer.status()["pending"] == 0 for s in macs)
    finally:
        for s in macs:
            s.close()


@pytest.mark.parametrize("n_macs", [2, 3])
@pytest.mark.parametrize("seed", range(50))
def test_macs_converge(tmp_path, seed, n_macs, monkeypatch):
    run(tmp_path, seed, n_macs, monkeypatch)
