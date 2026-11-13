"""Decisions, tasks and handoffs (including Claude Code's session tasks)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from tether import secrets
from tether.errors import TetherError
from tether.store.base import StoreBase
from tether.store.schema import *
from tether.store.schema import _days, _jaccard, _kind, _surface, _tags, _words  # noqa: F401


class RecordsMixin(StoreBase):
    def add_decision(self, pid: int, text: str, why: str = "", surface: str = "cli",
                     supersedes: str | int | None = None) -> sqlite3.Row:
        text = text.strip()
        if not text:
            raise TetherError("decision text is required")
        secrets.check(text, why)
        old_id = None
        if supersedes not in (None, ""):
            old_id = supersedes if isinstance(supersedes, int) else (
                int(supersedes) if str(supersedes).isdigit() else parse_ref(supersedes)[1])
            old = self.db.execute("SELECT * FROM decisions WHERE id=? AND project_id=?",
                                  (old_id, pid)).fetchone()
            if not old:
                raise TetherError(f"no decision d{old_id} in this project")
        with self.tx():
            cur = self.db.execute(
                "INSERT INTO decisions (project_id, text, why, surface, created_at) "
                "VALUES (?,?,?,?,?)", (pid, text, why.strip(), surface, now()))
            did = cast(int, cur.lastrowid)
            if old_id:
                self.db.execute("UPDATE decisions SET status='superseded', superseded_by=? "
                                "WHERE id=?", (did, old_id))
            suffix = f" (replaces d{old_id})" if old_id else ""
            self._log(pid, surface, "decision", f"d{did}", f"decided: {text}{suffix}")
        self._changed(pid)
        return self.db.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()

    def decisions(self, pid: int, include_superseded: bool = False,
                  limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM decisions WHERE project_id=?"
        if not include_superseded:
            sql += " AND status='active'"
        sql += " ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.db.execute(sql, (pid,)).fetchall()

    def add_task(self, pid: int, title: str, detail: str = "", for_surface: str = "any",
                 priority: int = 2, surface: str = "cli") -> sqlite3.Row:
        title = title.strip()
        if not title:
            raise TetherError("task title is required")
        secrets.check(title, detail)
        for_surface = _surface(for_surface)
        priority = min(3, max(1, int(priority)))
        ts = now()
        with self.tx():
            cur = self.db.execute(
                "INSERT INTO tasks (project_id, title, detail, for_surface, priority, "
                "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (pid, title, detail.strip(), for_surface, priority, surface, ts, ts))
            tid = cast(int, cur.lastrowid)
            self._log(pid, surface, "task", f"t{tid}", f"added task: {title}")
        self._changed(pid)
        return self.task(tid)

    def task(self, tid: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            raise TetherError(f"no task t{tid}")
        return row

    def update_task(self, tid: int, *, status: str | None = None, note: str | None = None,
                    title: str | None = None, for_surface: str | None = None,
                    priority: int | None = None, surface: str = "cli") -> sqlite3.Row:
        task = self.task(tid)
        secrets.check(note, title)
        fields: dict[str, Any] = {}
        if status is not None:
            status = status.strip().lower()
            if status not in TASK_STATUSES:
                raise TetherError(f"status must be one of {', '.join(TASK_STATUSES)}")
            fields["status"] = status
            fields["closed_at"] = now() if status in ("done", "dropped") else None
        if note is not None:
            fields["note"] = note.strip()
        if title is not None:
            fields["title"] = title.strip()
        if for_surface is not None:
            fields["for_surface"] = _surface(for_surface)
        if priority is not None:
            fields["priority"] = min(3, max(1, int(priority)))
        if not fields:
            return task
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx():
            self.db.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), tid))
            verb = {"done": "completed", "dropped": "dropped", "doing": "started",
                    "blocked": "blocked on", "todo": "reopened"}.get(status or "", "updated")
            extra = f" — {note.strip()}" if note else ""
            self._log(task["project_id"], surface, "task", f"t{tid}",
                      f"{verb} t{tid}: {fields.get('title', task['title'])}{extra}")
        self._changed(task["project_id"])
        return self.task(tid)

    def tasks(self, pid: int, open_only: bool = True, kind: str = "project") -> list[sqlite3.Row]:
        sql = "SELECT * FROM tasks WHERE project_id=? AND kind=?"
        if open_only:
            sql += " AND status IN ('todo','doing','blocked')"
        # doing first, then blocked, then todo; high priority first; oldest first.
        sql += (" ORDER BY CASE status WHEN 'doing' THEN 0 WHEN 'blocked' THEN 1 "
                "WHEN 'todo' THEN 2 ELSE 3 END, priority, id")
        return self.db.execute(sql, (pid, kind)).fetchall()

    def task_counts(self, pid: int) -> dict[str, int]:
        rows = self.db.execute("SELECT status, count(*) n FROM tasks WHERE project_id=? "
                               "AND kind='project' GROUP BY status", (pid,)).fetchall()
        counts = dict.fromkeys(TASK_STATUSES, 0)
        counts.update({r["status"]: r["n"] for r in rows})
        return counts

    def add_handoff(self, pid: int, to_surface: str, title: str, body: str = "",
                    links: list[str] | None = None, task_ids: list[str | int] | None = None,
                    surface: str = "cli") -> sqlite3.Row:
        title = title.strip()
        if not title:
            raise TetherError("handoff title is required")
        secrets.check(title, body, *(links or []))
        to_surface = _surface(to_surface)
        ids = [parse_task_id(t) for t in (task_ids or [])]
        for tid in ids:
            if self.task(tid)["project_id"] != pid:
                raise TetherError(f"t{tid} belongs to another project")
        ts = now()
        with self.tx():
            cur = self.db.execute(
                "INSERT INTO handoffs (project_id, from_surface, to_surface, title, body, links, "
                "task_ids, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, surface, to_surface, title, body.strip(), json.dumps(links or []),
                 json.dumps(ids), ts, ts))
            hid = cast(int, cur.lastrowid)
            self._log(pid, surface, "handoff", f"h{hid}",
                      f"handed off to {to_surface}: {title}")
        self._changed(pid)
        return self.handoff(hid)

    def handoff(self, hid: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM handoffs WHERE id=?", (hid,)).fetchone()
        if not row:
            raise TetherError(f"no handoff h{hid}")
        return row

    def update_handoff(self, hid: int, status: str, note: str = "",
                       surface: str = "cli") -> sqlite3.Row:
        h = self.handoff(hid)
        secrets.check(note)
        status = status.strip().lower()
        if status not in HANDOFF_STATUSES:
            raise TetherError(f"status must be one of {', '.join(HANDOFF_STATUSES)}")
        with self.tx():
            self.db.execute("UPDATE handoffs SET status=?, note=?, updated_at=? WHERE id=?",
                            (status, note.strip() or h["note"], now(), hid))
            extra = f" — {note.strip()}" if note else ""
            self._log(h["project_id"], surface, "handoff", f"h{hid}",
                      f"{status} handoff h{hid}: {h['title']}{extra}")
        self._changed(h["project_id"])
        return self.handoff(hid)

    def handoffs(self, pid: int, open_only: bool = True,
                 to_surface: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM handoffs WHERE project_id=?"
        args: list[Any] = [pid]
        if open_only:
            sql += " AND status IN ('open','accepted')"
        if to_surface and to_surface != "any":
            sql += " AND to_surface IN (?, 'any')"
            args.append(to_surface)
        return self.db.execute(sql + " ORDER BY id", args).fetchall()

    def sync_session_task(self, pid: int, session_id: str, task_id: str,
                          subject: str | None = None, description: str | None = None,
                          status: str | None = None) -> sqlite3.Row:
        """Upsert one task from Claude Code's own task list (TaskCreate/TaskUpdate or
        TodoWrite). Keyed by session and Code's per-session task id."""
        key = f"{session_id}:{task_id}"
        mapped = CODE_STATUS.get((status or "").lower()) if status else None
        row = self.db.execute("SELECT * FROM tasks WHERE project_id=? AND ext_key=?",
                              (pid, key)).fetchone()
        ts = now()
        with self.tx():
            if row is None:
                title = (subject or f"Code task #{task_id}").strip()
                st = mapped or "todo"
                cur = self.db.execute(
                    "INSERT INTO tasks (project_id, title, detail, for_surface, status, kind, "
                    "ext_key, created_by, created_at, updated_at, closed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, title, (description or "").strip(), "code", st, "session", key,
                     "code", ts, ts, ts if st in ("done", "dropped") else None))
                tid = cast(int, cur.lastrowid)
                self._log(pid, "code", "session-task", f"t{tid}", f"Code task: {title}")
            else:
                tid = row["id"]
                fields: dict[str, Any] = {}
                if subject and subject.strip() != row["title"]:
                    fields["title"] = subject.strip()
                if description is not None and description.strip() != row["detail"]:
                    fields["detail"] = description.strip()
                if mapped and mapped != row["status"]:
                    fields["status"] = mapped
                    fields["closed_at"] = ts if mapped in ("done", "dropped") else None
                if fields:
                    fields["updated_at"] = ts
                    sets = ", ".join(f"{k}=?" for k in fields)
                    self.db.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), tid))
                    if "status" in fields:
                        verb = {"doing": "working on", "done": "finished", "dropped": "dropped",
                                "todo": "reopened"}[mapped or ""]
                        self._log(pid, "code", "session-task", f"t{tid}",
                                  f"Code {verb}: {fields.get('title', row['title'])}")
        self._changed(pid)
        return self.task(tid)

    def sync_external_task(self, pid: int, ext_key: str, title: str, detail: str = "",
                           priority: int = 2, for_surface: str = "any",
                           surface: str = "github") -> tuple[sqlite3.Row, str]:
        """Create or refresh a task that mirrors something outside Tether (a GitHub issue,
        a branch that needs pushing). Keyed by `ext_key`, so it is safe to run repeatedly.
        A task you dropped stays dropped; one that was closed because the outside thing
        went away comes back if the thing does. Returns (task, what happened)."""
        row = self.db.execute("SELECT * FROM tasks WHERE project_id=? AND ext_key=?",
                              (pid, ext_key)).fetchone()
        if row is None:
            task = self.add_task(pid, title, detail, for_surface, priority, surface)
            with self.tx():
                self.db.execute("UPDATE tasks SET ext_key=? WHERE id=?", (ext_key, task["id"]))
            return self.task(task["id"]), "added"
        if row["status"] == "dropped":
            return row, "unchanged"
        fields: dict[str, Any] = {}
        if title.strip() != row["title"]:
            fields["title"] = title.strip()
        if detail.strip() != row["detail"]:
            fields["detail"] = detail.strip()
        action = "updated" if fields else "unchanged"
        if row["status"] == "done":
            fields["status"], fields["closed_at"] = "todo", None
            action = "reopened"
        if not fields:
            return row, action
        secrets.check(*(v for k, v in fields.items() if k in ("title", "detail")))
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx():
            self.db.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), row["id"]))
            self._log(pid, surface, "task", f"t{row['id']}", f"{action} task: {title.strip()}")
        self._changed(pid)
        return self.task(row["id"]), action

    def close_external_tasks(self, pid: int, prefix: str, keep: set[str], note: str,
                             surface: str = "github") -> int:
        """Mark open tasks whose ext_key starts with `prefix` done when they are not in
        `keep` any more (the issue closed, the branch got pushed). Returns how many."""
        n = 0
        for row in self.db.execute(
                "SELECT * FROM tasks WHERE project_id=? AND ext_key LIKE ? AND status IN "
                "('todo','doing','blocked')", (pid, prefix + "%")).fetchall():
            if row["ext_key"] in keep:
                continue
            with self.tx():
                ts = now()
                self.db.execute("UPDATE tasks SET status='done', note=?, closed_at=?, updated_at=? "
                                "WHERE id=?", (note, ts, ts, row["id"]))
                self._log(pid, surface, "task", f"t{row['id']}", f"done: {row['title']} ({note})")
            n += 1
        if n:
            self._changed(pid)
        return n

    def drop_missing_session_tasks(self, pid: int, session_id: str, keep: set[str]) -> None:
        """TodoWrite sends the whole list each time: open items it no longer lists are gone."""
        rows = self.db.execute(
            "SELECT id, ext_key FROM tasks WHERE project_id=? AND kind='session' "
            "AND ext_key LIKE ? AND status IN ('todo','doing','blocked')",
            (pid, f"{session_id}:%")).fetchall()
        stale = [r["id"] for r in rows if r["ext_key"].split(":", 1)[1] not in keep]
        if stale:
            with self.tx():
                self.db.execute(
                    f"UPDATE tasks SET status='dropped', closed_at=?, updated_at=? "
                    f"WHERE id IN ({','.join('?' * len(stale))})", (now(), now(), *stale))

    def promote_task(self, tid: int, surface: str = "cli") -> sqlite3.Row:
        """Turn one of Code's own session tasks into a real project task."""
        t = self.task(tid)
        if t["kind"] != "session":
            raise TetherError(f"t{tid} is already a project task")
        with self.tx():
            self.db.execute("UPDATE tasks SET kind='project', ext_key=NULL, for_surface='any', "
                            "updated_at=? WHERE id=?", (now(), tid))
            self._log(t["project_id"], surface, "task", f"t{tid}", f"promoted t{tid}: {t['title']}")
        self._changed(t["project_id"])
        return self.task(tid)

    def session_tasks(self, pid: int, limit: int = 5) -> list[sqlite3.Row]:
        """Code's unfinished own tasks, most recently touched first."""
        return self.db.execute(
            "SELECT * FROM tasks WHERE project_id=? AND kind='session' "
            "AND status IN ('todo','doing','blocked') ORDER BY updated_at DESC, id DESC LIMIT ?",
            (pid, limit)).fetchall()

    def recent_session_tasks(self, pid: int, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM tasks WHERE project_id=? AND kind='session' "
            "ORDER BY updated_at DESC, id DESC LIMIT ?", (pid, limit)).fetchall()
