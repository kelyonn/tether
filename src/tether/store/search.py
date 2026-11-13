"""Cross-surface lookup: recall (everything) and search (one project)."""

from __future__ import annotations

import sqlite3

from tether.errors import TetherError
from tether.store.base import StoreBase
from tether.store.schema import *
from tether.store.schema import _days, _jaccard, _kind, _surface, _tags, _words  # noqa: F401


class SearchMixin(StoreBase):
    def recall(self, query: str, limit: int = 15) -> list[tuple[str, sqlite3.Row, str | None]]:
        """Search memories and every project's records. Returns (kind, row, project slug).
        This is the cross-surface lookup: a chat that doesn't know the project uses it.
        If the live results don't fill `limit`, idle-archived items that match are pulled
        back out of the archive and included."""
        words = [w for w in query.split() if w]
        if not words:
            raise TetherError("search query is empty")
        out = self._recall_live(query, words, limit)
        if len(out) < limit and self._restore_matching(query, words, min(MAX_RESTORE, limit - len(out))):
            out = self._recall_live(query, words, limit)
        for kind, row, _slug in out:
            if kind == "memories":
                self.touch("memories", row["id"])
            elif kind == "projects":
                self.touch("projects", row["id"])
            elif "project_id" in row.keys() and row["project_id"]:
                self.touch("projects", row["project_id"])
        return out

    def _recall_live(self, query: str, words: list[str], limit: int
                     ) -> list[tuple[str, sqlite3.Row, str | None]]:
        like = [f"%{w}%" for w in words]
        out: list[tuple[str, sqlite3.Row, str | None]] = []
        cond = " AND ".join("(m.title || ' ' || m.body || ' ' || m.tags) LIKE ?" for _ in words)
        for r in self.db.execute(
                f"SELECT m.*, p.slug AS project_slug FROM memories m LEFT JOIN projects p "
                f"ON p.id=m.project_id WHERE m.archived=0 AND {cond} "
                f"ORDER BY m.updated_at DESC LIMIT ?", (*like, limit)):
            out.append(("memories", r, r["project_slug"]))
        pcond = " AND ".join("(p.name || ' ' || p.goal || ' ' || p.status || ' ' || p.brief) "
                             "LIKE ?" for _ in words)
        for r in self.db.execute(f"SELECT p.* FROM projects p WHERE p.archived=0 AND {pcond} "
                                 "LIMIT ?", (*like, limit)):
            out.append(("projects", r, r["slug"]))
        for r in self.projects():
            out.extend((t, row, r["slug"]) for t, row in self.search(r["id"], query, limit)
                       )
        return out[:limit]

    def search(self, pid: int, query: str, limit: int = 20) -> list[tuple[str, sqlite3.Row]]:
        """Case-insensitive match of every word in `query`, across all record types,
        including closed tasks and superseded decisions (this is the history lookup)."""
        words = [w for w in query.split() if w]
        if not words:
            raise TetherError("search query is empty")
        out: list[tuple[str, sqlite3.Row]] = []
        specs = [("decisions", "text || ' ' || why"),
                 ("tasks", "title || ' ' || detail || ' ' || note"),
                 ("handoffs", "title || ' ' || body || ' ' || note")]
        for table, expr in specs:
            cond = " AND ".join(f"({expr}) LIKE ?" for _ in words)
            rows = self.db.execute(
                f"SELECT * FROM {table} WHERE project_id=? AND {cond} ORDER BY id DESC LIMIT ?",
                (pid, *[f"%{w}%" for w in words], limit)).fetchall()
            out.extend((table, r) for r in rows)
        cond = " AND ".join("(title || ' ' || body || ' ' || tags) LIKE ?" for _ in words)
        mems = self.db.execute(
            f"SELECT * FROM memories WHERE project_id=? AND archived=0 AND {cond} "
            "ORDER BY id DESC LIMIT ?", (pid, *[f"%{w}%" for w in words], limit)).fetchall()
        out.extend(("memories", r) for r in mems)
        cond = " AND ".join("summary LIKE ?" for _ in words)
        notes = self.db.execute(
            f"SELECT * FROM events WHERE project_id=? AND kind='note' AND {cond} "
            "ORDER BY id DESC LIMIT ?", (pid, *[f"%{w}%" for w in words], limit)).fetchall()
        out.extend(("events", r) for r in notes)
        return out[:limit]
