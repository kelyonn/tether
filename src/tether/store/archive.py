"""Auto-archive: archive idle memories and projects, restore them on lookup."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from tether.store.base import StoreBase
from tether.store.schema import *
from tether.store.schema import _days, _jaccard, _kind, _surface, _tags, _words  # noqa: F401


class ArchiveMixin(StoreBase):
    def touch(self, tbl: str, rid: int) -> None:
        try:
            self.db.execute(
                "INSERT INTO usage (tbl, rid, last_used) VALUES (?,?,?) "
                "ON CONFLICT(tbl, rid) DO UPDATE SET last_used=excluded.last_used",
                (tbl, rid, now()))
        except sqlite3.OperationalError:
            pass            # a busy or read-only database must not break a lookup

    def _restore(self, tbl: str, rid: int) -> None:
        with self.tx():
            self.db.execute(f"UPDATE {tbl} SET archived=0 WHERE id=? AND archived=?",
                            (rid, AUTO_ARCHIVED))
            self.db.execute(
                "INSERT INTO usage (tbl, rid, last_used, restored) VALUES (?,?,?,1) "
                "ON CONFLICT(tbl, rid) DO UPDATE SET last_used=excluded.last_used, restored=1",
                (tbl, rid, now()))

    def _use_project(self, row: sqlite3.Row) -> sqlite3.Row:
        if row["archived"] == AUTO_ARCHIVED:
            self._restore("projects", row["id"])
            row = self.project(row["id"])
        self.touch("projects", row["id"])
        return row

    def _restore_matching(self, query: str, words: list[str], room: int) -> int:
        """Restore up to `room` idle-archived memories/projects matching the query."""
        like = [f"%{w}%" for w in words]
        n = 0
        cond = " AND ".join("(title || ' ' || body || ' ' || tags) LIKE ?" for _ in words)
        for m in self.db.execute(
                f"SELECT id FROM memories WHERE archived=? AND {cond} "
                "ORDER BY updated_at DESC LIMIT ?", (AUTO_ARCHIVED, *like, room)).fetchall():
            self._restore("memories", m["id"])
            n += 1
        pcond = " AND ".join("(name || ' ' || goal || ' ' || status || ' ' || brief) LIKE ?"
                             for _ in words)
        for p in self.db.execute("SELECT id FROM projects WHERE archived=?",
                                 (AUTO_ARCHIVED,)).fetchall():
            if n >= room:
                break
            hit = self.db.execute(f"SELECT 1 FROM projects WHERE id=? AND {pcond}",
                                  (p["id"], *like)).fetchone() or self.search(p["id"], query, 1)
            if hit:
                self._restore("projects", p["id"])
                n += 1
        return n

    def _idle_ids(self, tbl: str, activity: str, cutoff: str, re_cutoff: str) -> list[int]:
        # activity: SQL giving the newest sign of life other than a lookup, e.g. updated_at
        sql = (f"SELECT x.id FROM {tbl} x LEFT JOIN usage u ON u.tbl='{tbl}' AND u.rid=x.id "
               f"WHERE x.archived=0 AND max(coalesce(u.last_used,''), {activity}) < "
               "CASE WHEN coalesce(u.restored,0)=1 THEN ? ELSE ? END")
        return [r["id"] for r in self.db.execute(sql, (re_cutoff, cutoff))]

    def archive_idle(self, dry_run: bool = False) -> dict[str, list[int]]:
        """Archive every memory and project unused past the limits. Returns the ids."""
        after = _days("archive_after_days", ARCHIVE_AFTER_DAYS)
        again = _days("rearchive_after_days", REARCHIVE_AFTER_DAYS)
        found: dict[str, list[int]] = {"memories": [], "projects": []}
        if after <= 0:
            return found
        iso = lambda d: (datetime.now(timezone.utc) - timedelta(days=d)  # noqa: E731
                         ).isoformat(timespec="seconds")
        cutoff, re_cutoff = iso(after), iso(min(again, after) if again > 0 else after)
        found["memories"] = self._idle_ids(
            "memories", "x.updated_at, x.created_at", cutoff, re_cutoff)
        found["projects"] = self._idle_ids(
            "projects",
            "x.updated_at, x.created_at, "
            "coalesce((SELECT max(ts) FROM events e WHERE e.project_id=x.id),''), "
            "coalesce((SELECT max(updated_at) FROM tasks t WHERE t.project_id=x.id),''), "
            "coalesce((SELECT max(updated_at) FROM handoffs h WHERE h.project_id=x.id),''), "
            "coalesce((SELECT max(updated_at) FROM memories m WHERE m.project_id=x.id),'')",
            cutoff, re_cutoff)
        if not dry_run and (found["memories"] or found["projects"]):
            with self.tx():
                for tbl, ids in found.items():
                    for i in ids:
                        self.db.execute(f"UPDATE {tbl} SET archived=? WHERE id=?",
                                        (AUTO_ARCHIVED, i))
        return found

    def maybe_sweep(self) -> dict[str, list[int]] | None:
        """archive_idle at most once per SWEEP_EVERY_HOURS (called when the store opens)."""
        row = self.db.execute("SELECT value FROM local_state WHERE key='last_sweep'").fetchone()
        if row and row["value"] > (datetime.now(timezone.utc) - timedelta(
                hours=SWEEP_EVERY_HOURS)).isoformat(timespec="seconds"):
            return None
        found = self.archive_idle()
        self.db.execute("INSERT INTO local_state (key, value) VALUES ('last_sweep', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now(),))
        return found
