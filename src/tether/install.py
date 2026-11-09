"""Register Tether with Claude clients.

Claude Code:    user-scope MCP server (via the `claude` CLI when present, else
                ~/.claude.json) plus a SessionStart hook in ~/.claude/settings.json that
                injects the context pack, so Code never forgets to load it.
Claude Desktop: an entry in claude_desktop_config.json. Cowork runs inside Desktop.

Every file edit is a JSON merge that touches only Tether's own keys, with a
timestamped backup written first.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SERVER_NAME = "tether"
HOOK_MARKER = "-m tether hook"


@dataclass
class Result:
    client: str
    action: str
    detail: str


def server_command() -> list[str]:
    """Run the server with the interpreter Tether is installed in, so the entry works
    no matter what is on the client's PATH."""
    return [sys.executable, "-m", "tether", "serve"]


def desktop_config_path() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "Claude/claude_desktop_config.json"
    return home / ".config/Claude/claude_desktop_config.json"


def claude_code_config_path() -> Path:
    return Path.home() / ".claude.json"


def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    data = json.loads(text)  # Let a corrupt file raise: never overwrite what we can't parse.
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.name}.tether-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    tmp = path.with_name(f".{path.name}.tether-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _entry(surface: str) -> dict[str, Any]:
    cmd = server_command()
    return {"command": cmd[0], "args": cmd[1:], "env": {"TETHER_SURFACE": surface}}


# -- Claude Desktop ------------------------------------------------------------------

def install_desktop(dry_run: bool = False) -> Result:
    path = desktop_config_path()
    data = _read_json(path)
    servers = data.setdefault("mcpServers", {})
    entry = _entry("desktop")
    if servers.get(SERVER_NAME) == entry:
        return Result("Claude Desktop", "unchanged", str(path))
    servers[SERVER_NAME] = entry
    if not dry_run:
        _write_json(path, data)
    return Result("Claude Desktop", "would add" if dry_run else "added",
                  f"{path} (restart Claude Desktop to load it)")


def uninstall_desktop(dry_run: bool = False) -> Result:
    path = desktop_config_path()
    data = _read_json(path)
    if SERVER_NAME not in data.get("mcpServers", {}):
        return Result("Claude Desktop", "not installed", str(path))
    del data["mcpServers"][SERVER_NAME]
    if not dry_run:
        _write_json(path, data)
    return Result("Claude Desktop", "would remove" if dry_run else "removed", str(path))


# -- Claude Code ---------------------------------------------------------------------

def install_code(dry_run: bool = False, use_cli: bool = True) -> Result:
    claude = shutil.which("claude") if use_cli else None
    entry = _entry("code")
    if claude:
        cmd = [claude, "mcp", "add", "--scope", "user", SERVER_NAME,
               "-e", "TETHER_SURFACE=code", "--", *server_command()]
        if dry_run:
            return Result("Claude Code", "would run", shlex.join(cmd))
        subprocess.run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME],
                       capture_output=True)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return Result("Claude Code", "added", "user-scope MCP server via `claude mcp add`")
        # Fall through to the file edit if the CLI refuses.
    path = claude_code_config_path()
    data = _read_json(path)
    servers = data.setdefault("mcpServers", {})
    if servers.get(SERVER_NAME) == {"type": "stdio", **entry}:
        return Result("Claude Code", "unchanged", str(path))
    servers[SERVER_NAME] = {"type": "stdio", **entry}
    if not dry_run:
        _write_json(path, data)
    return Result("Claude Code", "would add" if dry_run else "added",
                  f"{path} (user-scope MCP server)")


def uninstall_code(dry_run: bool = False) -> Result:
    claude = shutil.which("claude")
    if claude and not dry_run:
        subprocess.run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME],
                       capture_output=True)
    path = claude_code_config_path()
    data = _read_json(path)
    if SERVER_NAME in data.get("mcpServers", {}):
        del data["mcpServers"][SERVER_NAME]
        if not dry_run:
            _write_json(path, data)
    return Result("Claude Code", "would remove" if dry_run else "removed", str(path))


def hook_command(event: str = "session-start") -> str:
    return f"{shlex.quote(sys.executable)} -m tether hook {event}"


# event -> (matcher, timeout seconds, what it does)
HOOKS = {
    "SessionStart": ("startup|resume|clear|compact", 10, "session-start",
                     "loads the context pack automatically"),
    "PostToolUse": ("TaskCreate|TaskUpdate|TodoWrite", 5, "post-tool-use",
                    "mirrors Claude Code's own tasks into Tether"),
}


def install_hook(dry_run: bool = False) -> Result:
    path = claude_settings_path()
    data = _read_json(path)
    for event, (matcher, timeout, name, _) in HOOKS.items():
        groups = data.setdefault("hooks", {}).setdefault(event, [])
        groups[:] = [g for g in groups if not _is_our_hook_group(g)]
        groups.append({"matcher": matcher,
                       "hooks": [{"type": "command", "command": hook_command(name),
                                  "timeout": timeout}]})
    if not dry_run:
        _write_json(path, data)
    return Result("Claude Code hooks", "would add" if dry_run else "added",
                  f"SessionStart + PostToolUse in {path}: loads context, syncs Code's tasks")


def uninstall_hook(dry_run: bool = False) -> Result:
    path = claude_settings_path()
    data = _read_json(path)
    hooks = data.get("hooks", {})
    removed = False
    for event in HOOKS:
        groups = hooks.get(event, [])
        kept = [g for g in groups if not _is_our_hook_group(g)]
        if len(kept) != len(groups):
            removed = True
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not removed:
        return Result("Claude Code hooks", "not installed", str(path))
    if not hooks:
        data.pop("hooks", None)
    if not dry_run:
        _write_json(path, data)
    return Result("Claude Code hooks", "would remove" if dry_run else "removed", str(path))


def _is_our_hook_group(group: dict[str, Any]) -> bool:
    return any(HOOK_MARKER in h.get("command", "") for h in group.get("hooks", []))


# -- status --------------------------------------------------------------------------

def _state(entry: dict[str, Any] | None) -> str:
    """installed | missing | stale (the configured interpreter no longer exists, which
    happens when the project folder or its virtualenv is moved)."""
    if not entry:
        return "missing"
    return "installed" if Path(entry.get("command", "")).exists() else "stale"


def status() -> list[Result]:
    out = []
    try:
        desk = _read_json(desktop_config_path()).get("mcpServers", {}).get(SERVER_NAME)
        out.append(Result("Claude Desktop", _state(desk), str(desktop_config_path())))
    except (ValueError, OSError) as e:
        out.append(Result("Claude Desktop", "error", str(e)))
    try:
        code = _read_json(claude_code_config_path()).get("mcpServers", {}).get(SERVER_NAME)
        out.append(Result("Claude Code", _state(code), str(claude_code_config_path())))
    except (ValueError, OSError) as e:
        out.append(Result("Claude Code", "error", str(e)))
    try:
        hooks = _read_json(claude_settings_path()).get("hooks", {})
        have = [e for e in HOOKS if any(_is_our_hook_group(g) for g in hooks.get(e, []))]
        state = "installed" if len(have) == len(HOOKS) else (
            "stale" if have else "missing")
        detail = str(claude_settings_path()) + (
            "" if state != "stale" else " (re-run tether install to add the task-sync hook)")
        out.append(Result("Claude Code hooks", state, detail))
    except (ValueError, OSError) as e:
        out.append(Result("Claude Code hooks", "error", str(e)))
    return out
