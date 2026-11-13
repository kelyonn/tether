"""Rule-following eval: does Claude actually use Tether the way the server instructions say?

Each scenario seeds a throwaway store, runs a short conversation against the real Tether
MCP server (in-process) with Claude calling its tools, then checks what ended up in the
store. Checks are grouped by rule, so the report reads as rates: "auto-saves ideas 9/10",
"records decisions 4/5", "no noise on small talk 3/3".

    pip install -e ".[eval]"
    tether eval                      # every scenario once, claude-opus-5-5
    tether eval --runs 3 --only decision_recorded

This calls the Claude API and costs money (roughly a few cents per scenario run).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client

from tether.render import context_pack
from tether.server import INSTRUCTIONS, build_server
from tether.store import Store

DEFAULT_MODEL = "claude-opus-5-5"
SCENARIOS = Path(__file__).with_name("eval_scenarios.json")  # ships inside the package
MAX_TURNS = 12  # model calls per user message


@dataclass
class CheckResult:
    rule: str
    desc: str
    ok: bool
    detail: str = ""


@dataclass
class RunResult:
    scenario: str
    checks: list[CheckResult]
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.ok for c in self.checks)


def load_scenarios(path: Path | None = None) -> list[dict]:
    return json.loads((path or SCENARIOS).read_text())["scenarios"]


# -- seeding -------------------------------------------------------------------------

def seed(store: Store, spec: dict, root: Path) -> None:
    for p in spec.get("projects", []):
        path = None
        if p.get("bound"):
            path = root / p["name"].lower().replace(" ", "-")
            path.mkdir(parents=True, exist_ok=True)
        row = store.create_project(p["name"], p.get("goal", ""), str(path) if path else None)
        if p.get("status"):
            store.update_project(row["id"], status=p["status"])
        for d in p.get("decisions", []):
            store.add_decision(row["id"], d["text"], d.get("why", ""))
        for t in p.get("tasks", []):
            task = store.add_task(row["id"], t["title"], "", t.get("for", "any"))
            if t.get("status"):
                store.update_task(task["id"], status=t["status"])
    for m in spec.get("memories", []):
        pid = store.require_project(m["project"])["id"] if m.get("project") else None
        store.add_memory(m["title"], m.get("body", ""), m.get("tags", ""), "chat", pid,
                         m.get("kind", "idea"), m.get("summary", ""))


@contextmanager
def _env(**values: str | None) -> Iterator[None]:
    old = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# -- running -------------------------------------------------------------------------

async def run_scenario(scn: dict, client: Any, model: str = DEFAULT_MODEL,
                       effort: str = "medium") -> RunResult:
    """`client` is an anthropic.AsyncAnthropic (or a fake with the same
    `beta.messages.create` coroutine, for tests)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        surface = scn.get("surface", "chat")
        cwd = None
        with _env(TETHER_HOME=str(root / "home"), TETHER_SURFACE=surface, TETHER_NOTIFY="0",
                  TETHER_PROJECT=None, TETHER_CWD=None):
            store = Store()
            seed(store, scn.get("seed", {}), root)
            if scn.get("cwd_project"):
                cwd = store.require_project(scn["cwd_project"])["root_path"]
            with _env(TETHER_CWD=cwd or str(root / "unbound-chat")):
                (root / "unbound-chat").mkdir(exist_ok=True)
                before = _snapshot(store)
                server = build_server(store)
                result = RunResult(scn["name"], [])
                try:
                    async with Client(server) as mcp:
                        listed = await mcp.list_tools()
                        tools = [{"name": t.name, "description": t.description or "",
                                  "input_schema": t.input_schema} for t in listed.tools]
                        system = _system_prompt(scn, store, surface)
                        messages: list[dict] = []
                        for user_msg in scn["turns"]:
                            messages.append({"role": "user", "content": user_msg})
                            await _loop(client, model, effort, system, tools, messages, mcp,
                                        result)
                except Exception as e:
                    err: Any = e
                    while len(getattr(err, "exceptions", ())) == 1:  # ExceptionGroup is builtin only on 3.11+
                        err = err.exceptions[0]  # the MCP client's task group wraps the real error
                    result.error = f"{type(err).__name__}: {err}"
                result.checks = [_check(c, store, before) for c in scn["checks"]]
                store.close()
                return result


