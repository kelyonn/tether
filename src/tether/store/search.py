"""Cross-surface lookup: recall (everything) and search (one project)."""

from __future__ import annotations

import sqlite3
from typing import Any

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

    def _recall_like(self, query: str, words: list[str], limit: int
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
            out.extend((t, row, r["slug"]) for t, row in self._search_like(r["id"], query, limit))
        return out[:limit]

    def search(self, pid: int, query: str, limit: int = 20) -> list[tuple[str, sqlite3.Row]]:
        """Every word in `query` must match (as a word start), across all record types,
        including closed tasks and superseded decisions (this is the history lookup).
        Best matches first; anything the word index misses is topped up by substring match."""
        words = [w for w in query.split() if w]
        if not words:
            raise TetherError("search query is empty")
        out: list[tuple[str, sqlite3.Row]] = []
        seen: set[tuple[str, int]] = set()
        for kind, rid in self._fts_hits(words, limit * 3, pid):
            row = self.db.execute(f"SELECT * FROM {kind} WHERE id=?", (rid,)).fetchone()
            if row is None or kind == "projects" or (kind == "memories" and row["archived"]):
                continue
            out.append((kind, row))
            seen.add((kind, rid))
        out.extend(x for x in self._search_like(pid, query, limit) if (x[0], x[1]["id"]) not in seen)
        return out[:limit]

    def _fts_hits(self, words: list[str], limit: int, pid: int | None = None) -> list[tuple[str, int]]:
        """(kind, id) of the best full-text matches, ranked. Empty if there is no FTS5 index."""
        match = " ".join('"' + w.replace('"', '""') + '"*' for w in words)
        sql = "SELECT kind, ref FROM search_idx WHERE search_idx MATCH ?"
        args: list[Any] = [match]
        if pid is not None:
            sql += " AND pid = ?"
            args.append(pid)
        sql += " ORDER BY bm25(search_idx) LIMIT ?"
        try:
            return [(r["kind"], r["ref"]) for r in self.db.execute(sql, (*args, limit))]
        except sqlite3.OperationalError:
            return []          # SQLite without FTS5, or a query it cannot parse

    def _recall_live(self, query: str, words: list[str], limit: int
                     ) -> list[tuple[str, sqlite3.Row, str | None]]:
        out: list[tuple[str, sqlite3.Row, str | None]] = []
        seen: set[tuple[str, int]] = set()
        for kind, rid in self._fts_hits(words, limit * 4):
            if kind == "memories":
                row = self.db.execute(
                    "SELECT m.*, p.slug AS project_slug FROM memories m LEFT JOIN projects p "
                    "ON p.id=m.project_id WHERE m.id=? AND m.archived=0", (rid,)).fetchone()
                slug = row["project_slug"] if row else None
            elif kind == "projects":
                row = self.db.execute("SELECT * FROM projects WHERE id=? AND archived=0", (rid,)).fetchone()
                slug = row["slug"] if row else None
            else:
                row = self.db.execute(f"SELECT * FROM {kind} WHERE id=?", (rid,)).fetchone()
                owner = (self.db.execute("SELECT slug FROM projects WHERE id=? AND archived=0",
                                         (row["project_id"],)).fetchone() if row else None)
                slug = owner["slug"] if owner else None
                row = row if owner else None
            if row is not None:
                out.append((kind, row, slug))
                seen.add((kind, rid))
        out.extend(x for x in self._recall_like(query, words, limit) if (x[0], x[1]["id"]) not in seen)
        return out[:limit]

    def _search_like(self, pid: int, query: str, limit: int) -> list[tuple[str, sqlite3.Row]]:
        words = [w for w in query.split() if w]
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
