"""Claude Code hooks. They must never break or slow a session: every failure is silent."""

from __future__ import annotations

import hashlib
import json
import os
import sys

from tether import notify
from tether.store import Store


def _payload() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _resp(payload: dict) -> dict:
    r = payload.get("tool_response")
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except json.JSONDecodeError:
            r = {}
    return r if isinstance(r, dict) else {}


def post_tool_use() -> int:
    """Mirror Claude Code's own task list (TaskCreate/TaskUpdate/TodoWrite) into Tether
    as session tasks, but only for folders bound to a project."""
    payload = _payload()
    tool = payload.get("tool_name", "")
    if tool not in ("TaskCreate", "TaskUpdate", "TodoWrite"):
        return 0
    try:
        store = Store()
        row = store.project_for_path(payload.get("cwd") or os.getcwd())
        if not row:
            return 0
        sid = str(payload.get("session_id") or "session")[:12]
        inp = payload.get("tool_input") or {}
        pid = row["id"]
        finished = None
        if tool == "TaskCreate":
            task = _resp(payload).get("task") or {}
            tid = str(task.get("id") or hashlib.sha1(
                (inp.get("subject") or "").encode()).hexdigest()[:6])
            store.sync_session_task(pid, sid, tid, inp.get("subject") or task.get("subject"),
                                    inp.get("description"), "pending")
        elif tool == "TaskUpdate":
            tid = str(inp.get("taskId") or inp.get("task_id") or "")
            if tid:
                t = store.sync_session_task(pid, sid, tid, inp.get("subject"),
                                            inp.get("description"), inp.get("status"))
                if t["status"] == "done":
                    finished = t["title"]
        else:  # TodoWrite sends the whole list every time
            keep = set()
            for item in inp.get("todos") or []:
                text = item.get("content") or item.get("subject") or ""
                if not text:
                    continue
                key = hashlib.sha1(text.encode()).hexdigest()[:8]
                keep.add(key)
                before = store.db.execute("SELECT status FROM tasks WHERE project_id=? AND "
                                          "ext_key=?", (pid, f"{sid}:{key}")).fetchone()
                t = store.sync_session_task(pid, sid, key, text, None, item.get("status"))
                if t["status"] == "done" and (not before or before["status"] != "done"):
                    finished = text
            store.drop_missing_session_tasks(pid, sid, keep)
        if finished:
            notify.notify(f"Code finished ({row['slug']})", finished)
    except Exception:
        return 0
    return 0
