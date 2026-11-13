"""Connection, migrations, transactions, sync hooks and the event log."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from tether import config, secrets, sync
from tether.errors import TetherError
from tether.store.base import StoreBase
from tether.store.schema import *
from tether.store.schema import _days, _jaccard, _kind, _surface, _tags, _words  # noqa: F401


class CoreMixin(StoreBase):
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
        try:
            self.maybe_sweep()
        except sqlite3.Error:
            pass            # tidying up must never stop a read or a write

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
                if callable(script):
                    self.db.execute("BEGIN")
                    try:
                        script(self.db)
                        self.db.execute(f"PRAGMA user_version={target}")
                    except BaseException:
                        self.db.execute("ROLLBACK")
                        raise
                    self.db.execute("COMMIT")
                else:
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

    def _log(self, pid: int, surface: str, kind: str, ref: str | None, summary: str) -> int:
        cur = self.db.execute(
            "INSERT INTO events (project_id, ts, surface, kind, ref, summary, machine) "
            "VALUES (?,?,?,?,?,?,?)", (pid, now(), surface, kind, ref, summary[:500],
                                       self.syncer.machine if self.syncer else None))
        return cast(int, cur.lastrowid)

    EDIT_LOG_EVERY = 300        # seconds between "Code edited ..." entries per session
    LIVE_WINDOW = 600           # a project counts as "Code is working" for this long after an edit

    def record_code_edit(self, pid: int, session_id: str, path: str,
                         root: str | None = None) -> bool:
        """Note that Claude Code edited a file. The first edit of a session is logged at once;
        after that edits are gathered and logged at most every EDIT_LOG_EVERY seconds, so a
        long editing run is a handful of lines, not hundreds. Only file names are kept,
        never contents. Returns True if an event was written."""
        import time as _time
        name = path
        if root and path.startswith(root.rstrip("/") + "/"):
            name = path[len(root.rstrip("/")) + 1:]
        else:
            name = Path(path).name
        if "/node_modules/" in f"/{name}" or name.startswith(".git/") or "/.git/" in name:
            return False
        key = f"edits:{pid}:{session_id}"
        row = self.db.execute("SELECT value FROM local_state WHERE key=?", (key,)).fetchone()
        state: dict[str, Any] = json.loads(row["value"]) if row else {"last": 0, "files": []}
        files: list[str] = state["files"]
        if name not in files:
            files.append(name)
        t = _time.time()
        write = t - state["last"] >= self.EDIT_LOG_EVERY
        if write:
            shown = ", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
            n = len(files)
            summary = f"edited {shown}" if n <= 3 else f"edited {n} files: {shown}"
            with self.tx():
                self._log(pid, "code", "code-activity", None, summary[:480])
            state = {"last": t, "files": []}
        else:
            state["files"] = files[-50:]
        self.db.execute("INSERT INTO local_state (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(state)))
        if write:
            self._changed(pid)
        return write

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

    def note(self, pid: int, text: str, surface: str = "cli") -> None:
        """Free-form progress note in the log (e.g. 'shipped v0.2 to TestFlight')."""
        if not text.strip():
            raise TetherError("note text is required")
        secrets.check(text)
        with self.tx():
            self._log(pid, surface, "note", None, text.strip())
        self._changed(pid)

    def change_marker(self) -> tuple:
        """Cheap value that changes whenever anything is written (for watch / live view)."""
        q = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        return (q("SELECT coalesce(max(id),0) FROM events"),
                q("SELECT coalesce(max(updated_at),'') FROM memories"),
                q("SELECT coalesce(max(updated_at),'') FROM tasks"),
                q("SELECT count(*) FROM memories WHERE archived=0"),
                q("SELECT count(*) FROM projects WHERE archived=0"),
                q("SELECT coalesce(max(updated_at),'') FROM projects"),
                q("SELECT coalesce(max(updated_at),'') FROM handoffs"),
                q("SELECT count(*) FROM decisions"))

    def get(self, ref: str) -> tuple[str, sqlite3.Row]:
        table, rid = parse_ref(ref)
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
        if not row:
            raise TetherError(f"{ref} not found")
        if table == "memories":
            if row["archived"] == AUTO_ARCHIVED:
                self._restore("memories", rid)
                row = self.memory(rid)
            self.touch("memories", rid)
        elif row["project_id"]:
            self._use_project(self.project(row["project_id"]))
        return table, row
