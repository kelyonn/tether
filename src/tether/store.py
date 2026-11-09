"""The SQLite store: projects, decisions, tasks, handoffs and an event log.

One file is shared by every process (the Code server, the Desktop server, the CLI,
hooks), so it runs in WAL mode with a busy timeout and every write is one short
transaction.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from tether import config, secrets, sync
from tether.errors import TetherError

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    goal          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT '',
    brief         TEXT NOT NULL DEFAULT '',
    root_path     TEXT,
    archived      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    text          TEXT NOT NULL,
    why           TEXT NOT NULL DEFAULT '',
    surface       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | superseded
    superseded_by INTEGER REFERENCES decisions(id),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    title         TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    for_surface   TEXT NOT NULL DEFAULT 'any',      -- who should do it
    priority      INTEGER NOT NULL DEFAULT 2,       -- 1 high, 2 normal, 3 low
    status        TEXT NOT NULL DEFAULT 'todo',     -- todo | doing | blocked | done | dropped
    note          TEXT NOT NULL DEFAULT '',
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    closed_at     TEXT
);

CREATE TABLE IF NOT EXISTS handoffs (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    from_surface  TEXT NOT NULL,
    to_surface    TEXT NOT NULL,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT '',
    links         TEXT NOT NULL DEFAULT '[]',       -- JSON list of URLs / paths
    task_ids      TEXT NOT NULL DEFAULT '[]',       -- JSON list of task ids it covers
    status        TEXT NOT NULL DEFAULT 'open',     -- open | accepted | done | dropped
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    ts            TEXT NOT NULL,
    surface       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    ref           TEXT,
    summary       TEXT NOT NULL
);

-- Last event each surface has seen per project, for "since you were last here".
CREATE TABLE IF NOT EXISTS seen (
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    surface       TEXT NOT NULL,
    event_id      INTEGER NOT NULL,
    PRIMARY KEY (project_id, surface)
);

CREATE INDEX IF NOT EXISTS ix_tasks_open ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS ix_decisions_active ON decisions(project_id, status);
CREATE INDEX IF NOT EXISTS ix_handoffs_open ON handoffs(project_id, status);
CREATE INDEX IF NOT EXISTS ix_events_project ON events(project_id, id);
"""

# Applied in order to databases older than SCHEMA_VERSION. Index = target version - 2.
MIGRATIONS = [
    # v2: memories (ideas saved from any chat, project optional) and Claude Code's own
    # session tasks, synced by a hook and kept apart from project tasks.
    """
    ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'project';  -- project | session
    ALTER TABLE tasks ADD COLUMN ext_key TEXT;     -- '<session_id>:<task id>' for session tasks
    CREATE UNIQUE INDEX IF NOT EXISTS ix_tasks_ext ON tasks(project_id, ext_key)
        WHERE ext_key IS NOT NULL;
    CREATE TABLE IF NOT EXISTS memories (
        id          INTEGER PRIMARY KEY,
        project_id  INTEGER REFERENCES projects(id),   -- NULL = unfiled
        title       TEXT NOT NULL,
        body        TEXT NOT NULL DEFAULT '',
        tags        TEXT NOT NULL DEFAULT '',
        surface     TEXT NOT NULL,
        archived    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_memories_project ON memories(project_id, archived);
    """,
    # v3: what kind of memory it is, and a one-line summary for lists.
    """
    ALTER TABLE memories ADD COLUMN kind TEXT NOT NULL DEFAULT 'idea';
    ALTER TABLE memories ADD COLUMN summary TEXT NOT NULL DEFAULT '';
    """,
    # v4: two-Mac sync (uids, change-capture triggers, journal bookkeeping). See sync.py.
    sync.migration_sql(),
]
MEMORY_KINDS = ("idea", "research", "spec", "decision-log", "bug", "todo-list", "note")
STALE_DAYS = 60
STOPWORDS = frozenset("""a an and are as at be but by for from has have how i if in into is it its
of on or our so that the their then there this to was we what when which with you your app
project idea build make new use using""".split())

TASK_STATUSES = ("todo", "doing", "blocked", "done", "dropped")
OPEN_TASK_STATUSES = ("todo", "doing", "blocked")
HANDOFF_STATUSES = ("open", "accepted", "done", "dropped")

