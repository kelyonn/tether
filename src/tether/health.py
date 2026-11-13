"""Health checks for the ledger, and repairs for the ones that can be repaired.

`tether doctor` prints these; `tether doctor --fix` applies the fixes. Every check only
reads. A fix never deletes user data: it rebuilds something derived (the search index,
this Mac's own sync journal) from the tables that hold the real data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tether.store import Store
from tether.store.schema import rebuild_fts


@dataclass
class Finding:
    name: str
    ok: bool
    detail: str = ""
    fix: Callable[[], str] | None = None      # present only when the problem can be repaired


def check(store: Store) -> list[Finding]:
    out: list[Finding] = []
    db = store.db

    rows = [r[0] for r in db.execute("PRAGMA integrity_check")]
    out.append(Finding("database integrity", rows == ["ok"], "; ".join(rows[:3]) if rows != ["ok"] else ""))

    fk = db.execute("PRAGMA foreign_key_check").fetchall()
    out.append(Finding("references between records", not fk,
                       f"{len(fk)} record(s) point at something that no longer exists" if fk else ""))

    bad = sum(db.execute(f"SELECT count(*) FROM {t} WHERE archived NOT IN (0, 1, 2)").fetchone()[0]
              for t in ("projects", "memories"))
    out.append(Finding("archive flags", not bad, f"{bad} record(s) with an unknown flag" if bad else ""))

    out.append(_search_index(store))

    if store.syncer:
        problem = store.syncer.check_own_journal()
        out.append(Finding("this Mac's sync journal", problem is None,
                           problem or "",
                           (lambda: _compact(store)) if problem else None))
        out.append(Finding("last sync", store.sync_error is None, store.sync_error or ""))
        pending = store.syncer.status()["pending"]
        out.append(Finding("changes waiting to sync", pending < 200,
                           f"{pending} not yet written to the shared folder" if pending >= 200 else ""))
    return out


def _search_index(store: Store) -> Finding:
    db = store.db
    has = db.execute("SELECT 1 FROM sqlite_master WHERE name='search_idx'").fetchone()
    if not has:
        return Finding("search index", True, "not available in this SQLite; using substring search")
    expected = sum(
        db.execute(f"SELECT count(*) FROM {t}{w}").fetchone()[0]
        for t, w in (("memories", ""), ("tasks", ""), ("decisions", ""), ("handoffs", ""),
                     ("projects", ""), ("events", " WHERE kind='note'")))
    actual = db.execute("SELECT count(*) FROM search_idx").fetchone()[0]
    ok = expected == actual
    return Finding("search index", ok,
                   "" if ok else f"{actual} entries for {expected} records",
                   None if ok else (lambda: f"rebuilt with {rebuild_fts(db)} entries"))


def _compact(store: Store) -> str:
    before, after = store.syncer.compact()
    return f"journal rewritten from the database ({before} -> {after} bytes)"


def repair(findings: list[Finding]) -> list[str]:
    """Run every available fix. Returns one line per repair made."""
    return [f"{f.name}: {f.fix()}" for f in findings if not f.ok and f.fix]
