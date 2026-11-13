"""Back up the whole ledger to one JSON file, and restore it into an empty store.

Export is plain data (every project, decision, task, handoff, event and memory) so it
outlives any version of Tether. Import refuses to run on a store that already has data:
merging two ledgers is what two-Mac sync is for, and a half-merged restore is worse than
none. Search and archive bookkeeping are rebuilt, not exported.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tether import __version__
from tether.errors import TetherError
from tether.store import Store
from tether.store.schema import rebuild_fts

FORMAT = 1
# Insert order matters for references.
TABLES = ("projects", "decisions", "tasks", "handoffs", "events", "memories")


def export_data(store: Store) -> dict[str, Any]:
    tables = {t: [dict(r) for r in store.db.execute(f"SELECT * FROM {t} ORDER BY id")] for t in TABLES}
    for row in tables["events"]:
        row.pop("machine", None)            # which Mac wrote it is local detail
    return {"tether_export": FORMAT, "tether_version": __version__,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counts": {t: len(r) for t, r in tables.items()}, "tables": tables}


def write_export(store: Store, path: str | os.PathLike[str]) -> dict[str, int]:
    data = export_data(store)
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # private: it is all your notes
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    tmp.replace(p)
    return data["counts"]


def import_data(store: Store, data: dict[str, Any]) -> dict[str, int]:
    if not isinstance(data, dict) or data.get("tether_export") != FORMAT:
        raise TetherError("this is not a Tether export file (or it is from a newer version)")
    have = sum(store.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES)
    if have:
        raise TetherError("this store already has data. Import only restores into an empty "
                          "store; to combine two Macs use `tether sync setup`.")
    tables = data.get("tables") or {}
    counts: dict[str, int] = {}
    store.db.execute("PRAGMA foreign_keys=OFF")
    try:
        with store.tx():
            for t in TABLES:
                have_cols = {r[1] for r in store.db.execute(f"PRAGMA table_info({t})")}
                rows = tables.get(t) or []
                for row in rows:
                    cols = [c for c in row if c in have_cols]
                    store.db.execute(
                        f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [row[c] for c in cols])
                counts[t] = len(rows)
            bad = store.db.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise TetherError(f"the file has {len(bad)} record(s) pointing at missing records")
    except sqlite3.IntegrityError as e:
        raise TetherError(f"the file does not fit this version of Tether: {e}") from e
    finally:
        store.db.execute("PRAGMA foreign_keys=ON")
    try:
        rebuild_fts(store.db)
    except sqlite3.OperationalError:
        pass
    return counts


def read_export(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TetherError(f"cannot read {path}: {e}") from e
