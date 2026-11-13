"""`tether doctor --deep`: prove the whole chain works without touching your real data.

Spawns the server exactly as Claude clients do (same interpreter, same entry point) in
a throwaway TETHER_HOME, drives it over stdio with a real MCP client, then runs the
Claude Code hook against the resulting project.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tether import install


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def run() -> list[Check]:
    checks: list[Check] = []
    with tempfile.TemporaryDirectory() as tmp:
        home, proj = os.path.join(tmp, "home"), os.path.join(tmp, "selftest-app")
        os.makedirs(proj)
        env = {**os.environ, "TETHER_HOME": home, "TETHER_SURFACE": "code"}
        cmd = install.server_command()
        params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env, cwd=proj)

        async def drive() -> dict[str, str]:
            out: dict[str, str] = {}
            async with Client(params) as client:
                tools = await client.list_tools()
                out["tools"] = ",".join(sorted(t.name for t in tools.tools))

                async def call(name: str, args: dict) -> str:
                    r = await client.call_tool(name, args)
                    text = "\n".join(t for c in r.content if (t := getattr(c, "text", None)))
                    if r.is_error:
                        raise RuntimeError(f"{name}: {text}")
                    return text

                await call("start_project", {"name": "selftest app", "goal": "prove it works",
                                             "path": proj})
                await call("record_decision", {"decision": "Use SQLite", "why": "local"})
                await call("add_tasks", {"tasks": [
                    {"title": "write code", "for_surface": "code", "priority": 1},
                    {"title": "make mockup", "for_surface": "design"}]})
                await call("create_handoff", {"to_surface": "design", "title": "brief",
                                              "body": "make it nice"})
                await call("update_task", {"task": "t1", "status": "done", "note": "ok"})
                out["context"] = await call("context", {})
                out["search"] = await call("search", {"query": "sqlite"})
                out["remember"] = await call("remember", {
                    "title": "Selftest reading app", "body": "swipe to save quotes",
                    "summary": "quote saver", "project": "selftest-app"})
                out["remember2"] = await call("remember", {
                    "title": "Selftest reading app", "body": "kindle import"})
                out["recall"] = await call("recall", {"query": "quotes"})
            return out

        try:
            got = anyio.run(drive)
        except BaseException as e:
            return [Check("server starts and answers over stdio", False, repr(e))]

        want = {"context", "record_decision", "add_tasks", "update_task", "create_handoff"}
        missing = want - set(got["tools"].split(","))
        checks.append(Check("server starts and lists tools", not missing,
                            f"missing: {sorted(missing)}" if missing else
                            f"{len(got['tools'].split(','))} tools"))
        ctx = got["context"]
        checks.append(Check("context pack has goal, decision and open task",
                            "prove it works" in ctx and "Use SQLite" in ctx
                            and "make mockup" in ctx, ""))
        checks.append(Check("finished tasks are hidden from the pack",
                            "write code" not in ctx.split("## Open tasks")[1]
                            and "1 closed and hidden" in ctx, ""))
        checks.append(Check("search finds history", "Use SQLite" in got["search"], ""))

        checks.append(Check("ideas save, merge duplicates and recall",
                            got["remember"].startswith("Saved m")
                            and "Merged into existing" in got["remember2"]
                            and "Selftest reading app" in got["recall"], got["remember2"]))

        hook = subprocess.run([sys.executable, "-m", "tether", "hook", "session-start"],
                              input=json.dumps({"cwd": proj}), capture_output=True, text=True,
                              env=env)
        try:
            injected = json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"]
            checks.append(Check("Claude Code hook injects the pack",
                                "selftest app" in injected, ""))
        except (json.JSONDecodeError, KeyError):
            checks.append(Check("Claude Code hook injects the pack", False, hook.stderr[:200]))

        task_hook = subprocess.run(
            [sys.executable, "-m", "tether", "hook", "post-tool-use"],
            input=json.dumps({"cwd": proj, "session_id": "selftest", "tool_name": "TaskCreate",
                              "tool_input": {"subject": "selftest code task"},
                              "tool_response": {"task": {"id": "1"}}}),
            capture_output=True, text=True, env={**env, "TETHER_NOTIFY": "0"})
        from tether.store import Store
        s = Store(os.path.join(home, "tether.db"))
        synced = any(t["title"] == "selftest code task"
                     for t in s.recent_session_tasks(s.require_project("selftest-app")["id"]))
        s.close()
        checks.append(Check("Claude Code task list syncs into Tether", synced,
                            task_hook.stderr[:200]))
        checks.append(_sync_check(tmp))
    return checks


def _sync_check(tmp: str) -> Check:
    """Two throwaway 'Macs' sharing a folder: an edit on each must reach the other."""
    from tether.store import Store
    try:
        shared = os.path.join(tmp, "shared")
        a = Store(os.path.join(tmp, "mac-a.db"), sync_dir=shared, machine="selftest-a")
        b = Store(os.path.join(tmp, "mac-b.db"), sync_dir=shared, machine="selftest-b")
        p = a.create_project("sync check")
        b.pull()
        b.update_project(b.require_project("sync-check")["id"], status="edited on B")
        a.pull()
        ok = a.project(p["id"])["status"] == "edited on B"
        a.close()
        b.close()
        return Check("two-Mac sync round trip", ok, "")
    except Exception as e:
        return Check("two-Mac sync round trip", False, repr(e))
