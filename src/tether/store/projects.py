"""Projects: create, find, resolve and update."""

from __future__ import annotations

import os
import sqlite3
from typing import Any, cast

from tether import config, secrets
from tether.errors import TetherError
from tether.store.base import StoreBase
from tether.store.schema import *
from tether.store.schema import _days, _jaccard, _kind, _surface, _tags, _words  # noqa: F401


class ProjectsMixin(StoreBase):
    def create_project(self, name: str, goal: str = "", root_path: str | None = None,
                       surface: str = "cli") -> sqlite3.Row:
        name = name.strip()
        if not name:
            raise TetherError("project name is required")
        secrets.check(name, goal)
        base = slugify(name)
        slug, n = base, 2
        while self.db.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
            slug, n = f"{base}-{n}", n + 1
        ts = now()
        root = norm_path(root_path) if root_path else None
        with self.tx():
            cur = self.db.execute(
                "INSERT INTO projects (slug, name, goal, root_path, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)", (slug, name, goal.strip(), root, ts, ts))
            pid = cast(int, cur.lastrowid)
            self._log(pid, surface, "project", None, f"created project {name!r}")
        self._changed(pid)
        return self.project(pid)

    def project(self, pid: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise TetherError(f"no project with id {pid}")
        return row

    def require_project(self, ident: str) -> sqlite3.Row:
        """find_project, but a missing project is an error instead of None."""
        row = self.find_project(ident)
        if row is None:
            raise TetherError(f"no project named {ident!r}")
        return row

    def find_project(self, ident: str) -> sqlite3.Row | None:
        ident = str(ident).strip()
        if ident.isdigit():
            row = self.db.execute("SELECT * FROM projects WHERE id=?", (int(ident),)).fetchone()
            if row:
                return row
        return self.db.execute(
            "SELECT * FROM projects WHERE slug=? OR lower(name)=lower(?) ORDER BY archived LIMIT 1",
            (slugify(ident), ident)).fetchone()

    def projects(self, include_archived: bool = False) -> list[sqlite3.Row]:
        sql = """
            SELECT p.*,
              (SELECT count(*) FROM tasks t WHERE t.project_id=p.id AND t.kind='project'
                 AND t.status IN ('todo','doing','blocked')) AS open_tasks,
              (SELECT count(*) FROM tasks t WHERE t.project_id=p.id AND t.kind='session'
                 AND t.status='doing') AS code_doing,
              (SELECT count(*) FROM handoffs h WHERE h.project_id=p.id
                 AND h.status IN ('open','accepted')) AS open_handoffs,
              (SELECT max(ts) FROM events e WHERE e.project_id=p.id) AS last_active
            FROM projects p {where} ORDER BY last_active DESC"""
        where = "" if include_archived else "WHERE p.archived=0"
        return self.db.execute(sql.format(where=where)).fetchall()

    def project_for_path(self, path: str | os.PathLike[str]) -> sqlite3.Row | None:
        """The project whose root_path contains `path` (deepest match wins)."""
        target = norm_path(path)
        best = None
        for row in self.db.execute(
                "SELECT * FROM projects WHERE root_path IS NOT NULL AND archived IN (0, 2)"):
            root = row["root_path"]
            inside = target == root or target.startswith(root.rstrip(os.sep) + os.sep)
            if inside and (best is None or len(root) > len(best["root_path"])):
                best = row
        return self._use_project(best) if best else None

    def resolve_project(self, ident: str | None = None, cwd: str | None = None,
                        use_current: bool = True) -> sqlite3.Row:
        """Pick the project for a request (see _resolve_project). Picking it counts as
        using it: an idle-archived project comes back, and its 30-day clock restarts."""
        return self._use_project(self._resolve_project(ident, cwd, use_current))

    def _resolve_project(self, ident: str | None = None, cwd: str | None = None,
                         use_current: bool = True) -> sqlite3.Row:
        """Pick the project for a request.

        Order: explicit name/slug, then TETHER_PROJECT, then the project bound to cwd,
        then the 'current' project from config (set by `tether use` or the last
        explicit choice), then the only project if there is exactly one.

        MCP clients pass use_current=False: one Desktop server serves every chat, so a
        global "current project" would let an unrelated chat write into the wrong project.
        """
        self.pull(force=False)
        ident = ident or os.environ.get("TETHER_PROJECT")
        if ident:
            row = self.find_project(ident)
            if not row:
                raise TetherError(f"no project named {ident!r}. Known: {self._names()}")
            return row
        if cwd:
            row = self.project_for_path(cwd)
            if row:
                return row
        current = config.load_config().get("current_project") if use_current else None
        if current:
            row = self.find_project(current)
            if row and row["archived"] in (0, AUTO_ARCHIVED):
                return row
        rows = self.projects()
        if len(rows) == 1:
            return rows[0]
        if not rows:
            raise TetherError("no projects yet. Create one with start_project "
                              "(MCP) or `tether init` (CLI).")
        raise TetherError(f"which project? Pass project=... One of: {self._names()}")

    def _names(self) -> str:
        return ", ".join(r["slug"] for r in self.projects()) or "(none)"

    def update_project(self, pid: int, *, goal: str | None = None, status: str | None = None,
                       brief: str | None = None, name: str | None = None,
                       root_path: str | None = None, archived: bool | None = None,
                       surface: str = "cli") -> sqlite3.Row:
        secrets.check(goal, status, brief)
        fields: dict[str, Any] = {}
        if goal is not None:
            fields["goal"] = goal.strip()
        if status is not None:
            fields["status"] = status.strip()
        if brief is not None:
            fields["brief"] = brief.strip()
        if name is not None:
            fields["name"] = name.strip()
        if root_path is not None:
            fields["root_path"] = norm_path(root_path)
        if archived is not None:
            fields["archived"] = int(archived)
        if not fields:
            return self.project(pid)
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx():
            self.db.execute(f"UPDATE projects SET {sets} WHERE id=?", (*fields.values(), pid))
            changed = ", ".join(k for k in fields if k != "updated_at")
            summary = f"status: {status.strip()}" if status is not None else f"updated {changed}"
            self._log(pid, surface, "project", None, summary)
        self._changed(pid)
        return self.project(pid)
