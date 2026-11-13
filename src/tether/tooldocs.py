"""Render docs/TOOLS.md from the MCP server's own tool definitions, so the reference can
never drift from the code. Regenerate with `python scripts/gen_tool_docs.py`; a test fails
when the checked-in file is out of date."""

from __future__ import annotations

import inspect
import os
import tempfile
from typing import Any

import anyio


def _type(schema: dict[str, Any], defs: dict[str, Any]) -> str:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _type(defs[name], defs) if name in defs and "properties" not in defs[name] else name
    if "enum" in schema:
        return " \\| ".join(f"`{v}`" for v in schema["enum"])
    if "anyOf" in schema:
        parts = [_type(s, defs) for s in schema["anyOf"] if s.get("type") != "null"]
        return " or ".join(dict.fromkeys(parts)) or "any"
    t = schema.get("type", "any")
    if t == "array":
        return f"list of {_type(schema.get('items', {}), defs)}"
    return str(t)


def _fmt_default(p: dict[str, Any]) -> str:
    if "default" not in p:
        return "required"
    d = p["default"]
    return "none" if d is None else f"`{d}`" if d != "" else "empty"


async def _tools() -> list[Any]:
    from tether.server import build_server
    return sorted(await build_server().list_tools(), key=lambda t: t.name)


def render() -> str:
    old = os.environ.get("TETHER_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TETHER_HOME"] = tmp
        try:
            tools = anyio.run(_tools)
        finally:
            if old is None:
                os.environ.pop("TETHER_HOME", None)
            else:
                os.environ["TETHER_HOME"] = old
    out = ["# Tether MCP tools", "",
           "Generated from the server's tool definitions. "
           "Do not edit by hand: run `python scripts/gen_tool_docs.py`.", "",
           f"{len(tools)} tools. Read-only tools never change anything; write tools never delete.", ""]
    for t in tools:
        ann = t.annotations
        kind = "read-only" if ann and ann.read_only_hint else "writes"
        out += [f"## `{t.name}`", "", f"*{kind}*", "", inspect.cleandoc(t.description or ""), ""]
        schema = t.input_schema or {}
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        defs = schema.get("$defs", {})
        if props:
            out += ["| Parameter | Type | Default |", "|---|---|---|"]
            for name, p in props.items():
                default = "required" if name in required else _fmt_default(p)
                out.append(f"| `{name}` | {_type(p, defs)} | {default} |")
            out.append("")
        for dname, d in defs.items():
            if "properties" in d:
                out += [f"`{dname}` fields:", "", "| Field | Type | Notes |", "|---|---|---|"]
                for fname, fp in d["properties"].items():
                    note = fp.get("description", "")
                    if fname in d.get("required", []):
                        note = ("required. " + note).strip()
                    out.append(f"| `{fname}` | {_type(fp, defs)} | {note} |")
                out.append("")
    return "\n".join(out).rstrip() + "\n"
