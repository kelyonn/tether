"""Run the live view in the background at login (macOS launchd), so
http://127.0.0.1:7878 is always there. `tether service install|uninstall|status`."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from tether import config
from tether.errors import TetherError

LABEL = "dev.tether.ui"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise TetherError("the background service uses launchd, which is macOS only. "
                          "Run `tether ui` instead.")


def plist(port: int) -> dict:
    env = {k: os.environ[k] for k in ("TETHER_HOME",) if k in os.environ}
    log = str(config.home() / "ui.log")
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "tether", "ui", "--no-open",
                             "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
        **({"EnvironmentVariables": env} if env else {}),
    }


def install(port: int = 7878) -> Path:
    _require_macos()
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config.home().mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", _domain(), str(path)], capture_output=True)
    with open(path, "wb") as f:
        plistlib.dump(plist(port), f)
    r = subprocess.run(["launchctl", "bootstrap", _domain(), str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise TetherError(f"launchctl could not start the service: {r.stderr.strip()}")
    return path


def uninstall() -> bool:
    _require_macos()
    path = plist_path()
    subprocess.run(["launchctl", "bootout", _domain(), str(path)], capture_output=True)
    if path.exists():
        path.unlink()
        return True
    return False


def status() -> str:
    if sys.platform != "darwin":
        return "unsupported (macOS only)"
    if not plist_path().exists():
        return "not installed"
    r = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True,
                       text=True)
    if r.returncode != 0:
        return "installed, not running"
    return "running" if "state = running" in r.stdout else "installed"
