"""Two-Mac sync through a shared folder (iCloud Drive by default).

SQLite must never live in a synced folder: two machines writing one database file
corrupts it. Instead each Mac keeps its own database and appends every change to its
own journal file, `<sync dir>/journals/<machine>.jsonl`. Each Mac only ever writes its
own file, so the sync service never sees conflicting edits to one file.

How changes are captured: SQLite triggers (schema v4) add every inserted or updated row
to `sync_outbox`, whatever wrote it (CLI, MCP server, hooks, live view). `flush()` turns
the outbox into field-level ops by diffing each row against the last exported copy
(`sync_shadow`), stamps them with a hybrid logical clock and appends them to the journal.

How changes are merged: `pull()` reads the new lines of every other machine's journal,
sorts them by clock and applies each field only if its clock is newer than the one this
machine has for that field (`sync_fields`). Last writer wins per field, so marking a
task done on one Mac and renaming it on the other both survive. Records reference each
other by uid, never by local integer id; an imported row keeps its original id when that
id is free here, so t12 is usually t12 on both Macs.

Nothing is ever hard-deleted in Tether (archive/dropped flags), so there are no deletes
to sync.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from tether import config

TABLES = ("projects", "decisions", "tasks", "handoffs", "memories", "events")
# column -> table it points at; values travel as uids
FKS: dict[str, dict[str, str]] = {
    "projects": {},
    "decisions": {"project_id": "projects", "superseded_by": "decisions"},
    "tasks": {"project_id": "projects"},
    "handoffs": {"project_id": "projects"},
    "memories": {"project_id": "projects"},
    "events": {"project_id": "projects"},
}
PULL_EVERY = 3.0  # seconds, for maybe_pull()
COMPACT_AT = 8 * 1024 * 1024  # bytes; a journal this big (and 3x its last compacted size) is rewritten


def migration_sql() -> str:
    """Schema v4: uids on every synced table, change-capture triggers, sync bookkeeping."""
    L = ["CREATE TABLE IF NOT EXISTS sync_flags (applying INTEGER NOT NULL);",
         "INSERT INTO sync_flags SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM sync_flags);",
         "CREATE TABLE IF NOT EXISTS sync_outbox (tbl TEXT NOT NULL, rid INTEGER NOT NULL, "
         "PRIMARY KEY (tbl, rid));",
         "CREATE TABLE IF NOT EXISTS sync_shadow (tbl TEXT NOT NULL, uid TEXT NOT NULL, "
         "data TEXT NOT NULL, PRIMARY KEY (tbl, uid));",
         "CREATE TABLE IF NOT EXISTS sync_fields (tbl TEXT NOT NULL, uid TEXT NOT NULL, "
         "field TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (tbl, uid, field));",
         "CREATE TABLE IF NOT EXISTS sync_offsets (machine TEXT PRIMARY KEY, pos INTEGER NOT "
         "NULL, last_ts TEXT, files_seen TEXT);",
         "CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT);",
         "CREATE TABLE IF NOT EXISTS sync_alias (tbl TEXT NOT NULL, uid TEXT NOT NULL, "
         "local_id INTEGER NOT NULL, PRIMARY KEY (tbl, uid));"]
    L.append("ALTER TABLE events ADD COLUMN machine TEXT;  -- which Mac made the change")
    for t in TABLES:
        L += [f"ALTER TABLE {t} ADD COLUMN uid TEXT;",
              f"UPDATE {t} SET uid = lower(hex(randomblob(16))) WHERE uid IS NULL;",
              f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{t}_uid ON {t}(uid);",
              f"CREATE TRIGGER IF NOT EXISTS sx_{t}_ins AFTER INSERT ON {t} "
              f"WHEN (SELECT applying FROM sync_flags) = 0 BEGIN "
              f"UPDATE {t} SET uid = lower(hex(randomblob(16))) WHERE id = NEW.id AND uid IS NULL; "
              f"INSERT OR IGNORE INTO sync_outbox (tbl, rid) VALUES ('{t}', NEW.id); END;",
              f"CREATE TRIGGER IF NOT EXISTS sx_{t}_upd AFTER UPDATE ON {t} "
              f"WHEN (SELECT applying FROM sync_flags) = 0 BEGIN "
              f"INSERT OR IGNORE INTO sync_outbox (tbl, rid) VALUES ('{t}', NEW.id); END;"]
    return "\n".join(L)


def _line(op: dict) -> str:
    return json.dumps(op, separators=(",", ":")) + "\n"


def _header(compacted: bool = False) -> str:
    """First line of every journal. A new id means 'this file was rewritten'."""
    h = {"journal": uuid.uuid4().hex, "format": 1}
    if compacted:
        h["compacted"] = int(time.time())
    return json.dumps(h, separators=(",", ":"))


def _journal_id(first_line: bytes) -> str | None:
    try:
        h = json.loads(first_line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return h.get("journal") if isinstance(h, dict) else None


def default_dir() -> Path:
    icloud = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    return (icloud if icloud.is_dir() else Path.home()) / "Tether"


def machine_id() -> str:
    cfg = config.load_config()
    if not cfg.get("machine_id"):
        host = socket.gethostname().split(".")[0].lower() or "mac"
        cfg["machine_id"] = f"{''.join(c for c in host if c.isalnum() or c == '-')[:24]}-{uuid.uuid4().hex[:6]}"
        config.save_config(cfg)
    return cfg["machine_id"]


class Syncer:
    def __init__(self, store: Any, directory: str | os.PathLike[str], machine: str):
        self.store = store
        self.db: sqlite3.Connection = store.db
        self.dir = Path(directory).expanduser()
        self.machine = machine
        self.journals = self.dir / "journals"
        self._last_pull = 0.0
        self._cols: dict[str, list[str]] = {}

    @classmethod
    def from_config(cls, store: Any) -> "Syncer | None":
        d = config.load_config().get("sync_dir")
        return cls(store, d, machine_id()) if d else None

    # -- clock ----------------------------------------------------------------------

    def _state(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_state(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO sync_state (key, value) VALUES (?,?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value", (key, value))

    def _tick(self) -> str:
        """Hybrid logical clock: wall-clock ms plus a counter, so order survives a Mac
        whose clock is a little behind. Sortable as a string."""
        last = self._state("clock") or "0000000000000.00000"
        lms, lc = (int(x) for x in last.split(".")[:2])
        ms = int(time.time() * 1000)
        ms, c = (ms, 0) if ms > lms else (lms, lc + 1)
        stamp = f"{ms:013d}.{c:05d}"
        self._set_state("clock", stamp)
        return f"{stamp}.{self.machine}"

    def _observe(self, ts: str) -> None:
        stamp = ".".join(ts.split(".")[:2])
        if stamp > (self._state("clock") or ""):
            self._set_state("clock", stamp)

    # -- row <-> op representation ----------------------------------------------------

    def cols(self, tbl: str) -> list[str]:
        if tbl not in self._cols:
            self._cols[tbl] = [r[1] for r in self.db.execute(f"PRAGMA table_info({tbl})")]
        return self._cols[tbl]

    def _uid_of(self, tbl: str, rid: int | None) -> str | None:
        if rid is None:
            return None
        row = self.db.execute(f"SELECT uid FROM {tbl} WHERE id=?", (rid,)).fetchone()
        return row[0] if row else None

    def _id_of(self, tbl: str, uid: str | None) -> int | None:
        if uid is None:
            return None
        row = self.db.execute(f"SELECT id FROM {tbl} WHERE uid=?", (uid,)).fetchone()
        if row:
            return row[0]
        row = self.db.execute("SELECT local_id FROM sync_alias WHERE tbl=? AND uid=?",
                              (tbl, uid)).fetchone()
        return row[0] if row else None

    def _export(self, tbl: str, row: sqlite3.Row) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for c in self.cols(tbl):
            if c in ("id", "uid"):
                continue
            v = row[c]
            if c in FKS[tbl]:
                v = self._uid_of(FKS[tbl][c], v)
            elif tbl == "handoffs" and c == "task_ids":
                v = [u for u in (self._uid_of("tasks", t) for t in json.loads(v or "[]")) if u]
            out[c] = v
        return out

    def _import_value(self, tbl: str, col: str, v: Any) -> Any:
        if col in FKS[tbl]:
            return self._id_of(FKS[tbl][col], v)
        if tbl == "handoffs" and col == "task_ids":
            return json.dumps([i for i in (self._id_of("tasks", u) for u in (v or [])) if i])
        return v

    # -- push -------------------------------------------------------------------------

    def flush(self) -> int:
        """Export pending local changes to this machine's journal. Returns ops written."""
        if not self.db.execute("SELECT 1 FROM sync_outbox LIMIT 1").fetchone():
            return 0
        self.journals.mkdir(parents=True, exist_ok=True)
        path = self.journals / f"{self.machine}.jsonl"
        self.db.execute("BEGIN IMMEDIATE")  # also serialises flushes from other processes
        try:
            ops = []
            pending = self.db.execute("SELECT tbl, rid FROM sync_outbox").fetchall()
            for tbl, rid in pending:
                row = self.db.execute(f"SELECT * FROM {tbl} WHERE id=?", (rid,)).fetchone()
                if row is None or tbl not in TABLES:
                    continue
                cur = self._export(tbl, row)
                sh = self.db.execute("SELECT data FROM sync_shadow WHERE tbl=? AND uid=?",
                                     (tbl, row["uid"])).fetchone()
                old = json.loads(sh[0]) if sh else {}
                diff = {k: v for k, v in cur.items() if k not in old or old[k] != v}
                if not diff:
                    continue
                ts = self._tick()
                ops.append({"ts": ts, "t": tbl, "u": row["uid"], "id": rid, "f": diff})
                self.db.executemany(
                    "INSERT INTO sync_fields (tbl, uid, field, ts) VALUES (?,?,?,?) "
                    "ON CONFLICT(tbl, uid, field) DO UPDATE SET ts=excluded.ts",
                    [(tbl, row["uid"], k, ts) for k in diff])
                self.db.execute("INSERT INTO sync_shadow (tbl, uid, data) VALUES (?,?,?) "
                                "ON CONFLICT(tbl, uid) DO UPDATE SET data=excluded.data",
                                (tbl, row["uid"], json.dumps(cur)))
            if ops:
                with open(path, "a", encoding="utf-8") as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    if f.tell() == 0:
                        f.write(_header() + "\n")
                    f.write("".join(_line(op) for op in ops))
                    f.flush()
                    os.fsync(f.fileno())
            self.db.execute("DELETE FROM sync_outbox")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        if ops and self._needs_compaction(path):
            self.compact()
        return len(ops)

    # -- compaction -------------------------------------------------------------------

    def _needs_compaction(self, path: Path) -> bool:
        try:
            size = path.stat().st_size
        except OSError:
            return False
        last = int(self._state("compacted_bytes") or 0)
        return size > max(COMPACT_AT, 3 * last)

    def compact(self) -> tuple[int, int]:
        """Rewrite this Mac's journal as one snapshot of the current state, keeping every
        field's original clock so merging stays correct. Returns (bytes before, after).

        The new file starts with a fresh journal id; other Macs notice the id changed and
        re-read it from the top. Re-applying is harmless: a field only changes when its
        clock is newer than the one already merged. A snapshot holds everything, so a new
        Mac can bootstrap from any single journal."""
        self.flush()
        path = self.journals / f"{self.machine}.jsonl"
        before = path.stat().st_size if path.exists() else 0
        self.journals.mkdir(parents=True, exist_ok=True)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            ops = []
            for tbl in TABLES:
                for row in self.db.execute(f"SELECT * FROM {tbl} WHERE uid IS NOT NULL").fetchall():
                    data = self._export(tbl, row)
                    known = dict(self.db.execute(
                        "SELECT field, ts FROM sync_fields WHERE tbl=? AND uid=?",
                        (tbl, row["uid"])).fetchall())
                    ft = {k: known.get(k) or self._tick() for k in data}
                    top = max(ft.values())
                    op = {"ts": top, "t": tbl, "u": row["uid"], "id": row["id"], "f": data}
                    odd = {k: v for k, v in ft.items() if v != top}  # only clocks that differ
                    if odd:
                        op["ft"] = odd
                    ops.append(op)
            ops.sort(key=lambda o: o["ts"])
            tmp = path.with_name(f".{path.name}.compact")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(_header(compacted=True) + "\n")
                f.write("".join(_line(op) for op in ops))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            after = path.stat().st_size
            self._set_state("compacted_bytes", str(after))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return before, after

    def queue_everything(self) -> None:
        """Queue every row this machine has never exported or received (first setup).
        Rows that came from another Mac already have a shadow and are not sent back."""
        for t in TABLES:
            self.db.execute(
                f"INSERT OR IGNORE INTO sync_outbox (tbl, rid) SELECT '{t}', id FROM {t} "
                f"WHERE uid NOT IN (SELECT uid FROM sync_shadow WHERE tbl='{t}')")

    # -- pull -------------------------------------------------------------------------

    def maybe_pull(self) -> int:
        if time.monotonic() - self._last_pull < PULL_EVERY:
            return 0
        return self.pull()

    def pull(self) -> int:
        """Merge new ops from every other machine's journal. Returns ops applied."""
        self._last_pull = time.monotonic()
        if not self.journals.is_dir():
            return 0
        batch: list[tuple[dict, str]] = []
        new_pos: dict[str, int] = {}
        journal_ids: dict[str, str | None] = {}
        for f in sorted(self.journals.glob("*.jsonl")):
            m = f.stem
            if m == self.machine:
                continue
            row = self.db.execute("SELECT pos, files_seen FROM sync_offsets WHERE machine=?",
                                  (m,)).fetchone()
            pos, seen_id = (row[0], row[1]) if row else (0, None)
            try:
                size = f.stat().st_size
                with open(f, "rb") as fh:
                    jid = _journal_id(fh.readline())
                    if jid != seen_id or size < pos:
                        pos = 0  # compacted or re-created: read the new file from the top
                    if size == pos:
                        continue
                    fh.seek(pos)
                    data = fh.read()
            except OSError:
                continue  # iCloud may not have downloaded it yet
            journal_ids[m] = jid
            end = data.rfind(b"\n") + 1  # only complete lines; a half-synced tail waits
            for line in data[:end].splitlines():
                try:
                    batch.append((json.loads(line), m))
                except json.JSONDecodeError:
                    continue
            new_pos[m] = pos + end
        if not new_pos:
            return 0
        batch = [b for b in batch if "t" in b[0]]  # drop journal headers
        batch.sort(key=lambda x: x[0].get("ts", ""))
        applied = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("UPDATE sync_flags SET applying=1")
            self._retry: list[dict] = []
            for op, _m in batch:
                applied += self._apply(op)
                for t in [op.get("ts", ""), *(op.get("ft") or {}).values()]:
                    self._observe(t)
            for op in self._retry:  # references to rows that arrived later in the batch
                self._fix_refs(op)
            last = {m: max((op["ts"] for op, mm in batch if mm == m and "ts" in op), default=None)
                    for m in new_pos}
            for m, p in new_pos.items():
                self.db.execute(
                    "INSERT INTO sync_offsets (machine, pos, last_ts, files_seen) VALUES (?,?,?,?) "
                    "ON CONFLICT(machine) DO UPDATE SET pos=excluded.pos, files_seen=excluded.files_seen, "
                    "last_ts=coalesce(excluded.last_ts, sync_offsets.last_ts)",
                    (m, p, last[m], journal_ids.get(m)))
            self._set_state("last_pull", str(int(time.time())))
            self.db.execute("UPDATE sync_flags SET applying=0")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return applied

    def _apply(self, op: dict) -> int:
        tbl, uid, fields, ts = op.get("t"), op.get("u"), op.get("f") or {}, op.get("ts", "")
        if tbl not in TABLES or not uid or not fields:
            return 0
        if self._unresolved(tbl, fields):
            self._retry.append(op)
        cols = set(self.cols(tbl))
        fields = {k: v for k, v in fields.items() if k in cols and k not in ("id", "uid")}
        rid = self._id_of(tbl, uid)
        if rid is None and tbl == "projects" and fields.get("slug"):
            # Both Macs created the same project before syncing: merge them by slug.
            same = self.db.execute("SELECT id FROM projects WHERE slug=?",
                                   (fields["slug"],)).fetchone()
            if same:
                rid = same[0]
                self.db.execute("INSERT OR REPLACE INTO sync_alias (tbl, uid, local_id) "
                                "VALUES (?,?,?)", (tbl, uid, rid))
        fts = {k: (op.get("ft") or {}).get(k, ts) for k in fields}
        if rid is None:
            return self._insert(tbl, uid, op.get("id"), fields, fts)
        if tbl == "projects":
            fields = {k: v for k, v in fields.items() if k != "root_path"}  # bindings are per Mac
        luid = self._uid_of(tbl, rid)
        known = dict(self.db.execute("SELECT field, ts FROM sync_fields WHERE tbl=? AND uid=?",
                                     (tbl, luid)).fetchall())
        win = {k: v for k, v in fields.items() if fts[k] > known.get(k, "")}
        if not win:
            return 0
        vals = {k: self._import_value(tbl, k, v) for k, v in win.items()}
        # never overwrite a reference with NULL just because its target is unknown here
        vals = {k: v for k, v in vals.items()
                if not (k in FKS[tbl] and v is None and win[k] is not None)}
        if vals:
            sets = ", ".join(f"{k}=?" for k in vals)
            self.db.execute(f"UPDATE {tbl} SET {sets} WHERE id=?", (*vals.values(), rid))
        self._record(tbl, luid, rid, {k: fts[k] for k in win})
        return 1

    def _unresolved(self, tbl: str, fields: dict) -> bool:
        for k, v in fields.items():
            if k in FKS[tbl] and v and self._id_of(FKS[tbl][k], v) is None:
                return True
            if tbl == "handoffs" and k == "task_ids" and any(
                    self._id_of("tasks", u) is None for u in (v or [])):
                return True
        return False

    def _fix_refs(self, op: dict) -> None:
        tbl, f = op["t"], op["f"]
        rid = self._id_of(tbl, op["u"])
        if rid is None:
            return
        luid = self._uid_of(tbl, rid)
        for k, v in f.items():
            if not (k in FKS[tbl] or (tbl == "handoffs" and k == "task_ids")):
                continue
            cur = self.db.execute("SELECT ts FROM sync_fields WHERE tbl=? AND uid=? AND field=?",
                                  (tbl, luid, k)).fetchone()
            val = self._import_value(tbl, k, v)
            if cur and cur[0] == (op.get("ft") or {}).get(k, op["ts"]) and val is not None:
                self.db.execute(f"UPDATE {tbl} SET {k}=? WHERE id=?", (val, rid))
        self._record(tbl, luid, rid, {})

    def _insert(self, tbl: str, uid: str, hint: int | None, fields: dict,
                fts: dict[str, str]) -> int:
        vals = {k: self._import_value(tbl, k, v) for k, v in fields.items()}
        if tbl not in ("projects", "memories") and fields.get("project_id") and \
                vals.get("project_id") is None:
            return 0  # parent unknown here (ops are clock-sorted, so this is a broken journal)
        if tbl == "projects":
            # A folder binding only makes sense if that folder exists on this Mac too.
            if vals.get("root_path") and not os.path.isdir(vals["root_path"]):
                vals["root_path"] = None
            base, n = vals.get("slug") or "project", 2
            while self.db.execute("SELECT 1 FROM projects WHERE slug=?", (vals.get("slug"),)).fetchone():
                vals["slug"], n = f"{base}-{n}", n + 1
        if tbl == "tasks" and vals.get("ext_key") and self.db.execute(
                "SELECT 1 FROM tasks WHERE project_id=? AND ext_key=?",
                (vals.get("project_id"), vals["ext_key"])).fetchone():
            return 0
        if hint and not self.db.execute(f"SELECT 1 FROM {tbl} WHERE id=?", (hint,)).fetchone():
            vals["id"] = hint
        vals["uid"] = uid
        names = ", ".join(vals)
        try:
            cur = self.db.execute(f"INSERT INTO {tbl} ({names}) VALUES ({', '.join('?' * len(vals))})",
                                  tuple(vals.values()))
        except sqlite3.IntegrityError:
            return 0  # incomplete row (e.g. a journal that starts mid-history); skip
        self._record(tbl, uid, cur.lastrowid, fts)
        return 1

    def _record(self, tbl: str, uid: str, rid: int, fields: dict[str, str]) -> None:
        """Remember each applied field's clock, and refresh the shadow so the merged
        values are not exported back as local changes."""
        self.db.executemany(
            "INSERT INTO sync_fields (tbl, uid, field, ts) VALUES (?,?,?,?) "
            "ON CONFLICT(tbl, uid, field) DO UPDATE SET ts=excluded.ts "
            "WHERE excluded.ts > sync_fields.ts", [(tbl, uid, k, t) for k, t in fields.items()])
        row = self.db.execute(f"SELECT * FROM {tbl} WHERE id=?", (rid,)).fetchone()
        if row is not None:
            self.db.execute("INSERT INTO sync_shadow (tbl, uid, data) VALUES (?,?,?) "
                            "ON CONFLICT(tbl, uid) DO UPDATE SET data=excluded.data",
                            (tbl, row["uid"], json.dumps(self._export(tbl, row))))

    def announce(self) -> None:
        """Leave a small marker so other Macs can list this one before it changes anything."""
        d = self.dir / "machines"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.machine}.json").write_text(json.dumps(
            {"machine": self.machine, "host": socket.gethostname(), "joined": int(time.time())}))

    # -- status -----------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        peers = []
        names = {f.stem for f in self.journals.glob("*.jsonl")} if self.journals.is_dir() else set()
        if (self.dir / "machines").is_dir():
            names |= {f.stem for f in (self.dir / "machines").glob("*.json")}
        for name in sorted(names - {self.machine}):
            f = self.journals / f"{name}.jsonl"
            row = self.db.execute("SELECT pos, last_ts FROM sync_offsets WHERE machine=?",
                                  (name,)).fetchone()
            last = int(row[1].split(".")[0]) / 1000 if row and row[1] else None
            try:
                behind = f.stat().st_size - (row[0] if row else 0)
            except OSError:
                behind = 0
            peers.append({"machine": name, "last_change": last, "unread_bytes": behind})
        mine = self.journals / f"{self.machine}.jsonl"
        lp = self._state("last_pull")
        return {"dir": str(self.dir), "machine": self.machine, "peers": peers,
                "pending": self.db.execute("SELECT count(*) FROM sync_outbox").fetchone()[0],
                "journal_bytes": mine.stat().st_size if mine.exists() else 0,
                "last_pull": int(lp) if lp else None}
