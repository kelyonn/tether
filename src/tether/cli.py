"""The `tether` command. Everything Claude can do through MCP, you can do here."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from tether import __version__, config, gitlink, hooks, inboxfmt, install, mirror, sync
from tether import apply as apply_mod
from tether.render import (
    UPDATE_FORMAT_HELP,
    ago,
    context_pack,
    detail,
    memory_line,
    recall_results,
    search_results,
    task_line,
)
from tether.store import MEMORY_KINDS, Store, TetherError, parse_handoff_id, parse_memory_id, parse_task_id

PASTE_SURFACES = ("chat", "design")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except TetherError as e:
        print(f"tether: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except sqlite3.Error as e:
        if os.environ.get("TETHER_DEBUG"):
            raise
        print(f"tether: {_db_problem(e)}", file=sys.stderr)
        return 1
    except PermissionError as e:
        if os.environ.get("TETHER_DEBUG"):
            raise
        print(f"tether: no permission to use {e.filename or 'a file it needs'}. "
              "Check the folder's permissions, or set TETHER_HOME to a folder you own.", file=sys.stderr)
        return 1


def _db_problem(e: sqlite3.Error) -> str:
    """What went wrong with the ledger, and what to do next, in plain words."""
    msg = str(e).lower()
    if "locked" in msg or "busy" in msg:
        return ("the ledger is busy, probably another Tether process is writing. "
                "Wait a moment and try again.")
    if "malformed" in msg or "not a database" in msg or "corrupt" in msg:
        return (f"the ledger file looks damaged ({e}). Run `tether doctor` to see what is wrong. "
                "If it cannot be repaired, restore a backup with `tether import <file>`.")
    if "readonly" in msg or "read-only" in msg:
        return "the ledger is read-only. Check the permissions of the folder that holds it (`tether doctor` shows where)."
    return f"the ledger could not be used ({e}). `tether doctor` can usually tell why. Set TETHER_DEBUG=1 for details."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tether",
        description="Shared project state (decisions, tasks, handoffs) for every Claude surface.")
    p.add_argument("--version", action="version", version=f"tether {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--project", help="project slug (default: the one bound to "
                        "this folder, else the current one)")
    common.add_argument("--as", dest="surface", default="cli", choices=config.SURFACES,
                        help="record changes as this surface (default: cli)")
    sub = p.add_subparsers(metavar="command")

    def cmd(name, func, help, aliases=()):
        sp = sub.add_parser(name, help=help, description=help, aliases=list(aliases),
                            parents=[common])
        sp.set_defaults(func=func)
        return sp

    s = cmd("install", cmd_install, "register Tether with Claude Code and Claude Desktop")
    s.add_argument("--only", choices=["code", "desktop"], help="install for one client only")
    s.add_argument("--no-hook", action="store_true", help="skip the Claude Code SessionStart hook")
    s.add_argument("--dry-run", action="store_true", help="show what would change")
    s.add_argument("--uninstall", action="store_true", help="remove Tether from the clients")
    s.add_argument("--no-check", action="store_true",
                   help="skip the end-to-end check that normally runs after installing")

    s = cmd("doctor", cmd_doctor, "check the install and the store")
    s.add_argument("--deep", action="store_true",
                   help="also start the real server in a temp store and drive it end to end")
    s.add_argument("--fix", action="store_true",
                   help="repair what can be repaired (rebuild the search index or your sync journal)")

    s = cmd("export", cmd_export, "back up everything to one JSON file")
    s.add_argument("file", nargs="?", help="where to write it (default: tether-backup-DATE.json)")

    s = cmd("import", cmd_import, "restore a backup into an empty store")
    s.add_argument("file")

    s = cmd("init", cmd_init, "create a project bound to this folder (or its git root)")
    s.add_argument("name", nargs="?", help="project name (default: folder name)")
    s.add_argument("--goal", default="", help="one-line goal")
    s.add_argument("--path", help="folder to bind (default: git root or cwd)")
    s.add_argument("--unbound", action="store_true",
                   help="don't bind to a folder (for non-code projects)")

    s = cmd("use", cmd_use, "set the current project for clients without a folder (Desktop)")
    s.add_argument("project_ref", metavar="project")

    cmd("ls", cmd_ls, "list projects", aliases=["projects"])

    s = cmd("ctx", cmd_ctx, "print the context pack", aliases=["context"])
    s.add_argument("--for", dest="for_surface", default="any", choices=config.SURFACES,
                   help="tailor to a surface; chat/design add paste-back instructions")
    s.add_argument("--budget", type=int, default=1500, help="token budget (default 1500)")
    s.add_argument("--copy", action="store_true", help="copy to the clipboard")

    s = cmd("tasks", cmd_tasks, "list tasks")
    s.add_argument("--all", action="store_true", help="include closed tasks")
    s.add_argument("--for", dest="for_surface", choices=config.SURFACES)

    s = cmd("add", cmd_add, "add a task")
    s.add_argument("title")
    s.add_argument("--for", dest="for_surface", default="any", choices=config.SURFACES)
    s.add_argument("-P", "--priority", type=int, default=2, choices=[1, 2, 3])
    s.add_argument("--detail", default="")

    for name, status, help in (("done", "done", "finish a task"),
                               ("start", "doing", "start a task"),
                               ("block", "blocked", "mark a task blocked"),
                               ("drop", "dropped", "drop a task"),
                               ("reopen", "todo", "reopen a task")):
        s = cmd(name, cmd_set_status, help)
        s.add_argument("task")
        s.add_argument("note", nargs="?", default=None)
        s.set_defaults(status=status)

    s = cmd("decide", cmd_decide, "record a decision")
    s.add_argument("text")
    s.add_argument("--why", default="")
    s.add_argument("--replaces", help="decision it overturns, e.g. d3")

    s = cmd("status", cmd_status, "set the project's one-line status (no arg: show it)")
    s.add_argument("text", nargs="?")

    s = cmd("goal", cmd_goal, "set the project's goal (no arg: show it)")
    s.add_argument("text", nargs="?")

    s = cmd("brief", cmd_brief, "set the long-form brief from a file or stdin (no arg: show it)")
    s.add_argument("file", nargs="?", help="markdown file, or - for stdin")

    s = cmd("handoff", cmd_handoff, "hand work to another surface")
    s.add_argument("to", choices=config.SURFACES)
    s.add_argument("title")
    s.add_argument("--body", default="", help="self-contained brief (or - for stdin)")
    s.add_argument("--link", action="append", default=[], help="URL or path (repeatable)")
    s.add_argument("--task", action="append", default=[], help="task ref it covers (repeatable)")

    s = cmd("inbox", cmd_inbox, "list open handoffs and unfiled ideas; "
            "`inbox import <folder>` imports ideas Chat saved as files")
    s.add_argument("what", nargs="?", choices=["import"])
    s.add_argument("folder", nargs="?", help="folder of markdown files (for import)")
    s.add_argument("--keep", action="store_true", help="import: don't move files to imported/")
    s.add_argument("--for", dest="for_surface", choices=config.SURFACES)

    s = cmd("remember", cmd_remember, "save an idea (works with no project)")
    s.add_argument("title")
    s.add_argument("--body", default="", help="the content (or - for stdin)")
    s.add_argument("--kind", default="idea", choices=MEMORY_KINDS)
    s.add_argument("--tags", default="")
    s.add_argument("--summary", default="")
    s.add_argument("--update", metavar="MEM", help="add to an existing idea, e.g. m4")
    s.add_argument("--attach", action="store_true", help="file it under the current project")

    s = cmd("recall", cmd_recall, "search ideas and every project")
    s.add_argument("query", nargs="+")

    s = cmd("memories", cmd_memories, "list saved ideas", aliases=["ideas"])
    s.add_argument("--unfiled", action="store_true")
    s.add_argument("--all", action="store_true", help="every project, not just the current one")
    s.add_argument("-n", type=int, default=20)

    s = cmd("attach", cmd_attach, "file an idea under a project")
    s.add_argument("memory", help="e.g. m4")
    s.add_argument("to", nargs="?", help="project slug (default: current/bound project)")

    s = cmd("unremember", cmd_unremember, "archive an idea (kept, hidden)")
    s.add_argument("memory")

    s = cmd("promote", cmd_promote, "turn one of Claude Code's own tasks into a project task")
    s.add_argument("task")

    s = cmd("setup", cmd_setup, "one-time setup helpers (setup chat: phone/web Chat -> Tether)")
    s.add_argument("what", choices=["chat"])

    s = cmd("config", cmd_config, "show or change settings (notify on|off)")
    s.add_argument("key", nargs="?", choices=["notify"])
    s.add_argument("value", nargs="?", choices=["on", "off"])

    s = cmd("ui", cmd_ui, "open the live view in your browser (editable, local only)")
    s.add_argument("--port", type=int, default=7878)
    s.add_argument("--no-open", action="store_true")

    s = cmd("watch", cmd_watch, "live terminal view (read-only)")

    s = cmd("eval", cmd_eval, "measure how well Claude follows Tether's rules (calls the "
            "Claude API; costs money)")
    s.add_argument("--provider", default="anthropic", choices=["anthropic", "nvidia"],
                   help="nvidia = free NIM API (needs NVIDIA_API_KEY); tests a non-Claude model")
    s.add_argument("--model", default=None)
    s.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    s.add_argument("--runs", type=int, default=1, help="runs per scenario")
    s.add_argument("--only", action="append", help="scenario name (repeatable)")
    s.add_argument("--scenarios", help="scenarios file (default: the bundled set)")
    s.add_argument("--out", default="evals/results", help="where to save the JSON report")
    s.add_argument("--list", action="store_true", help="list scenarios and exit")

    s = cmd("service", cmd_service, "keep the live view running in the background at login "
            "(macOS)")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--port", type=int, default=7878)

    s = cmd("sync", cmd_sync, "two-Mac sync through a shared folder (iCloud Drive by default)")
    s.add_argument("action", nargs="?", default="now", choices=["now", "setup", "status", "compact", "off"],
                   help="now (default): push and pull; setup: turn on; status; "
                        "compact: shrink this Mac's journal; off")
    s.add_argument("--dir", help="setup: shared folder (default: iCloud Drive/Tether)")

    s = cmd("accept", cmd_handoff_status, "accept a handoff")
    s.add_argument("handoff")
    s.add_argument("note", nargs="?", default="")
    s.set_defaults(status="accepted")
    s = cmd("deliver", cmd_handoff_status, "mark a handoff done")
    s.add_argument("handoff")
    s.add_argument("note", nargs="?", default="")
    s.set_defaults(status="done")

    s = cmd("note", cmd_note, "add a progress note to the log")
    s.add_argument("text")

    s = cmd("log", cmd_log, "show recent activity")
    s.add_argument("-n", type=int, default=20)

    s = cmd("search", cmd_search, "search decisions, tasks, handoffs and notes")
    s.add_argument("query", nargs="+")

    s = cmd("show", cmd_show, "show one item (t12, d3, h2)")
    s.add_argument("ref")

    s = cmd("apply", cmd_apply, "apply a TETHER UPDATE block pasted from Chat or Design")
    s.add_argument("file", nargs="?", help="file with the block (default: stdin)")
    s.add_argument("--paste", action="store_true", help="read from the clipboard")
    s.add_argument("--dry-run", action="store_true")

    s = cmd("mirror", cmd_mirror, "write markdown copies to a folder Chat/Design can read")
    s.add_argument("--dir", help="set the mirror folder (e.g. a Google Drive folder)")
    s.add_argument("--off", action="store_true", help="stop mirroring")

    s = cmd("archive", cmd_archive, "archive a project (hidden from lists)")
    s.add_argument("--undo", action="store_true")

    cmd("archived", cmd_archived, "list what is archived (by you, or for being unused)")

    s = cmd("restore", cmd_restore, "bring an archived idea (m12) or project back")
    s.add_argument("ref", help="m12 for an idea, or a project name")

    s = cmd("sweep", cmd_sweep, "archive memories and projects unused for 30 days")
    s.add_argument("--dry-run", action="store_true", help="show what would be archived")

    cmd("serve", cmd_serve, "run the MCP server on stdio (clients start this)")

    s = cmd("git", cmd_git, "close tasks from commit messages (\"fixes t12\")")
    s.add_argument("action", choices=["install", "uninstall"])
    s.add_argument("--repo", help="repository (default: current folder)")

    s = cmd("hook", cmd_hook, "hook entry points (Claude Code session start, git post-commit)")
    s.add_argument("event", choices=["session-start", "post-tool-use", "post-commit"])

    return p


# -- helpers -------------------------------------------------------------------------

def _store() -> Store:
    store = Store()
    mirror.attach(store)
    return store


def _pid(store: Store, args) -> int:
    return store.resolve_project(args.project, cwd=os.getcwd())["id"]


def _git_root(path: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def _copy(text: str) -> bool:
    for tool in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["clip"]):
        if shutil.which(tool[0]):
            subprocess.run(tool, input=text, text=True)
            return True
    return False


def _paste() -> str:
    for tool in (["pbpaste"], ["wl-paste"], ["xclip", "-selection", "clipboard", "-o"]):
        if shutil.which(tool[0]):
            return subprocess.run(tool, capture_output=True, text=True).stdout
    raise TetherError("no clipboard tool found; pipe the text in instead")


# -- commands ------------------------------------------------------------------------

def cmd_install(args) -> int:
    steps = []
    if args.uninstall:
        if args.only in (None, "code"):
            steps += [install.uninstall_code, install.uninstall_hook]
        if args.only in (None, "desktop"):
            steps.append(install.uninstall_desktop)
    else:
        if args.only in (None, "code"):
            steps.append(install.install_code)
            if not args.no_hook:
                steps.append(install.install_hook)
        if args.only in (None, "desktop"):
            steps.append(install.install_desktop)
    failed = False
    for step in steps:
        try:
            r = step(dry_run=args.dry_run)
            print(f"{r.client:18} {r.action:13} {r.detail}")
        except (ValueError, OSError) as e:
            failed = True
            print(f"{step.__name__:18} failed        {e}", file=sys.stderr)
    if not args.uninstall and not args.dry_run and not failed:
        if not args.no_check:
            print("\nChecking that it works (a throwaway ledger, your data is not touched)...")
            from tether import selftest
            checks = selftest.run()
            for c in checks:
                print(f"  {'ok  ' if c.ok else 'FAIL'} {c.name}" + (f" — {c.detail}" if c.detail else ""))
            if not all(c.ok for c in checks):
                print("\nSomething did not work. Run `tether doctor --deep` for details.")
                return 1
            print("  All good.")
        print("\nNext:\n  1. cd into a project folder and run `tether init --goal \"what you are building\"`"
              "\n  2. start Claude Code (or restart Claude Desktop) there; it will already know the project"
              "\n  3. open the live view any time with `tether ui`")
    return 1 if failed else 0


def cmd_doctor(args) -> int:
    print(f"tether {__version__} · python {sys.version.split()[0]} · {sys.executable}")
    print(f"store: {config.db_path()}")
    store = _store()
    print(f"projects: {len(store.projects())}")
    md = mirror.mirror_dir()
    print(f"mirror: {md or 'off'}")
    print("sync: " + (_sync_line(store) if store.syncer else "off (tether sync setup)"))
    from tether import service
    print(f"live view service: {service.status()}")
    bad = False
    for r in install.status():
        print(f"{r.client:18} {r.action:10} {r.detail}")
        bad = bad or r.action in ("error", "stale")
    try:
        import mcp  # noqa: F401
        print("mcp sdk: ok")
    except ImportError as e:
        print(f"mcp sdk: missing ({e})")
        return 1
    from tether import health
    findings = health.check(store)
    print("\nledger health:")
    for f in findings:
        print(f"  {'ok  ' if f.ok else 'FAIL'} {f.name}" + (f" — {f.detail}" if f.detail else ""))
    if getattr(args, "fix", False):
        repairs = health.repair(findings)
        for line in repairs:
            print(f"  fixed {line}")
        if repairs:
            findings = health.check(store)
        else:
            print("  nothing here can be fixed automatically")
    elif any(not f.ok and f.fix for f in findings):
        print("  Some of these can be repaired: run `tether doctor --fix`.")
    ledger_bad = not all(f.ok for f in findings)
    bad = bad or ledger_bad
    if args.deep:
        from tether import selftest
        print("\ndeep check (temporary store, real server over stdio):")
        checks = selftest.run()
        for c in checks:
            print(f"  {'ok  ' if c.ok else 'FAIL'} {c.name}" + (f" — {c.detail}" if c.detail else ""))
        bad = bad or not all(c.ok for c in checks)
    if bad:
        if ledger_bad:
            print("\nProblems found in the ledger (above)."
                  + ("" if getattr(args, "fix", False) else " `tether doctor --fix` repairs what it can."))
        if any(r.action in ("error", "stale") for r in install.status()):
            print("\nA client install is stale. Re-run `tether install` to repair it.")
        elif not ledger_bad:
            print("\nProblems found. Run `tether doctor --deep` for details.")
    return 1 if bad else 0


def cmd_export(args) -> int:
    from datetime import date

    from tether import portable
    path = args.file or f"tether-backup-{date.today().isoformat()}.json"
    counts = portable.write_export(_store(), path)
    print(f"Wrote {path}: " + ", ".join(f"{n} {t}" for t, n in counts.items()) + ".")
    print("It holds all your notes. Keep it somewhere private.")
    return 0


def cmd_import(args) -> int:
    from tether import portable
    counts = portable.import_data(_store(), portable.read_export(args.file))
    print("Restored " + ", ".join(f"{n} {t}" for t, n in counts.items()) + ".")
    return 0


def cmd_init(args) -> int:
    store = _store()
    path = None
    if not args.unbound:
        path = args.path or _git_root(os.getcwd()) or os.getcwd()
        existing = store.project_for_path(path)
        if existing and existing["root_path"] == os.path.realpath(path):
            print(f"{path} is already bound to {existing['slug']}.")
            return 0
    name = args.name or Path(path or os.getcwd()).name
    same = store.find_project(name)
    if same and path and not (same["root_path"] and os.path.isdir(same["root_path"])):
        # e.g. synced from your other Mac, where the folder lives somewhere else
        row = store.update_project(same["id"], root_path=path, surface=args.surface)
        if args.goal:
            store.update_project(row["id"], goal=args.goal, surface=args.surface)
        print(f"Bound existing project {row['slug']} to {row['root_path']}.")
        return 0
    row = store.create_project(name, args.goal, path, args.surface)
    cfg = config.load_config()
    cfg["current_project"] = row["slug"]
    config.save_config(cfg)
    print(f"Created {row['slug']}" + (f", bound to {row['root_path']}" if path else "") + ".")
    if not args.goal:
        print("Set a goal with: tether goal \"...\"")
    return 0


def cmd_use(args) -> int:
    store = _store()
    row = store.find_project(args.project_ref)
    if not row:
        raise TetherError(f"no project {args.project_ref!r}")
    cfg = config.load_config()
    cfg["current_project"] = row["slug"]
    config.save_config(cfg)
    print(f"Current project: {row['slug']}")
    return 0


def cmd_ls(args) -> int:
    store = _store()
    rows = store.projects()
    if not rows:
        print("No projects. Run `tether init` in a project folder.")
        return 0
    current = config.load_config().get("current_project")
    here = store.project_for_path(os.getcwd())
    for r in rows:
        mark = "*" if (here and here["id"] == r["id"]) or (not here and r["slug"] == current) else " "
        extras = f" · {r['open_handoffs']} handoffs" if r["open_handoffs"] else ""
        print(f"{mark} {r['slug']:24} {r['open_tasks']:>3} open{extras} · "
              f"{ago(r['last_active'])} · {r['goal'] or ''}")
    return 0


def cmd_ctx(args) -> int:
    store = _store()
    pid = _pid(store, args)
    # The CLI is a viewer: don't move a surface's "last seen" marker unless it's for
    # a paste-only surface, where this pack *is* that surface's session start.
    text = context_pack(store, pid, args.for_surface, args.budget,
                        mark_seen=args.for_surface in PASTE_SURFACES)
    if args.for_surface in PASTE_SURFACES:
        slug = store.project(pid)["slug"]
        text += (f"\n\n---\nThis is the shared Tether state for this project. Treat it as "
                 f"ground truth and don't re-ask what it already answers. When we finish, "
                 f"end your last reply with a TETHER UPDATE block (header: "
                 f"`TETHER UPDATE {slug}`) listing new decisions, tasks, finished tasks, "
                 f"status and handoffs, so I can sync it back.\n\n" + UPDATE_FORMAT_HELP)
    if args.copy:
        if _copy(text):
            print(f"Copied context for {store.project(pid)['slug']} "
                  f"(~{len(text) // 4} tokens).")
            return 0
        print("(no clipboard tool found; printing instead)", file=sys.stderr)
    print(text)
    return 0


def cmd_tasks(args) -> int:
    store = _store()
    rows = store.tasks(_pid(store, args), open_only=not args.all)
    if args.for_surface and args.for_surface != "any":
        rows = [r for r in rows if r["for_surface"] in (args.for_surface, "any")]
    print("\n".join(task_line(r) for r in rows) or "No tasks.")
    return 0


def cmd_add(args) -> int:
    store = _store()
    row = store.add_task(_pid(store, args), args.title, args.detail, args.for_surface,
                         args.priority, args.surface)
    print(f"t{row['id']} added.")
    return 0


def cmd_set_status(args) -> int:
    store = _store()
    row = store.update_task(parse_task_id(args.task), status=args.status, note=args.note,
                            surface=args.surface)
    print(task_line(row).lstrip("- "))
    return 0


def cmd_decide(args) -> int:
    store = _store()
    row = store.add_decision(_pid(store, args), args.text, args.why, args.surface, args.replaces)
    print(f"d{row['id']} recorded.")
    return 0


def _project_field(args, field: str, value: str | None) -> int:
    store = _store()
    pid = _pid(store, args)
    if value is None:
        print(store.project(pid)[field] or f"(no {field} set)")
        return 0
    fields: dict[str, Any] = {field: value}
    store.update_project(pid, surface=args.surface, **fields)
    print(f"{field} updated.")
    return 0


def cmd_status(args) -> int:
    return _project_field(args, "status", args.text)


def cmd_goal(args) -> int:
    return _project_field(args, "goal", args.text)


def cmd_brief(args) -> int:
    if args.file is None:
        return _project_field(args, "brief", None)
    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
    return _project_field(args, "brief", text)


def cmd_handoff(args) -> int:
    store = _store()
    body = sys.stdin.read() if args.body == "-" else args.body
    row = store.add_handoff(_pid(store, args), args.to, args.title, body, args.link,
                            args.task, args.surface)
    print(f"h{row['id']} created for {args.to}.")
    return 0


def cmd_inbox(args) -> int:
    store = _store()
    if args.what == "import":
        if not args.folder:
            raise TetherError("which folder? tether inbox import <folder>")
        for line in inboxfmt.import_folder(store, args.folder, "chat", move=not args.keep) or [
                "nothing to import"]:
            print(line)
        return 0
    try:
        rows = store.handoffs(_pid(store, args), to_surface=args.for_surface)
    except TetherError:
        rows = []
    for h in rows:
        print(f"h{h['id']} [{h['status']}] {h['from_surface']} → {h['to_surface']}: "
              f"{h['title']} ({ago(h['created_at'])})")
    box = store.inbox()
    for m in box["fresh"]:
        g = store.suggest_project(m)
        print(memory_line(m) + (f"  → {g[0]['slug']}?" if g else ""))
    if box["older"]:
        print(f"({len(box['older'])} older unfiled ideas: tether memories --unfiled)")
    if not rows and not box["fresh"]:
        print("Nothing waiting.")
    return 0


def cmd_remember(args) -> int:
    store = _store()
    body = sys.stdin.read() if args.body == "-" else args.body
    pid = None
    if args.attach or args.project:
        pid = _pid(store, args)
    sf = args.surface
    if args.update:
        row = store.update_memory(parse_memory_id(args.update), title=args.title, append=body,
                                  tags=args.tags or None, kind=args.kind,
                                  summary=args.summary or None, surface=sf)
        print(f"Updated m{row['id']}.")
        return 0
    similar = store.similar_memories(args.title, body, args.tags, pid)
    if similar and similar[0][0] >= 0.6:
        m = similar[0][1]
        store.update_memory(m["id"], append=body, surface=sf)
        print(f"Merged into existing m{m['id']}: {m['title']}")
        return 0
    row = store.add_memory(args.title, body, args.tags, sf, pid, args.kind, args.summary)
    print(f"Saved m{row['id']}" + (" (unfiled)" if not pid else f" in {store.project(pid)['slug']}"))
    for score, m in similar:
        print(f"  similar: m{m['id']} {m['title']} ({score}) — `tether remember --update m{m['id']}`")
    return 0


def cmd_recall(args) -> int:
    print(recall_results(_store().recall(" ".join(args.query))))
    return 0


def cmd_memories(args) -> int:
    store = _store()
    pid = None
    if not args.unfiled and not args.all:
        try:
            pid = _pid(store, args)
        except TetherError:
            pass
    rows = store.memories(pid=pid, unfiled=args.unfiled, limit=args.n)
    print("\n".join(memory_line(m) for m in rows) or "No saved ideas.")
    return 0


def cmd_attach(args) -> int:
    store = _store()
    pid = store.resolve_project(args.to, cwd=os.getcwd())["id"]
    row = store.attach_memory(parse_memory_id(args.memory), pid, args.surface)
    print(f"m{row['id']} attached to {store.project(pid)['slug']}.")
    return 0


def cmd_unremember(args) -> int:
    row = _store().archive_memory(parse_memory_id(args.memory))
    print(f"m{row['id']} archived.")
    return 0


def cmd_promote(args) -> int:
    store = _store()
    row = store.promote_task(parse_task_id(args.task), args.surface)
    print(f"t{row['id']} is now a project task: {row['title']}")
    return 0


def cmd_setup(args) -> int:
    cfg = config.load_config()
    cfg["drive_inbox"] = True
    config.save_config(cfg)
    print(inboxfmt.CHAT_PREFERENCES)
    print("Next:\n  1. Paste the text above into claude.ai > Settings > Profile.\n"
          "  2. Turn on the Google Drive connector in Chat (web and phone).\n"
          "  3. Create a folder called 'Tether Inbox' in Drive.\n"
          "Claude Code now reminds itself to import new files from that folder.")
    return 0


def cmd_config(args) -> int:
    cfg = config.load_config()
    if args.key == "notify":
        if args.value:
            cfg["notify"] = args.value == "on"
            config.save_config(cfg)
        print(f"notify: {'on' if cfg.get('notify', True) else 'off'}")
        return 0
    print(json.dumps({"notify": cfg.get("notify", True), "drive_inbox": bool(cfg.get("drive_inbox")),
                      "mirror_dir": cfg.get("mirror_dir")}, indent=2))
    return 0


def _sync_line(store: Store) -> str:
    st = store.syncer.status()
    peers = ", ".join(f"{p['machine']} ({ago(_iso(p['last_change'])) or 'nothing yet'})"
                      for p in st["peers"]) or "no other Mac yet"
    err = f" · {store.sync_error}" if store.sync_error else ""
    return f"{st['machine']} ↔ {peers} via {st['dir']}{err}"


def _iso(epoch: float | None) -> str | None:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch else None


def cmd_sync(args) -> int:
    cfg = config.load_config()
    if args.action == "setup":
        d = Path(args.dir).expanduser() if args.dir else sync.default_dir()
        d.mkdir(parents=True, exist_ok=True)
        cfg["sync_dir"] = str(d.resolve())
        config.save_config(cfg)
        store = Store()  # picks up the new setting and pulls what's already there
        store.syncer.announce()
        store.syncer.queue_everything()
        pushed = store.flush()
        print(f"Sync on. This Mac is {store.syncer.machine}.")
        print(f"Shared folder: {d}")
        merged = store.pulled_on_open + store.pull()
        print(f"Shared {pushed} records from this Mac; merged {merged} changes from other Macs.")
        print("On your other Mac, install Tether and run the same command "
              f"(`tether sync setup{' --dir ' + str(d) if args.dir else ''}`).")
        return 0
    if args.action == "off":
        cfg.pop("sync_dir", None)
        config.save_config(cfg)
        print("Sync off. Nothing was deleted; `tether sync setup` turns it back on.")
        return 0
    store = Store()
    if not store.syncer:
        raise TetherError("sync is off. Turn it on with: tether sync setup")
    if args.action == "now":
        print(f"Pushed {store.flush()} changes, merged {store.pull()} from other Macs.")
    if args.action == "compact":
        before, after = store.syncer.compact()
        print(f"Compacted this Mac's journal: {before:,} → {after:,} bytes. Other Macs "
              "re-read it automatically.")
        return 0
    st = store.syncer.status()
    print(f"this Mac: {st['machine']} · folder: {st['dir']}")
    print(f"pending: {st['pending']} · journal: {st['journal_bytes']} bytes")
    for p in st["peers"]:
        when = ago(_iso(p["last_change"])) or "no changes yet"
        lag = f" · {p['unread_bytes']} bytes not merged yet" if p["unread_bytes"] else ""
        print(f"peer: {p['machine']} · last change {when}{lag}")
    if not st["peers"]:
        print("peer: none yet. Run `tether sync setup` on your other Mac.")
    if store.sync_error:
        print(f"error: {store.sync_error}")
        return 1
    return 0


def cmd_ui(args) -> int:
    from tether import ui
    if ui.already_running(args.port):
        url = f"http://127.0.0.1:{args.port}"
        print(f"The live view is already running (background service?): {url}")
        if not args.no_open:
            import webbrowser
            webbrowser.open(url)
        return 0
    ui.serve(args.port, open_browser=not args.no_open)
    return 0


def cmd_eval(args) -> int:
    from tether import evalkit
    path = Path(args.scenarios) if args.scenarios else None
    if args.list:
        for scn in evalkit.load_scenarios(path):
            rules = sorted({c.get("rule", c["kind"]) for c in scn["checks"]})
            print(f"{scn['name']:24} {scn.get('surface', 'chat'):6} {', '.join(rules)}")
        return 0
    import anyio
    if args.provider == "nvidia":
        from tether import openai_compat
        args.model = args.model or openai_compat.NVIDIA_MODEL
        make_client: Any = openai_compat.OpenAICompatClient
    else:
        try:
            import anthropic
        except ImportError as e:
            raise TetherError('the eval needs the Anthropic SDK: pip install -e ".[eval]"') from e
        args.model = args.model or evalkit.DEFAULT_MODEL
        make_client = anthropic.AsyncAnthropic

    async def go():
        async with make_client() as client:
            return await evalkit.run_all(client, args.model, args.effort, args.only, args.runs,
                                         path)
    results = anyio.run(go)
    summary = evalkit.summarize(results)
    print(evalkit.report(results, summary))
    saved = evalkit.save(results, summary, args.model, args.effort, Path(args.out))
    print(f"\nSaved {saved}")
    return 0 if summary["overall"] >= 0.8 else 1


def cmd_service(args) -> int:
    from tether import service
    if args.action == "install":
        path = service.install(args.port)
        print(f"Live view now starts at login and stays up: http://127.0.0.1:{args.port}")
        print(f"(launchd agent {path}; log in {config.home() / 'ui.log'})")
    elif args.action == "uninstall":
        print("Removed." if service.uninstall() else "Not installed.")
    else:
        print(f"live view service: {service.status()}")
    return 0


def cmd_watch(args) -> int:
    from tether import ui
    ui.watch(args.project)
    return 0


def cmd_handoff_status(args) -> int:
    store = _store()
    row = store.update_handoff(parse_handoff_id(args.handoff), args.status, args.note,
                               args.surface)
    print(f"h{row['id']} is {row['status']}.")
    return 0


def cmd_note(args) -> int:
    store = _store()
    store.note(_pid(store, args), args.text, args.surface)
    print("Noted.")
    return 0


def cmd_log(args) -> int:
    store = _store()
    for e in store.events(_pid(store, args), limit=args.n):
        print(f"{e['ts'][:16].replace('T', ' ')}  {e['surface']:8} {e['summary']}")
    return 0


def cmd_search(args) -> int:
    store = _store()
    print(search_results(store.search(_pid(store, args), " ".join(args.query))))
    return 0


def cmd_show(args) -> int:
    print(detail(_store(), args.ref))
    return 0


def cmd_apply(args) -> int:
    if args.paste:
        text = _paste()
    elif args.file:
        text = Path(args.file).read_text()
    else:
        text = sys.stdin.read()
    parsed = apply_mod.parse(text)
    store = _store()
    project = args.project or parsed.project
    pid = store.resolve_project(project, cwd=None if project else os.getcwd(),
                                use_current=False)["id"]
    slug = store.project(pid)["slug"]
    surface = args.surface if args.surface != "cli" else "chat"
    for err in parsed.errors:
        print(f"skip {err}", file=sys.stderr)
    if not parsed.ops:
        print("Nothing to apply.")
        return 1 if parsed.errors else 0
    if args.dry_run:
        print(f"Would apply {len(parsed.ops)} changes to {slug} as {surface}:")
        for op in parsed.ops:
            print(f"  {op.line}")
        return 0
    print(f"Applying to {slug} as {surface}:")
    results = apply_mod.apply(store, pid, parsed, surface)
    for line in results:
        print(f"  {line}")
    return 1 if any(r.startswith("err") for r in results) or parsed.errors else 0


def cmd_mirror(args) -> int:
    cfg = config.load_config()
    if args.off:
        cfg.pop("mirror_dir", None)
        config.save_config(cfg)
        print("Mirror off.")
        return 0
    if args.dir:
        cfg["mirror_dir"] = str(Path(args.dir).expanduser().resolve())
        config.save_config(cfg)
    if not cfg.get("mirror_dir"):
        raise TetherError("no mirror folder set. Use: tether mirror --dir <folder>")
    paths = mirror.write_all(_store())
    print(f"Wrote {len(paths)} project file(s) to {cfg['mirror_dir']}. "
          "They update automatically on every change.")
    return 0


def cmd_archive(args) -> int:
    store = _store()
    pid = _pid(store, args)
    row = store.update_project(pid, archived=not args.undo, surface=args.surface)
    print(f"{row['slug']} {'restored' if args.undo else 'archived'}.")
    return 0


def cmd_archived(args) -> int:
    items = _store().archived_items()
    if not items["memories"] and not items["projects"]:
        print("Nothing is archived.")
        return 0
    for p in items["projects"]:
        why = "unused" if p["archived"] == 2 else "by you"
        print(f"project {p['slug']:20} {p['name']} ({why})")
    for m in items["memories"]:
        why = "unused" if m["archived"] == 2 else "by you"
        print(f"m{m['id']:<6} {m['title'][:60]} ({why})")
    print("\nBring one back: tether restore m12   or   tether restore <project>")
    print("Anything marked 'unused' also comes back by itself when you ask for it.")
    return 0


def cmd_restore(args) -> int:
    kind, row = _store().restore_item(args.ref)
    print(f"{'Project ' + row['slug'] if kind == 'projects' else 'm' + str(row['id'])} restored.")
    return 0


def cmd_sweep(args) -> int:
    store = _store()
    found = store.archive_idle(dry_run=args.dry_run)
    verb = "Would archive" if args.dry_run else "Archived"
    print(f"{verb} {len(found['memories'])} memories and {len(found['projects'])} projects "
          "that were unused. Asking for one again brings it back.")
    return 0


def cmd_git(args) -> int:
    repo = args.repo or _git_root(os.getcwd())
    if not repo:
        raise TetherError("not inside a git repository")
    try:
        if args.action == "install":
            print(f"Installed post-commit hook: {gitlink.install(repo)}")
            print('Commit messages like "fixes t12" or "closes t3, t4" now mark those tasks done.')
        else:
            print("Removed." if gitlink.uninstall(repo) else "No Tether hook found.")
    except ValueError as e:
        raise TetherError(str(e)) from e
    return 0


def cmd_serve(args) -> int:
    from tether.server import serve
    serve()
    return 0


def cmd_hook(args) -> int:
    if args.event == "post-commit":
        try:
            gitlink.run_post_commit()
        except Exception:
            pass
        return 0
    if args.event == "post-tool-use":
        return hooks.post_tool_use()
    return _session_start_hook()


def _session_start_hook() -> int:
    """SessionStart hook for Claude Code: inject the context pack for the project bound
    to the session's folder. It must never break a session, so every failure is silent."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    try:
        store = Store()
        row = store.project_for_path(payload.get("cwd") or os.getcwd())
        if not row:
            return 0
        pack = context_pack(store, row["id"], "code")
    except Exception:
        return 0
    note = ("This is the Tether context for this project, loaded at session start. "
            "You don't need to call the tether `context` tool. Follow Tether's rules: "
            "record decisions, keep tasks current, update status at milestones.\n\n")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": note + pack}}))
    return 0
