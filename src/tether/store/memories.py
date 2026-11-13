"""Memories (ideas saved from any surface) and the inbox."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from tether import secrets
from tether.errors import TetherError
from tether.store.base import StoreBase
from tether.store.schema import *
from tether.store.schema import _days, _jaccard, _kind, _surface, _tags, _words  # noqa: F401


class MemoriesMixin(StoreBase):
    def add_memory(self, title: str, body: str = "", tags: str = "", surface: str = "cli",
                   pid: int | None = None, kind: str = "idea",
                   summary: str = "") -> sqlite3.Row:
        title = title.strip()
        if not title:
            raise TetherError("memory title is required")
        secrets.check(title, body)
        ts = now()
        with self.tx():
            cur = self.db.execute(
                "INSERT INTO memories (project_id, title, body, tags, surface, kind, summary, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, title, body.strip(), _tags(tags), surface, _kind(kind),
                 summary.strip()[:200], ts, ts))
            mid = cast(int, cur.lastrowid)
            if pid:
                self._log(pid, surface, "memory", f"m{mid}", f"saved memory: {title}")
        if pid:
            self._changed(pid)
        return self.memory(mid)

    def memory(self, mid: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        if not row:
            raise TetherError(f"no memory m{mid}")
        return row

    def update_memory(self, mid: int, *, title: str | None = None, body: str | None = None,
                      append: str | None = None, tags: str | None = None,
                      kind: str | None = None, summary: str | None = None,
                      surface: str = "cli") -> sqlite3.Row:
        m = self.memory(mid)
        secrets.check(title, body, append, summary)
        fields: dict[str, Any] = {}
        if title is not None and title.strip():
            fields["title"] = title.strip()
        if body is not None:
            fields["body"] = body.strip()
        if append:
            base = fields.get("body", m["body"])
            fields["body"] = (base + "\n\n" + append.strip()).strip()
        if tags is not None:
            fields["tags"] = _tags(tags)
        if kind:
            fields["kind"] = _kind(kind)
        if summary is not None:
            fields["summary"] = summary.strip()[:200]
        if not fields:
            return m
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx():
            self.db.execute(f"UPDATE memories SET {sets} WHERE id=?", (*fields.values(), mid))
            if m["project_id"]:
                self._log(m["project_id"], surface, "memory", f"m{mid}",
                          f"updated memory: {fields.get('title', m['title'])}")
        if m["project_id"]:
            self._changed(m["project_id"])
        return self.memory(mid)

    def attach_memory(self, mid: int, pid: int, surface: str = "cli") -> sqlite3.Row:
        m = self.memory(mid)
        with self.tx():
            self.db.execute("UPDATE memories SET project_id=?, updated_at=? WHERE id=?",
                            (pid, now(), mid))
            self._log(pid, surface, "memory", f"m{mid}", f"attached memory: {m['title']}")
        self._changed(pid)
        return self.memory(mid)

    def archive_memory(self, mid: int, archived: bool = True) -> sqlite3.Row:
        self.memory(mid)
        with self.tx():
            self.db.execute("UPDATE memories SET archived=?, updated_at=? WHERE id=?",
                            (int(archived), now(), mid))
        return self.memory(mid)

    def memories(self, pid: int | None = None, unfiled: bool = False, limit: int = 20,
                 include_archived: bool = False) -> list[sqlite3.Row]:
        """Newest first. pid=None and unfiled=False lists everything."""
        sql = ("SELECT m.*, p.slug AS project_slug FROM memories m "
               "LEFT JOIN projects p ON p.id = m.project_id WHERE 1=1")
        args: list[Any] = []
        if unfiled:
            sql += " AND m.project_id IS NULL"
        elif pid is not None:
            sql += " AND m.project_id=?"
            args.append(pid)
        if not include_archived:
            sql += " AND m.archived=0"
        sql += " ORDER BY m.updated_at DESC, m.id DESC LIMIT ?"
        return self.db.execute(sql, (*args, limit)).fetchall()

    def similar_memories(self, title: str, body: str = "", tags: str = "",
                         pid: int | None = None) -> list[tuple[float, sqlite3.Row]]:
        """Existing memories that look like this one, best first, with a 0-1 score.
        >= 0.6 is a near-certain duplicate (merge it); >= 0.3 is worth asking about."""
        mine_title = _words(title)
        mine_all = _words(f"{title} {tags} {body[:400]}")
        out = []
        for m in self.db.execute("SELECT * FROM memories WHERE archived=0"):
            if pid is not None and m["project_id"] not in (None, pid):
                continue
            t = _jaccard(mine_title, _words(m["title"]))
            a = _jaccard(mine_all, _words(f"{m['title']} {m['tags']} {m['body'][:400]}"))
            score = max(t, a * 0.9)
            if score >= 0.3:
                out.append((round(score, 2), m))
        out.sort(key=lambda x: -x[0])
        return out[:3]

    def suggest_project(self, memory: sqlite3.Row) -> tuple[sqlite3.Row, float] | None:
        """The project an unfiled memory most likely belongs to, if any. Never attaches."""
        text = _words(f"{memory['title']} {memory['tags']} {memory['summary']} {memory['body'][:600]}")
        best: tuple[sqlite3.Row, float] | None = None
        for p in self.projects():
            name_words = _words(p["name"])
            hit = 1.0 if name_words and name_words <= text else 0.0
            ctx = _words(f"{p['goal']} {p['status']} {p['brief']}")
            overlap = len(text & ctx) / max(4, min(len(text), len(ctx))) if ctx else 0.0
            score = max(hit, overlap)
            if score >= 0.35 and (best is None or score > best[1]):
                best = (p, round(score, 2))
        return best

    def inbox(self, limit: int = 20) -> dict[str, list[sqlite3.Row]]:
        """Unfiled memories split into fresh and older (untouched for STALE_DAYS+)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)).isoformat(timespec="seconds")
        rows = self.memories(unfiled=True, limit=200)
        fresh = [m for m in rows if m["updated_at"] >= cutoff]
        older = [m for m in rows if m["updated_at"] < cutoff]
        return {"fresh": fresh[:limit], "older": older[:limit]}
