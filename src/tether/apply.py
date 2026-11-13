"""Parse and apply TETHER UPDATE blocks.

This is the write path for surfaces that cannot call MCP (claude.ai Chat on the web
or mobile, Claude Design): Claude ends its reply with a block of plain lines, the
user copies it and runs `tether apply`. Every line maps to one store call.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from tether.store import Store, TetherError, parse_ref, parse_task_id

HEADER_RE = re.compile(r"^\s*TETHER UPDATE\b\s*(?P<project>\S+)?", re.IGNORECASE)
LINE_RE = re.compile(r"^\s*[-*]?\s*(?P<verb>[A-Za-z]+)(?P<args>[^:]*)(?::(?P<rest>.*))?$")
SURFACE_WORDS = {"code", "desktop", "cowork", "chat", "design", "any"}


@dataclass
class Op:
    verb: str
    line: str
    run: Callable[..., str] | None = None  # (store, pid, surface) -> str


@dataclass
class Parsed:
    project: str | None = None
    ops: list[Op] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def extract_block(text: str) -> tuple[str | None, list[str]]:
    """Return (project, lines) for the last TETHER UPDATE block in `text`.
    If there is no header, every line is treated as part of the block."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if HEADER_RE.match(line):
            start = i
    if start is None:
        body, project = lines, None
    else:
        header = HEADER_RE.match(lines[start])
        assert header is not None  # `start` was found with the same pattern
        project = header.group("project")
        body = lines[start + 1:]
    out = []
    for line in body:
        s = line.strip()
        if s.upper() == "END":
            break
        if not s or s.startswith("```") or s.startswith("#"):
            continue
        out.append(s)
    return project, out


def parse(text: str) -> Parsed:
    project, lines = extract_block(text)
    result = Parsed(project=project)
    for line in lines:
        try:
            result.ops.append(_parse_line(line))
        except TetherError as e:
            result.errors.append(f"{line!r}: {e}")
    return result


def _parse_line(line: str) -> Op:
    m = LINE_RE.match(line)
    if not m:
        raise TetherError("not an update line")
    verb = m.group("verb").upper()
    args = m.group("args").split()
    rest = (m.group("rest") or "").strip()

    if verb in ("STATUS", "GOAL"):
        _need(rest, verb)
        key = verb.lower()
        return Op(verb, line, lambda s, pid, sf: (
            s.update_project(pid, surface=sf, **{key: rest}), f"{key}: {rest}")[1])

    if verb == "NOTE":
        _need(rest, verb)
        return Op(verb, line, lambda s, pid, sf: (s.note(pid, rest, sf), f"note: {rest}")[1])

    if verb == "DECISION":
        _need(rest, verb)
        text, _, why = rest.partition("|")
        why = re.sub(r"^\s*why\s*:\s*", "", why, flags=re.IGNORECASE)
        supersedes = None
        if args and args[0].lower() == "replaces" and len(args) > 1:
            supersedes = parse_ref(args[1])[1]
        return Op(verb, line, lambda s, pid, sf: "d{} {}".format(
            s.add_decision(pid, text.strip(), why.strip(), sf, supersedes)["id"], text.strip()))

    if verb == "TASK":
        _need(rest, verb)
        for_surface, priority = "any", 2
        for a in args:
            low = a.lower()
            if low in SURFACE_WORDS:
                for_surface = low
            elif re.fullmatch(r"p[123]", low):
                priority = int(low[1])
            else:
                raise TetherError(f"unknown TASK option {a!r}")
        title, _, detail = rest.partition("::")
        title, _, why = title.partition("|")  # "| why ..." is a rationale, not part of the title
        why = re.sub(r"^\s*why\s*:?\s*", "", why, flags=re.IGNORECASE).strip()
        if why:
            detail = "why: " + why + ("\n" + detail.strip() if detail.strip() else "")
        return Op(verb, line, lambda s, pid, sf: "t{} {}".format(
            s.add_task(pid, title.strip(), detail.strip(), for_surface, priority, sf)["id"],
            title.strip()))

    if verb in ("DONE", "DROP", "DROPPED", "START", "DOING", "BLOCKED", "REOPEN"):
        if not args:
            raise TetherError(f"{verb} needs a task ref like t12")
        tid = parse_task_id(args[0])
        status = {"DONE": "done", "DROP": "dropped", "DROPPED": "dropped", "START": "doing",
                  "DOING": "doing", "BLOCKED": "blocked", "REOPEN": "todo"}[verb]
        return Op(verb, line, lambda s, pid, sf: "t{} → {}".format(
            _own_task(s, pid, tid) and s.update_task(
                tid, status=status, note=rest or None, surface=sf)["id"], status))

    if verb == "HANDOFF":
        if not args or args[0].lower() not in SURFACE_WORDS:
            raise TetherError("HANDOFF needs a target surface, e.g. HANDOFF code: title")
        to = args[0].lower()
        parts = [p.strip() for p in rest.split("::")]
        title = parts[0] if parts else ""
        _need(title, verb)
        body = parts[1] if len(parts) > 1 else ""
        links = [u.strip() for u in parts[2].split(",") if u.strip()] if len(parts) > 2 else []
        return Op(verb, line, lambda s, pid, sf: "h{} → {}: {}".format(
            s.add_handoff(pid, to, title, body, links, surface=sf)["id"], to, title))

    raise TetherError(f"unknown verb {verb}")


def apply(store: Store, pid: int, parsed: Parsed, surface: str) -> list[str]:
    """Run every op; returns one line per result. Stops at nothing: a failed op is
    reported and the rest still run, so one typo doesn't lose a whole session."""
    out = []
    for op in parsed.ops:
        try:
            assert op.run is not None
            out.append("ok  " + op.run(store, pid, surface))
        except TetherError as e:
            out.append(f"err {op.line}: {e}")
    return out


def _need(value: str, verb: str) -> None:
    if not value.strip():
        raise TetherError(f"{verb} needs text after ':'")


def _own_task(store: Store, pid: int, tid: int) -> bool:
    if store.task(tid)["project_id"] != pid:
        raise TetherError(f"t{tid} belongs to another project")
    return True
