"""Import ideas that Chat saved as markdown files (the Drive inbox).

Chat writes files like:

    ---
    title: Reading app
    kind: idea
    tags: reading, mobile
    summary: Swipe to save quotes
    project: query          (optional)
    update_of: m4           (optional)
    ---
    ...body...

The parser is forgiving: no front matter means the first heading (or file name) is
the title and the whole text is the body.
"""

from __future__ import annotations

import re
from pathlib import Path

from tether.store import MEMORY_KINDS, Store, TetherError, parse_memory_id

FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
TEXT_SUFFIXES = {".md", ".txt", ".markdown"}


def parse(text: str, filename: str = "") -> dict:
    meta: dict[str, str] = {}
    body = text.strip()
    m = FRONT_RE.match(body)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip().lower()] = v.strip()
        body = m.group(2).strip()
    title = meta.get("title", "")
    if not title:
        h = re.match(r"#+\s*(.+)", body)
        title = h.group(1).strip() if h else Path(filename).stem.replace("-", " ").replace("_", " ")
    kind = meta.get("kind", "idea").lower()
    return {"title": title.strip(), "body": body, "kind": kind if kind in MEMORY_KINDS else "idea",
            "tags": meta.get("tags", ""), "summary": meta.get("summary", ""),
            "project": meta.get("project") or meta.get("maybe-project") or "",
            "update_of": meta.get("update_of") or meta.get("update-of") or ""}


def save(store: Store, item: dict, surface: str = "chat") -> str:
    """Store one parsed item the way the `remember` tool does. Returns a one-line result."""
    pid = None
    if item["project"]:
        row = store.find_project(item["project"])
        pid = row["id"] if row else None  # a guess that doesn't exist stays unfiled
    if item["update_of"]:
        try:
            mid = parse_memory_id(item["update_of"])
            store.update_memory(mid, append=item["body"], tags=item["tags"] or None,
                                summary=item["summary"] or None, surface=surface)
            return f"updated m{mid}: {item['title']}"
        except TetherError:
            pass  # stale id: fall through and save as new
    similar = store.similar_memories(item["title"], item["body"], item["tags"], pid)
    if similar and similar[0][0] >= 0.6:
        m = similar[0][1]
        store.update_memory(m["id"], append=item["body"], tags=item["tags"] or None,
                            summary=item["summary"] or None, surface=surface)
        return f"merged into m{m['id']}: {m['title']}"
    row = store.add_memory(item["title"], item["body"], item["tags"], surface, pid,
                           item["kind"], item["summary"])
    return f"saved m{row['id']}: {row['title']}"


def import_folder(store: Store, folder: str | Path, surface: str = "chat",
                  move: bool = True) -> list[str]:
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise TetherError(f"{root} is not a folder")
    done = root / "imported"
    out = []
    for f in sorted(root.iterdir()):
        if not f.is_file() or f.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            out.append(f"{f.name}: " + save(store, parse(f.read_text(errors="replace"), f.name),
                                             surface))
        except TetherError as e:
            out.append(f"{f.name}: skipped ({e})")
            continue
        if move:
            done.mkdir(exist_ok=True)
            f.rename(done / f.name)
    return out


CHAT_PREFERENCES = """\
Tether memory (paste into claude.ai Settings > Profile > personal preferences):

When an idea, plan, piece of research or spec takes shape in our conversation, or I ask
you to remember something, save it automatically without asking, using my Google Drive
connector. Put it in the Drive folder named "Tether Inbox" as a markdown file named
after the idea (lowercase-with-dashes.md), in exactly this format:

---
title: <short title>
kind: <idea | research | spec | decision-log | bug | todo-list | note>
tags: <a few comma-separated tags>
summary: <one line>
project: <slug of an existing project only if I named it, otherwise leave out>
---
<the content. A quick thought is two lines; a long design session is a full write-up
with options weighed, decisions, open questions and next steps.>

Before creating a file, search "Tether Inbox" for one about the same idea and update that
file instead of creating a duplicate. Skip small talk and one-off questions. Never put
passwords, API keys or tokens in the file.
"""
