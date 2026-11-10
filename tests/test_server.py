import os
import sys

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tether.server import INSTRUCTIONS, build_server


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "text", None))


def call(server, name, args=None):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, args or {})
    return anyio.run(go)


def test_tools_and_instructions(store):
    server = build_server(store)

    async def go():
        async with Client(server) as client:
            tools = await client.list_tools()
            return {t.name for t in tools.tools}, client
    names, _ = anyio.run(go)
    assert {"context", "record_decision", "add_tasks", "update_task", "create_handoff",
            "update_handoff", "search", "get", "list_projects", "start_project",
            "update_project", "list_tasks", "log_note"} <= names
    assert "call `context` once" in INSTRUCTIONS


def test_full_flow_through_mcp(store, monkeypatch, tmp_path):
    monkeypatch.setenv("TETHER_SURFACE", "desktop")
    server = build_server(store)
    out = text_of(call(server, "start_project", {"name": "Shop", "goal": "Sell mugs"}))
    assert "Created project shop" in out

    out = text_of(call(server, "add_tasks", {"tasks": [
        {"title": "Mock product page", "for_surface": "design"},
        {"title": "Build cart", "for_surface": "code", "priority": 1}]}))
    assert out == "Added t1, t2."

    call(server, "record_decision", {"decision": "Shopify, not custom", "why": "speed"})
    call(server, "create_handoff", {"to_surface": "design", "title": "Product page brief",
                                    "body": "Hero, gallery, reviews", "tasks": ["t1"]})
    out = text_of(call(server, "update_task", {"task": "t2", "status": "done",
                                               "note": "cart works"}))
    assert out.startswith("t2 [done]")

    monkeypatch.setenv("TETHER_SURFACE", "design")
    pack = text_of(call(server, "context", {}))
    assert "Handed to you (1)" in pack and "Product page brief" in pack
    assert "Build cart" not in pack.split("## Open tasks")[1]
    assert "Shopify, not custom" in pack
    assert "desktop: completed t2" in pack

    hist = text_of(call(server, "search", {"query": "cart"}))
    assert "t2 [done]" in hist


def test_errors_are_tool_errors(store):
    server = build_server(store)
    assert call(server, "get", {"ref": "zz"}).is_error
    assert call(server, "context", {"project": "nope"}).is_error


def test_no_project_guessing(store):
    """An unnamed chat must get an overview, never someone else's project."""
    store.create_project("alpha")
    store.create_project("beta")
    server = build_server(store)
    out = text_of(call(server, "context", {}))
    assert "No project selected" in out and "alpha" in out and "beta" in out
    call(server, "context", {"project": "beta"})
    assert "No project selected" in text_of(call(server, "context", {}))
    assert call(server, "add_tasks", {"tasks": [{"title": "x"}]}).is_error


def test_remember_recall_and_merge(store):
    server = build_server(store)
    out = text_of(call(server, "remember", {
        "title": "Reading app quote saving", "body": "swipe to save quotes, offline first",
        "summary": "Quote-saving reader", "kind": "idea", "tags": ["reading", "mobile"]}))
    assert "Saved m1" in out and "unfiled" in out
    # same idea again merges instead of duplicating
    out = text_of(call(server, "remember", {
        "title": "Reading app quote saving", "body": "maybe Kindle import"}))
    assert "Merged into existing m1" in out
    assert len(store.memories()) == 1 and "Kindle" in store.memory(1)["body"]
    assert "Reading app" in text_of(call(server, "recall", {"query": "quotes"}))
    out = text_of(call(server, "remember", {"title": "x", "body": "more", "update_of": "m1"}))
    assert out.startswith("Updated m1")


def test_attach_and_context_ideas(store):
    store.create_project("reader", "Reading app for saving quotes")
    server = build_server(store)
    call(server, "remember", {"title": "Quote saving sketch", "body": "swipe gestures"})
    call(server, "attach_memory", {"memory": "m1", "project": "reader"})
    pack = text_of(call(server, "context", {"project": "reader"}))
    assert "Ideas saved for this project" in pack and "Quote saving sketch" in pack


def test_secrets_refused_everywhere(store):
    server = build_server(store)
    key = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    result = call(server, "remember", {"title": "api notes", "body": f"use {key}"})
    assert result.is_error and "Tether never stores secrets" in text_of(result)
    assert store.memories() == []


@pytest.mark.skipif(sys.platform == "win32", reason="posix stdio smoke test")
def test_stdio_end_to_end(tmp_path):
    """Spawn the real server the way Claude clients do."""
    env = {**os.environ, "TETHER_SURFACE": "code"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "tether", "serve"],
                                   env=env, cwd=str(tmp_path))

    async def go():
        async with Client(params) as client:
            await client.call_tool("start_project", {"name": "e2e"})
            return await client.call_tool("context", {})
    pack = text_of(anyio.run(go))
    assert pack.startswith("# e2e")


def test_remember_never_files_into_the_only_project(store, monkeypatch, tmp_path):
    store.create_project("query", "NL to SQL")
    monkeypatch.setenv("TETHER_CWD", str(tmp_path))  # an unbound folder
    server = build_server(store)
    out = text_of(call(server, "remember", {"title": "Pizza oven plans", "body": "brick"}))
    assert "unfiled" in out and store.memories()[0]["project_id"] is None
