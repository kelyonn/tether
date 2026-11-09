"""Text views of the store: the compact context pack, item details, and the full
markdown mirror.

The pack is the product. It has to be small enough to load at the start of every
session, so it never includes closed tasks or superseded decisions, and it trims
itself to a character budget (roughly 4 chars per token).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from tether import config
from tether.store import STALE_DAYS, Store

DEFAULT_BUDGET_TOKENS = 1500
PRIORITY = {1: "P1", 2: "", 3: "P3"}


def ago(ts: str | None) -> str:
    if not ts:
        return ""
    then = datetime.fromisoformat(ts)
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


def clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def task_line(t: sqlite3.Row, show_surface: bool = True) -> str:
    bits = [f"t{t['id']}"]
    if t["status"] != "todo":
        bits.append(f"[{t['status']}]")
    if PRIORITY.get(t["priority"]):
        bits.append(PRIORITY[t["priority"]])
    if show_surface and t["for_surface"] != "any":
        bits.append(f"({t['for_surface']})")
    line = " ".join(bits) + " " + clip(t["title"], 120)
    if t["note"] and t["status"] in ("doing", "blocked"):
        line += f" — {clip(t['note'], 80)}"
    return "- " + line


def memory_line(m: sqlite3.Row) -> str:
    where = m["project_slug"] if "project_slug" in m.keys() and m["project_slug"] else "unfiled"
    text = m["summary"] or clip(m["body"], 100)
    tags = f" #{m['tags'].replace(',', ' #')}" if m["tags"] else ""
    line = f"- m{m['id']} [{m['kind']}] {clip(m['title'], 80)} ({where}, {ago(m['updated_at'])})"
    return line + (f" — {clip(text, 100)}" if text else "") + tags


def decision_line(d: sqlite3.Row, why_len: int = 100) -> str:
    line = f"- d{d['id']} {clip(d['text'], 160)}"
    if d["why"] and why_len:
        line += f" — why: {clip(d['why'], why_len)}"
    return line


def handoff_block(h: sqlite3.Row, body_len: int = 400) -> list[str]:
    head = f"- h{h['id']} from {h['from_surface']} ({ago(h['created_at'])})"
    if h["status"] == "accepted":
        head += " [accepted]"
    out = [f"{head}: {clip(h['title'], 120)}"]
    if h["body"] and body_len:
        out.append(f"  {clip(h['body'], body_len)}")
    links = json.loads(h["links"])
    if links:
        out.append("  links: " + ", ".join(links))
    tids = json.loads(h["task_ids"])
    if tids:
        out.append("  tasks: " + ", ".join(f"t{t}" for t in tids))
    return out


def context_pack(store: Store, pid: int, surface: str = "any",
                 budget_tokens: int = DEFAULT_BUDGET_TOKENS, mark_seen: bool = True) -> str:
    """Everything a fresh session needs, newest and most relevant first."""
    p = store.project(pid)
    budget = max(400, budget_tokens) * 4
    tasks = store.tasks(pid)
    mine = [t for t in tasks if surface == "any" or t["for_surface"] in (surface, "any")]
    mine_ids = {t["id"] for t in mine}
    others = [t for t in tasks if t["id"] not in mine_ids]
    decisions = store.decisions(pid)
    handoffs = store.handoffs(pid, to_surface=surface)
    counts = store.task_counts(pid)
    ideas = store.memories(pid=pid, limit=6)
    maybe = [m for m in store.memories(unfiled=True, limit=30)
             if (g := store.suggest_project(m)) and g[0]["id"] == pid][:3]
    unfiled_n = len(store.memories(unfiled=True, limit=200))
    code_tasks = store.session_tasks(pid, limit=5) if surface in ("code", "any") else []
    drive = bool(config.load_config().get("drive_inbox"))
    seen = store.last_seen(pid, surface) if surface != "any" else None
    if seen is None:
        recent = [e for e in store.events(pid, limit=20) if e["kind"] != "session-task"][:8]
        recent_title = "Recent activity"
    else:
        recent = [e for e in store.events(pid, limit=30, after_id=seen)
                  if e["surface"] != surface and e["kind"] != "session-task"]
        recent_title = f"Since {surface} was last here"

    # Shrinkable limits, reduced in this order until the pack fits.
    lim = {"recent": len(recent), "others": len(others), "decisions": len(decisions),
           "why": 100, "mine": len(mine), "body": 400, "brief": 500,
           "ideas": len(ideas)}
    shrink_order = [("recent", 3), ("others", 5), ("ideas", 3), ("why", 0), ("decisions", 10),
                    ("brief", 0), ("body", 150), ("recent", 0), ("others", 0), ("ideas", 1),
                    ("decisions", 5), ("mine", 15)]

    def build() -> str:
        L = [f"# {p['name']} · tether [{p['slug']}]"]
        if p["goal"]:
            L.append(f"Goal: {p['goal']}")
        if p["status"]:
            L.append(f"Status: {p['status']} ({ago(p['updated_at'])})")
        if p["brief"] and lim["brief"]:
            L.append(f"Brief: {clip(p['brief'], lim['brief'])}")
        L.append(f"You are: {surface}. Refs: t=task d=decision h=handoff.")

        if handoffs:
            L += ["", f"## Handed to you ({len(handoffs)})"]
            for h in handoffs:
                L += handoff_block(h, lim["body"])

        if recent and lim["recent"]:
            L += ["", f"## {recent_title}"]
            for e in recent[: lim["recent"]]:
                L.append(f"- {ago(e['ts'])} {e['surface']}: {clip(e['summary'], 140)}")

        if ideas and lim["ideas"]:
            L += ["", f"## Ideas saved for this project ({len(ideas)}; recall or get m<id> for the rest)"]
            L += [memory_line(m) for m in ideas[: lim["ideas"]]]
        if maybe and lim["ideas"]:
            L += ["", "## Unfiled ideas that may belong here (attach_memory to confirm)"]
            L += [memory_line(m) for m in maybe]
        if code_tasks:
            L += ["", "## Claude Code's own unfinished tasks (from its last sessions)"]
            L += [task_line(t, show_surface=False) for t in code_tasks]
        if drive:
            L += ["", "## Drive inbox",
                  "Phone/web Chat saves ideas to the Drive folder 'Tether Inbox'. If your Google "
                  "Drive tools are available, check it for new files, `remember` each one, then "
                  "move it to 'Tether Inbox/imported'."]
        if unfiled_n:
            L += ["", f"({unfiled_n} unfiled ideas: recent_memories unfiled_only=true)"]

        closed = counts["done"] + counts["dropped"]
        L += ["", f"## Open tasks ({len(tasks)} open, {closed} closed and hidden)"]
        if not tasks:
            L.append("- none")
        if mine:
            if others:
                L.append(f"For {surface}:")
            L += [task_line(t, show_surface=surface == "any") for t in mine[: lim["mine"]]]
            if len(mine) > lim["mine"]:
                L.append(f"- …{len(mine) - lim['mine']} more (list_tasks)")
        if others:
            shown = others[: lim["others"]]
            if shown:
                L.append("For others:")
                L += [task_line(t) for t in shown]
            if len(others) > len(shown):
                L.append(f"- …{len(others) - len(shown)} more for other surfaces (list_tasks)")

        if decisions:
            n = lim["decisions"]
            L += ["", f"## Decisions ({len(decisions)} active, newest first)"]
            L += [decision_line(d, lim["why"]) for d in decisions[:n]]
            if len(decisions) > n:
                L.append(f"- …{len(decisions) - n} older (search or get d<id>)")
        return "\n".join(L)

    text = build()
    for key, value in shrink_order:
        if len(text) <= budget:
            break
        if lim[key] > value:
            lim[key] = value
            text = build()
    if mark_seen and surface != "any":
        store.mark_seen(pid, surface)
    return text


def detail(store: Store, ref: str) -> str:
    table, r = store.get(ref)
    if table == "tasks":
        L = [f"t{r['id']} [{r['status']}] {r['title']}",
             f"for: {r['for_surface']} · priority: {r['priority']} · by {r['created_by']} "
             f"{ago(r['created_at'])}"]
        if r["detail"]:
            L += ["", r["detail"]]
        if r["note"]:
            L += ["", f"note: {r['note']}"]
    elif table == "decisions":
        L = [f"d{r['id']} [{r['status']}] {r['text']}"]
        if r["why"]:
            L.append(f"why: {r['why']}")
        L.append(f"by {r['surface']} {ago(r['created_at'])}")
        if r["superseded_by"]:
            L.append(f"superseded by d{r['superseded_by']}")
        prev = store.db.execute("SELECT id FROM decisions WHERE superseded_by=?",
                                (r["id"],)).fetchall()
        if prev:
            L.append("replaces " + ", ".join(f"d{x['id']}" for x in prev))
    elif table == "memories":
        proj = store.project(r["project_id"])["slug"] if r["project_id"] else "unfiled"
        L = [f"m{r['id']} [{r['kind']}] {r['title']}",
             f"{proj} · by {r['surface']} · updated {ago(r['updated_at'])}"
             + (f" · tags: {r['tags']}" if r["tags"] else "")]
        if r["summary"]:
            L.append(f"summary: {r['summary']}")
        if r["body"]:
            L += ["", r["body"]]
    else:
        L = [f"h{r['id']} [{r['status']}] {r['from_surface']} → {r['to_surface']}: {r['title']}",
             f"created {ago(r['created_at'])}"]
        if r["body"]:
            L += ["", r["body"]]
        for key in ("links", "task_ids"):
            vals = json.loads(r[key])
            if vals:
                L.append(f"{key}: " + ", ".join(f"t{v}" if key == "task_ids" else v for v in vals))
        if r["note"]:
            L.append(f"note: {r['note']}")
    return "\n".join(L)


def search_results(results: list[tuple[str, sqlite3.Row]]) -> str:
    if not results:
        return "no matches"
    L = []
    for table, r in results:
        if table == "tasks":
            L.append(f"- t{r['id']} [{r['status']}] {clip(r['title'], 120)}")
        elif table == "decisions":
            L.append(f"- d{r['id']} [{r['status']}] {clip(r['text'], 140)}")
        elif table == "handoffs":
            L.append(f"- h{r['id']} [{r['status']}] {r['from_surface']}→{r['to_surface']}: "
                     f"{clip(r['title'], 120)}")
        elif table == "memories":
            L.append(memory_line(r))
        else:
            L.append(f"- note {ago(r['ts'])} {r['surface']}: {clip(r['summary'], 140)}")
    return "\n".join(L)


def recall_results(results: list[tuple[str, sqlite3.Row, str | None]]) -> str:
    if not results:
        return "no matches"
    L = []
    for kind, r, slug in results:
        if kind == "projects":
            L.append(f"- project {r['slug']}: {clip(r['goal'] or r['name'], 100)}")
        elif kind == "memories":
            L.append(memory_line(r))
        else:
            L.append(search_results([(kind, r)]) + f"  [{slug}]")
    return "\n".join(L)


def projects_overview(store: Store) -> str:
    """What `context` returns when no project was named: orientation, not a guess."""
    L = ["No project selected. Pass project=<slug> to load one."]
    rows = store.projects()
    if rows:
        L += ["", "## Projects"]
        for r in rows:
            L.append(f"- {r['slug']}: {clip(r['goal'] or r['name'], 90)} · {r['open_tasks']} open"
                     + (f" · Code working on {r['code_doing']}" if r["code_doing"] else ""))
    box = store.inbox(limit=8)
    if box["fresh"]:
        L += ["", "## Unfiled ideas"]
        L += [memory_line(m) for m in box["fresh"]]
    if box["older"]:
        L.append(f"({len(box['older'])} older ideas, untouched {STALE_DAYS}+ days: "
                 "recent_memories unfiled_only=true)")
    if not rows and not box["fresh"]:
        L.append("Nothing saved yet. Use remember for ideas or start_project for a project.")
    return "\n".join(L)


def mirror_markdown(store: Store, pid: int) -> str:
    """The full, untrimmed state for surfaces that can only read files (Chat, Design)."""
    p = store.project(pid)
    L = [f"# {p['name']}", "",
         f"> Tether mirror, generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. "
         "Read-only copy: changes go through Tether. To send changes back, end your reply "
         "with a TETHER UPDATE block (format at the bottom).", ""]
    if p["goal"]:
        L.append(f"**Goal:** {p['goal']}  ")
    if p["status"]:
        L.append(f"**Status:** {p['status']}  ")
    if p["brief"]:
        L += ["", "## Brief", "", p["brief"]]
    handoffs = store.handoffs(pid)
    if handoffs:
        L += ["", "## Open handoffs", ""]
        for h in handoffs:
            L.append(f"### h{h['id']} · {h['from_surface']} → {h['to_surface']}: {h['title']}")
            if h["body"]:
                L += ["", h["body"]]
            links = json.loads(h["links"])
            if links:
                L += [""] + [f"- {u}" for u in links]
            L.append("")
    tasks = store.tasks(pid)
    L += ["", "## Open tasks", ""]
    L += [task_line(t) for t in tasks] or ["- none"]
    decisions = store.decisions(pid)
    L += ["", "## Decisions", ""]
    L += [decision_line(d, why_len=400) for d in decisions] or ["- none"]
    ideas = store.memories(pid=pid, limit=50)
    if ideas:
        L += ["", "## Ideas", ""]
        for m in ideas:
            L += [f"### m{m['id']} · {m['title']} ({m['kind']})", ""]
            if m["summary"]:
                L += [f"*{m['summary']}*", ""]
            if m["body"]:
                L += [m["body"], ""]
    L += ["", "## Recent activity", ""]
    L += [f"- {e['ts'][:16].replace('T', ' ')} {e['surface']}: {e['summary']}"
          for e in store.events(pid, limit=15)]
    L += ["", "## Sending updates back", "", UPDATE_FORMAT_HELP]
    return "\n".join(L) + "\n"


UPDATE_FORMAT_HELP = """\
End the conversation with a block like this. The user pastes it into `tether apply`.

```
TETHER UPDATE <project-slug>
DECISION: Use Stripe Checkout, not Elements | why: less PCI scope
TASK code P1: Wire checkout success webhook
TASK design: Empty state for the billing page
DONE t12: shipped in PR 41
STATUS: Billing flow designed; implementation next
HANDOFF code: Billing page spec :: Layout, states and copy are final. See link. :: https://example.com/mock
NOTE: Talked to two users, both want annual pricing
END
```"""