def _system_prompt(scn: dict, store: Store, surface: str) -> str:
    parts = [INSTRUCTIONS, f"\nYou are Claude in {surface}. Tether is connected as an MCP "
             "server; its tools are available to you."]
    if scn.get("cwd_project"):  # what the Claude Code session-start hook injects
        pid = store.require_project(scn["cwd_project"])["id"]
        parts.append("\nThis is the Tether context for this project, loaded at session start. "
                     "You don't need to call the tether `context` tool.\n\n"
                     + context_pack(store, pid, surface, mark_seen=False))
    return "\n".join(parts)


async def _loop(client: Any, model: str, effort: str, system: str, tools: list[dict],
                messages: list[dict], mcp: Client, result: RunResult) -> None:
    for _ in range(MAX_TURNS):
        response = await client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            result.input_tokens += getattr(usage, "input_tokens", 0) or 0
            result.output_tokens += getattr(usage, "output_tokens", 0) or 0
        if response.stop_reason == "refusal":
            raise RuntimeError("model declined the request (stop_reason=refusal)")
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not uses:
            return
        results = []
        for block in uses:
            result.tool_calls.append(block.name)
            r = await mcp.call_tool(block.name, block.input)
            text = "\n".join(t for c in r.content if (t := getattr(c, "text", None)))
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": text or "(empty)", "is_error": bool(r.is_error)})
        messages.append({"role": "user", "content": results})
    raise RuntimeError(f"no final answer after {MAX_TURNS} model calls")


# -- checks --------------------------------------------------------------------------

def _snapshot(store: Store) -> dict[str, int]:
    q = lambda t, w="1=1": store.db.execute(f"SELECT count(*) FROM {t} WHERE {w}").fetchone()[0]  # noqa: E731
    return {"tasks": q("tasks", "kind='project'"), "decisions": q("decisions"),
            "handoffs": q("handoffs"), "memories": q("memories"), "projects": q("projects"),
            "status": store.db.execute("SELECT group_concat(status, '|') FROM projects")
            .fetchone()[0] or ""}


def _has(text: str, words: list[str]) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in words)


def _check(c: dict, store: Store, before: dict) -> CheckResult:
    kind, rule = c["kind"], c.get("rule", c["kind"])
    desc = c.get("desc", kind)
    db = store.db
    after = _snapshot(store)
    if kind == "memory_saved":
        rows = db.execute("SELECT m.*, p.slug FROM memories m LEFT JOIN projects p "
                          "ON p.id=m.project_id WHERE m.archived=0").fetchall()
        hits = [r for r in rows if _has(f"{r['title']} {r['body']} {r['tags']}", c["contains"])]
        ok = bool(hits)
        if ok and "project" in c:
            ok = any(r["slug"] == c["project"] for r in hits)
        if ok and c.get("summary"):
            ok = all(r["summary"] for r in hits)
        return CheckResult(rule, desc, ok, f"{len(rows)} memories")
    if kind == "memory_count":
        n = after["memories"]
        return CheckResult(rule, desc, n == c["count"], f"{n} memories")
    if kind == "decision":
        rows = db.execute("SELECT text, why FROM decisions WHERE status='active'").fetchall()
        ok = any(_has(f"{r['text']} {r['why']}", c["contains"]) for r in rows)
        return CheckResult(rule, desc, ok, f"{len(rows)} active decisions")
    if kind == "superseded":
        row = db.execute("SELECT status FROM decisions WHERE id=?", (c["id"],)).fetchone()
        return CheckResult(rule, desc, bool(row) and row["status"] == "superseded",
                           row["status"] if row else "missing")
    if kind == "tasks_added":
        n = after["tasks"] - before["tasks"]
        ok = n >= c.get("min", 1) and n <= c.get("max", 99)
        return CheckResult(rule, desc, ok, f"{n} added")
    if kind == "task_status":
        row = db.execute("SELECT status FROM tasks WHERE id=?", (c["id"],)).fetchone()
        return CheckResult(rule, desc, bool(row) and row["status"] == c["status"],
                           row["status"] if row else "missing")
    if kind == "handoff":
        rows = db.execute("SELECT to_surface, body FROM handoffs").fetchall()
        ok = any(r["to_surface"] == c["to"] and len(r["body"]) >= c.get("min_body", 40)
                 for r in rows)
        return CheckResult(rule, desc, ok, f"{len(rows)} handoffs")
    if kind == "status_changed":
        return CheckResult(rule, desc, after["status"] != before["status"], str(after["status"]))
    if kind == "no_writes":
        changed = [t for t in c.get("tables", ["tasks", "decisions", "handoffs", "memories",
                                                 "projects"]) if after[t] != before[t]]
        return CheckResult(rule, desc, not changed, "changed: " + ", ".join(changed) if changed
                           else "")
    if kind == "no_project_writes":
        n = db.execute("SELECT count(*) FROM memories WHERE project_id IS NOT NULL").fetchone()[0]
        moved = (after["tasks"], after["decisions"], after["projects"]) != (
            before["tasks"], before["decisions"], before["projects"])
        return CheckResult(rule, desc, n == 0 and not moved,
                           f"{n} filed memories" + (", project records changed" if moved else ""))
    if kind == "no_text":
        texts = [" ".join(str(v) for v in tuple(r)) for t in ("memories", "tasks", "decisions",
                 "handoffs", "projects", "events") for r in db.execute(f"SELECT * FROM {t}")]
        leaked = [w for w in c["contains"] if any(w.lower() in x.lower() for x in texts)]
        return CheckResult(rule, desc, not leaked, "found: " + ", ".join(leaked) if leaked else "")
    raise ValueError(f"unknown check kind {kind!r}")