REF_RE = re.compile(r"^\s*([tdhm])#?(\d+)\s*$", re.IGNORECASE)
REF_TABLES = {"t": "tasks", "d": "decisions", "h": "handoffs", "m": "memories"}
CODE_STATUS = {"pending": "todo", "in_progress": "doing", "completed": "done",
               "deleted": "dropped", "cancelled": "dropped"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def norm_path(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def parse_ref(ref: str) -> tuple[str, int]:
    """'t12' -> ('tasks', 12). Accepts t/d/h prefixes, optional '#'."""
    m = REF_RE.match(str(ref))
    if not m:
        raise TetherError(f"bad ref {ref!r}: use t12 (task), d3 (decision), h2 (handoff) "
                          "or m4 (memory)")
    return REF_TABLES[m.group(1).lower()], int(m.group(2))


def parse_task_id(ref: str | int) -> int:
    if isinstance(ref, int):
        return ref
    if str(ref).strip().isdigit():
        return int(ref)
    table, rid = parse_ref(ref)
    if table != "tasks":
        raise TetherError(f"{ref!r} is not a task ref")
    return rid


def parse_memory_id(ref: str | int) -> int:
    if isinstance(ref, int):
        return ref
    if str(ref).strip().isdigit():
        return int(ref)
    table, rid = parse_ref(ref)
    if table != "memories":
        raise TetherError(f"{ref!r} is not a memory ref")
    return rid


def parse_handoff_id(ref: str | int) -> int:
    if isinstance(ref, int):
        return ref
    if str(ref).strip().isdigit():
        return int(ref)
    table, rid = parse_ref(ref)
    if table != "handoffs":
        raise TetherError(f"{ref!r} is not a handoff ref")
    return rid


class Store:
    def __init__(self, path: str | os.PathLike[str] | None = None,
                 sync_dir: str | os.PathLike[str] | None = None, machine: str | None = None):
        self.path = Path(path) if path else config.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._listeners: list[Any] = []
        self.syncer = (sync.Syncer(self, sync_dir, machine or sync.machine_id()) if sync_dir
                       else sync.Syncer.from_config(self))
        self.sync_error: str | None = None
        self.pulled_on_open = self.pull()

    # -- sync (see sync.py); a sync failure must never break a write or a read ----------

    def pull(self, force: bool = True) -> int:
        if not self.syncer:
            return 0
        try:
            n = self.syncer.pull() if force else self.syncer.maybe_pull()
            self.sync_error = None
            return n
        except (OSError, sqlite3.Error, ValueError) as e:
            self.sync_error = f"pull failed: {e}"
            return 0

    def flush(self) -> int:
        if not self.syncer:
            return 0
        try:
            n = self.syncer.flush()
            self.sync_error = None
            return n
        except (OSError, sqlite3.Error) as e:
            self.sync_error = f"push failed: {e}"
            return 0

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        if version == 0:
            self.db.executescript(SCHEMA)
            version = 1
        for target, script in enumerate(MIGRATIONS, start=2):
            if version < target:
                self.db.executescript(f"BEGIN; {script}; PRAGMA user_version={target}; COMMIT;")
                version = target

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")
            self.flush()

    def on_change(self, callback: Any) -> None:
        """Register callback(project_id) to run after each write (used by the mirror)."""
        self._listeners.append(callback)

    def _changed(self, project_id: int) -> None:
        for cb in self._listeners:
            cb(project_id)

    # -- projects ---------------------------------------------------------------

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
            pid = cur.lastrowid
            self._log(pid, surface, "project", None, f"created project {name!r}")
        self._changed(pid)
        return self.project(pid)

    def project(self, pid: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise TetherError(f"no project with id {pid}")
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
                "SELECT * FROM projects WHERE root_path IS NOT NULL AND archived=0"):
            root = row["root_path"]
            if target == root or target.startswith(root.rstrip(os.sep) + os.sep):
                if best is None or len(root) > len(best["root_path"]):
                    best = row
        return best

    def resolve_project(self, ident: str | None = None, cwd: str | None = None,
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
            if row and not row["archived"]:
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

    # -- decisions --------------------------------------------------------------

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
            did = cur.lastrowid
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

    # -- tasks ------------------------------------------------------------------

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
            tid = cur.lastrowid
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
        counts = {s: 0 for s in TASK_STATUSES}
        counts.update({r["status"]: r["n"] for r in rows})
        return counts

    # -- handoffs ---------------------------------------------------------------

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
            hid = cur.lastrowid
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

    # -- Claude Code session tasks ------------------------------------------------

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
                tid = cur.lastrowid
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
                                "todo": "reopened"}[mapped]
                        self._log(pid, "code", "session-task", f"t{tid}",
                                  f"Code {verb}: {fields.get('title', row['title'])}")
        self._changed(pid)
        return self.task(tid)

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

    # -- memories -----------------------------------------------------------------

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
            mid = cur.lastrowid
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

    def recall(self, query: str, limit: int = 15) -> list[tuple[str, sqlite3.Row, str | None]]:
        """Search memories and every project's records. Returns (kind, row, project slug).
        This is the cross-surface lookup: a chat that doesn't know the project uses it."""
        words = [w for w in query.split() if w]
        if not words:
            raise TetherError("search query is empty")
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

    def change_marker(self) -> tuple:
        """Cheap value that changes whenever anything is written (for watch / live view)."""
        q = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        return (q("SELECT coalesce(max(id),0) FROM events"),
                q("SELECT coalesce(max(updated_at),'') FROM memories"),
                q("SELECT coalesce(max(updated_at),'') FROM tasks"),
                q("SELECT count(*) FROM memories WHERE archived=0"),
                q("SELECT coalesce(max(updated_at),'') FROM projects"),
                q("SELECT coalesce(max(updated_at),'') FROM handoffs"),
                q("SELECT count(*) FROM decisions"))

    # -- events -----------------------------------------------------------------

    def _log(self, pid: int, surface: str, kind: str, ref: str | None, summary: str) -> int:
        cur = self.db.execute(
            "INSERT INTO events (project_id, ts, surface, kind, ref, summary, machine) "
            "VALUES (?,?,?,?,?,?,?)", (pid, now(), surface, kind, ref, summary[:500],
                                       self.syncer.machine if self.syncer else None))
        return cur.lastrowid

    def note(self, pid: int, text: str, surface: str = "cli") -> None:
        """Free-form progress note in the log (e.g. 'shipped v0.2 to TestFlight')."""
        if not text.strip():
            raise TetherError("note text is required")
        secrets.check(text)
        with self.tx():
            self._log(pid, surface, "note", None, text.strip())
        self._changed(pid)

    def events(self, pid: int, limit: int = 20, after_id: int = 0) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id DESC LIMIT ?",
            (pid, after_id, limit)).fetchall()

    def last_seen(self, pid: int, surface: str) -> int | None:
        row = self.db.execute("SELECT event_id FROM seen WHERE project_id=? AND surface=?",
                              (pid, surface)).fetchone()
        return row["event_id"] if row else None

    def mark_seen(self, pid: int, surface: str) -> None:
        latest = self.db.execute("SELECT coalesce(max(id),0) FROM events WHERE project_id=?",
                                 (pid,)).fetchone()[0]
        self.db.execute("INSERT INTO seen (project_id, surface, event_id) VALUES (?,?,?) "
                        "ON CONFLICT(project_id, surface) DO UPDATE SET event_id=excluded.event_id",
                        (pid, surface, latest))

    # -- lookup -----------------------------------------------------------------

    def get(self, ref: str) -> tuple[str, sqlite3.Row]:
        table, rid = parse_ref(ref)
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
        if not row:
            raise TetherError(f"{ref} not found")
        return table, row

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


def _kind(kind: str | None) -> str:
    k = (kind or "idea").strip().lower()
    return k if k in MEMORY_KINDS else "idea"


def _words(text: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", text.lower())
                     if len(w) > 2 and w not in STOPWORDS)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _tags(tags: str | list[str] | None) -> str:
    if isinstance(tags, list):
        tags = ",".join(tags)
    return ",".join(t.strip().lower() for t in (tags or "").split(",") if t.strip())


def _surface(value: str | None) -> str:
    v = (value or "any").strip().lower()
    if v not in config.SURFACES:
        raise TetherError(f"surface must be one of {', '.join(config.SURFACES)}")
    return v
