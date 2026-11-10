"""The MCP server every Claude surface talks to.

Tools return compact text rather than JSON: the reader is a model, and text costs
fewer tokens. The working rules ship as server instructions, so users edit no prompts.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from tether import __version__, config, mirror
from tether import notify
from tether.render import (DEFAULT_BUDGET_TOKENS, context_pack, detail, memory_line,
                           projects_overview, recall_results, search_results, task_line)
from tether.store import (Store, TetherError, parse_handoff_id, parse_memory_id,
                          parse_task_id)

Surface = Literal["code", "desktop", "cowork", "chat", "design", "any"]
TaskStatus = Literal["todo", "doing", "blocked", "done", "dropped"]

INSTRUCTIONS = """\
Tether holds this project's shared state across Claude surfaces (Code, Desktop, Cowork, \
Chat, Design): goal, status, decisions, open tasks and handoffs. Other sessions rely on \
what you record here, so keep it accurate.

1. Start: call `context` once at the beginning of work on a project, before answering \
questions about it. It returns goal, status, open tasks, active decisions, handoffs \
waiting for you and what changed since you were last here. Do not call it again in the \
same session unless the user switches project. In Claude Code it may already be in your \
context from the session-start hook; if so, skip the call.
2. Decisions: when the user and you settle something that a future session must not \
re-litigate (a choice of tech, scope, design direction, naming, a constraint), call \
`record_decision` right away with a one-line decision and a short why. If it overturns \
an earlier decision, pass `replaces`.
3. Tasks: when concrete follow-up work emerges, add it with `add_tasks` (batch them), \
tagging `for_surface` with who should do it (code for implementation, design for \
mockups, chat for thinking/writing). Set a task to `doing` when you start it and `done` \
when finished, with a short note of what changed. Don't add tasks for things you will \
finish in the next few minutes.
4. Handoffs: when work must continue in another surface (a design brief for Design, a \
spec or mockup for Code), call `create_handoff` with a self-contained body: the next \
surface will not see this conversation. When you pick one up, mark it `accepted`, \
then `done`.
5. Status: when a session ends a meaningful chunk of work, call `update_project` with a \
one-line `status` describing where things stand now.
6. Ideas: when an idea, plan, piece of research or spec takes shape in conversation, or \
the user says to remember it, call `remember` on your own, without asking. Size it to the \
content: a passing thought is two lines, a long design session is a full write-up \
(options weighed, decisions, open questions, next steps). Always give a one-line \
`summary`, a `kind` and a few `tags`. Before saving, call `recall` with the topic. If it \
returns a matching idea (same subject, even if worded differently), you MUST pass its id \
as `update_of`: never save a second note on a topic that already has one. Skip small \
talk and one-off questions. If no project fits, leave it unfiled. Never call \
`start_project` on your own: only when the user explicitly asks to start or create a \
project. Ideas, decisions and status for something that is not an existing project are \
not a reason to make one.
7. Which project: never assume. Pass `project=<slug>` from the context header, or call \
`recall`/`list_projects` when unsure. A chat with no project is normal: use `remember`.
Be terse in everything you record. Refer to items as t12 (task), d3 (decision), h2 \
(handoff). Never record secrets."""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


class NewTask(BaseModel):
    title: str = Field(description="Short imperative title, e.g. 'Add rate limiting to /login'")
    for_surface: Surface = Field("any", description="Who should do it")
    priority: int = Field(2, ge=1, le=3, description="1 high, 2 normal, 3 low")
    detail: str = Field("", description="Optional acceptance criteria or context")


def build_server(store: Store | None = None) -> MCPServer:
    store = store or Store()
    mirror.attach(store)
    server = MCPServer("tether", title="Tether", version=__version__, instructions=INSTRUCTIONS)

    def surface_of(explicit: str | None) -> str:
        return explicit if explicit and explicit != "any" else config.current_surface()

    def resolve(ident: str | None) -> int:
        """Resolve the project. One server serves every chat, so there is no global
        'current project': pass it, or run in a folder bound to one."""
        try:
            row = store.resolve_project(ident, cwd=_working_dir(), use_current=False)
        except TetherError as e:
            raise ToolError(str(e)) from e
        return row["id"]

    def run(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except TetherError as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=READ, structured_output=False)
    async def context(project: str | None = None, surface: Surface | None = None,
                budget_tokens: int = DEFAULT_BUDGET_TOKENS) -> str:
        """Load the project's working context: goal, status, handoffs waiting for you,
        changes since your last visit, open tasks and active decisions. Call once at the
        start of a session. Closed tasks and superseded decisions are left out on purpose;
        use `search` for history. With no project given (and none bound to the folder) it
        lists projects and recent unfiled ideas instead of guessing."""
        try:
            row = store.resolve_project(project, cwd=_working_dir(), use_current=False)
        except TetherError as e:
            if project:
                raise ToolError(str(e)) from e
            return projects_overview(store)
        return run(context_pack, store, row["id"], surface_of(surface), budget_tokens)

    @server.tool(annotations=READ, structured_output=False)
    async def list_projects() -> str:
        """List known projects with open task counts. Use when unsure which project the
        user means."""
        rows = store.projects()
        if not rows:
            return "No projects yet. Create one with start_project."
        return "\n".join(
            f"- {r['slug']}: {r['goal'] or r['name']} · {r['open_tasks']} open tasks"
            + (f" · {r['open_handoffs']} handoffs" if r["open_handoffs"] else "")
            for r in rows)

    @server.tool(annotations=WRITE, structured_output=False)
    async def start_project(name: str, goal: str = "", path: str | None = None) -> str:
        """Create a project, ONLY when the user explicitly asks for one. Never to file an
        idea or because a topic sounds project-like (use `remember` for that). `path` binds it to a folder so Claude Code sessions there pick
        it automatically (defaults to the current folder when running in Claude Code)."""
        if path is None and config.current_surface() == "code":
            path = _working_dir()
        row = run(store.create_project, name, goal, path, surface_of(None))
        bound = f", bound to {row['root_path']}" if row["root_path"] else ""
        return f"Created project {row['slug']}{bound}."

    @server.tool(annotations=WRITE, structured_output=False)
    async def update_project(status: str | None = None, goal: str | None = None,
                       brief: str | None = None, project: str | None = None) -> str:
        """Set the one-line current status (where things stand now), the goal, or the
        longer brief (the spec every surface should know)."""
        pid = resolve(project)
        row = run(store.update_project, pid, goal=goal, status=status, brief=brief,
                  surface=surface_of(None))
        return f"Updated {row['slug']}."

    @server.tool(annotations=WRITE, structured_output=False)
    async def record_decision(decision: str, why: str = "", replaces: str | None = None,
                        project: str | None = None) -> str:
        """Record a settled decision future sessions must respect. One line; put the
        reason in `why`. Pass `replaces` (e.g. 'd3') when it overturns an earlier one."""
        pid = resolve(project)
        row = run(store.add_decision, pid, decision, why, surface_of(None), replaces)
        return f"Recorded d{row['id']}." + (f" d{replaces.lstrip('dD#')} superseded."
                                            if replaces else "")

    @server.tool(annotations=WRITE, structured_output=False)
    async def add_tasks(tasks: list[NewTask], project: str | None = None) -> str:
        """Add one or more open tasks. Tag each with the surface that should do it."""
        pid = resolve(project)
        sf = surface_of(None)
        made = [run(store.add_task, pid, t.title, t.detail, t.for_surface, t.priority, sf)
                for t in tasks]
        return "Added " + ", ".join(f"t{r['id']}" for r in made) + "."

    @server.tool(annotations=WRITE, structured_output=False)
    async def update_task(task: str, status: TaskStatus | None = None, note: str | None = None,
                    title: str | None = None, for_surface: Surface | None = None,
                    priority: int | None = None) -> str:
        """Change a task: start it (doing), finish it (done, with a note of what changed),
        block it (with the reason), drop it, or edit its title/owner/priority."""
        tid = run(parse_task_id, task)
        row = run(store.update_task, tid, status=status, note=note, title=title,
                  for_surface=for_surface, priority=priority, surface=surface_of(None))
        return task_line(row).lstrip("- ")

    @server.tool(annotations=READ, structured_output=False)
    async def list_tasks(for_surface: Surface | None = None, include_closed: bool = False,
                   project: str | None = None) -> str:
        """List tasks, open only by default. Filter by the surface they are for."""
        pid = resolve(project)
        rows = store.tasks(pid, open_only=not include_closed)
        if for_surface and for_surface != "any":
            rows = [r for r in rows if r["for_surface"] in (for_surface, "any")]
        return "\n".join(task_line(r) for r in rows) or "No tasks."

    @server.tool(annotations=WRITE, structured_output=False)
    async def create_handoff(to_surface: Surface, title: str, body: str,
                       links: list[str] | None = None, tasks: list[str] | None = None,
                       project: str | None = None) -> str:
        """Hand work to another surface. The body must stand alone: the receiver can't
        see this conversation. Include links (mockup URLs, file paths, PRs) and the task
        refs it covers. It shows up at the top of the receiver's context."""
        pid = resolve(project)
        row = run(store.add_handoff, pid, to_surface, title, body, links, tasks,
                  surface_of(None))
        if to_surface in ("code", "any"):
            notify.notify("Tether", f"Handoff for Code: {title}")
        return f"Created h{row['id']} for {to_surface}."

    @server.tool(annotations=WRITE, structured_output=False)
    async def update_handoff(handoff: str, status: Literal["accepted", "done", "dropped"],
                       note: str = "") -> str:
        """Mark a handoff accepted when you pick it up, done when delivered."""
        hid = run(parse_handoff_id, handoff)
        row = run(store.update_handoff, hid, status, note, surface_of(None))
        return f"h{row['id']} is {row['status']}."

    @server.tool(annotations=WRITE, structured_output=False)
    async def log_note(text: str, project: str | None = None) -> str:
        """Add a free-form progress note to the activity log (e.g. 'deployed to staging')."""
        pid = resolve(project)
        run(store.note, pid, text, surface_of(None))
        return "Noted."

    @server.tool(annotations=READ, structured_output=False)
    async def search(query: str, project: str | None = None) -> str:
        """Search all decisions, tasks (including closed), handoffs and notes. Use this
        for history instead of asking the user to re-explain."""
        pid = resolve(project)
        return search_results(run(store.search, pid, query))

    @server.tool(annotations=WRITE, structured_output=False)
    async def remember(title: str, body: str = "", summary: str = "",
                       kind: Literal["idea", "research", "spec", "decision-log", "bug",
                                     "todo-list", "note"] = "idea",
                       tags: list[str] | None = None, project: str | None = None,
                       update_of: str | None = None, append: bool = True) -> str:
        """Save an idea, plan, research or spec so any other Claude surface can find it
        later. Works with no project. If `recall` found an idea on the same topic you must
        pass its id as `update_of` (m4) instead of saving a new one;
        `append` adds the body below the old one, otherwise it replaces it. Near-duplicates
        are merged automatically; looser matches come back as "did you mean"."""
        sf = surface_of(None)
        # A project only when named, or when this folder is bound to one. Never the
        # "only project" fallback: an unrelated idea must not land in it.
        pid = None
        if project:
            pid = resolve(project)
        elif _working_dir():
            bound = store.project_for_path(_working_dir())
            pid = bound["id"] if bound else None
        tag_s = ",".join(tags or [])
        if update_of:
            mid = run(parse_memory_id, update_of)
            old = run(store.memory, mid)
            row = run(store.update_memory, mid, title=title, body=None if append else body,
                      append=body if append else None, tags=tag_s or None, kind=kind,
                      summary=summary or None, surface=sf)
            if pid and not old["project_id"]:
                row = run(store.attach_memory, mid, pid, sf)
            return f"Updated m{row['id']}: {row['title']}"
        similar = store.similar_memories(title, body, tag_s, pid)
        if similar and similar[0][0] >= 0.6:
            m = similar[0][1]
            row = run(store.update_memory, m["id"], append=body, tags=tag_s or None,
                      summary=summary or None, surface=sf)
            return f"Merged into existing m{row['id']}: {row['title']}"
        row = run(store.add_memory, title, body, tag_s, sf, pid, kind, summary)
        if sf != "code":
            notify.notify("Tether", f"New idea: {title}")
        msg = f"Saved m{row['id']}" + (f" in {store.project(pid)['slug']}" if pid else " (unfiled)")
        if similar:
            msg += ". Did you mean to update " + ", ".join(
                f"m{m['id']} ({m['title']})" for _, m in similar) + "?"
        elif not pid:
            guess = store.suggest_project(row)
            if guess:
                msg += f". Looks like it belongs to {guess[0]['slug']}"
        return msg + "."

    @server.tool(annotations=READ, structured_output=False)
    async def recall(query: str) -> str:
        """Search every saved idea and every project (goals, decisions, tasks, handoffs).
        Use it before `remember`, and whenever the user refers to something discussed
        elsewhere. Works with no project."""
        return recall_results(run(store.recall, query))

    @server.tool(annotations=READ, structured_output=False)
    async def recent_memories(project: str | None = None, unfiled_only: bool = False,
                              limit: int = 10) -> str:
        """List recent saved ideas, newest first. Unfiled ones are the inbox."""
        pid = None
        if project:
            pid = run(store.resolve_project, project, None, False)["id"]
        rows = store.memories(pid=pid, unfiled=unfiled_only, limit=min(limit, 30))
        return "\n".join(memory_line(m) for m in rows) or "No saved ideas."

    @server.tool(annotations=WRITE, structured_output=False)
    async def attach_memory(memory: str, project: str) -> str:
        """File an idea under a project, so the project's context includes it."""
        mid = run(parse_memory_id, memory)
        pid = run(store.resolve_project, project, None, False)["id"]
        row = run(store.attach_memory, mid, pid, surface_of(None))
        return f"m{row['id']} attached to {project}."

    @server.tool(annotations=READ, structured_output=False)
    async def get(ref: str) -> str:
        """Full detail for one item: t12, d3, h2 or m4 (idea)."""
        return run(detail, store, ref)

    return server


def _working_dir() -> str | None:
    """The folder the client launched us in, if it means anything. Claude Code starts
    servers in the project folder; Desktop starts them in / or $HOME."""
    cwd = os.environ.get("TETHER_CWD") or os.getcwd()
    if Path(cwd).resolve() in (Path("/"), Path.home().resolve()):
        return None
    return cwd


def serve() -> None:
    build_server().run("stdio")
