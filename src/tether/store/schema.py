"""Schema, migrations, constants and small pure helpers for the store."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone

from tether import config, sync
from tether.errors import TetherError

SCHEMA_VERSION = 6

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

FTS_SOURCES = {
    # table -> (SQL for the text to index, project id column, extra WHERE for events)
    "memories": ("{r}.title || ' ' || {r}.body || ' ' || {r}.tags || ' ' || {r}.summary", "{r}.project_id", None),
    "tasks": ("{r}.title || ' ' || {r}.detail || ' ' || {r}.note", "{r}.project_id", None),
    "decisions": ("{r}.text || ' ' || {r}.why", "{r}.project_id", None),
    "handoffs": ("{r}.title || ' ' || {r}.body || ' ' || {r}.note", "{r}.project_id", None),
    "projects": ("{r}.name || ' ' || {r}.goal || ' ' || {r}.status || ' ' || {r}.brief", "{r}.id", None),
    "events": ("{r}.summary", "{r}.project_id", "{r}.kind = 'note'"),
}


def setup_fts(db: sqlite3.Connection) -> None:
    """v6: a ranked full-text index over every searchable record, kept current by triggers.
    SQLite builds without FTS5 skip this; search then falls back to substring matching."""
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_idx USING fts5("
                   "kind UNINDEXED, ref UNINDEXED, pid UNINDEXED, text, "
                   "tokenize='unicode61 remove_diacritics 2', prefix='2 3')")
    except sqlite3.OperationalError:
        return
    for tbl, (text, pid, only) in FTS_SOURCES.items():
        n = "NEW"
        when = f" WHEN {only.format(r=n)}" if only else ""
        ins = (f"INSERT INTO search_idx (kind, ref, pid, text) VALUES ('{tbl}', NEW.id, "
               f"{pid.format(r=n)}, {text.format(r=n)})")
        dele = f"DELETE FROM search_idx WHERE kind='{tbl}' AND ref=OLD.id"
        db.execute(f"CREATE TRIGGER IF NOT EXISTS fts_{tbl}_ai AFTER INSERT ON {tbl}{when} BEGIN {ins}; END")
        db.execute(f"CREATE TRIGGER IF NOT EXISTS fts_{tbl}_au AFTER UPDATE ON {tbl} BEGIN "
                   f"{dele}; " + (f"INSERT INTO search_idx (kind, ref, pid, text) SELECT '{tbl}', NEW.id, "
                                  f"{pid.format(r=n)}, {text.format(r=n)}"
                                  + (f" WHERE {only.format(r=n)}" if only else "") + "; END"))
    rebuild_fts(db)


def rebuild_fts(db: sqlite3.Connection) -> int:
    """Throw the index away and rebuild it from the tables. Returns the rows indexed."""
    db.execute("DELETE FROM search_idx")
    for tbl, (text, pid, only) in FTS_SOURCES.items():
        r = "t"
        where = f" WHERE {only.format(r=r)}" if only else ""
        db.execute(f"INSERT INTO search_idx (kind, ref, pid, text) SELECT '{tbl}', t.id, "
                   f"{pid.format(r=r)}, {text.format(r=r)} FROM {tbl} t{where}")
    return db.execute("SELECT count(*) FROM search_idx").fetchone()[0]


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
    # v5: auto-archive. `usage` is local to this Mac and never synced: when a memory or
    # project was last looked up, and whether it was pulled back out of the archive.
    # projects/memories.archived is 0 active, 1 archived by hand, 2 archived for being idle.
    """
    CREATE TABLE IF NOT EXISTS usage (
        tbl       TEXT NOT NULL,
        rid       INTEGER NOT NULL,
        last_used TEXT NOT NULL,
        restored  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (tbl, rid)
    );
    CREATE TABLE IF NOT EXISTS local_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    # v6: ranked full-text search. A function, because SQLite without FTS5 must still open.
    setup_fts,
]
ARCHIVE_AFTER_DAYS = 30      # unused this long -> auto-archived
REARCHIVE_AFTER_DAYS = 7     # unused this long after being restored -> archived again
SWEEP_EVERY_HOURS = 12
AUTO_ARCHIVED = 2
MAX_RESTORE = 5
MEMORY_KINDS = ("idea", "research", "spec", "decision-log", "bug", "todo-list", "note")
STALE_DAYS = 60
STOPWORDS = frozenset(["a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "of", "on", "or", "our", "so", "that", "the", "their", "then", "there", "this", "to", "was", "we", "what", "when", "which", "with", "you", "your", "app", "project", "idea", "build", "make", "new", "use", "using"])

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


def _days(key: str, default: float) -> float:
    try:
        return float(config.load_config().get(key, default))
    except (TypeError, ValueError):
        return float(default)


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