# -- reporting -----------------------------------------------------------------------

def summarize(results: list[RunResult]) -> dict:
    rules: dict[str, list[int]] = {}
    for r in results:
        for c in r.checks:
            ok = c.ok and r.error is None
            rules.setdefault(c.rule, [0, 0])
            rules[c.rule][0] += int(ok)
            rules[c.rule][1] += 1
    total = sum(v[1] for v in rules.values())
    good = sum(v[0] for v in rules.values())
    return {"rules": {k: {"passed": v[0], "total": v[1], "rate": round(v[0] / v[1], 2)}
                      for k, v in sorted(rules.items())},
            "overall": round(good / total, 2) if total else 0.0,
            "scenarios_passed": sum(r.passed for r in results), "runs": len(results),
            "input_tokens": sum(r.input_tokens for r in results),
            "output_tokens": sum(r.output_tokens for r in results)}


def report(results: list[RunResult], summary: dict) -> str:
    L = []
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        L.append(f"{mark}  {r.scenario}  ({', '.join(r.tool_calls) or 'no tool calls'})")
        if r.error:
            L.append(f"      error: {r.error}")
        for c in r.checks:
            if not c.ok:
                L.append(f"      ✗ [{c.rule}] {c.desc}" + (f" — {c.detail}" if c.detail else ""))
    L += ["", "By rule:"]
    for rule, v in summary["rules"].items():
        L.append(f"  {rule:16} {v['passed']}/{v['total']}  {v['rate']:.0%}")
    L.append(f"\nOverall {summary['overall']:.0%} · {summary['scenarios_passed']}/"
             f"{summary['runs']} scenario runs fully passed · "
             f"{summary['input_tokens']:,} in / {summary['output_tokens']:,} out tokens")
    return "\n".join(L)


async def run_all(client: Any, model: str = DEFAULT_MODEL, effort: str = "medium",
                  only: list[str] | None = None, runs: int = 1,
                  path: Path | None = None) -> list[RunResult]:
    out = []
    for scn in load_scenarios(path):
        if only and scn["name"] not in only:
            continue
        for _ in range(runs):
            out.append(await run_scenario(scn, client, model, effort))
    return out


def save(results: list[RunResult], summary: dict, model: str, effort: str,
         directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}-{model.replace('/', '_')}-{effort}.json"
    path.write_text(json.dumps({
        "model": model, "effort": effort, "summary": summary,
        "results": [{"scenario": r.scenario, "passed": r.passed, "error": r.error,
                     "tool_calls": r.tool_calls,
                     "checks": [c.__dict__ for c in r.checks]} for r in results]},
        indent=2))
    return path
