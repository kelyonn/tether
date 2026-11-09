"""macOS notifications through osascript. Off with `tether config notify off`
or TETHER_NOTIFY=0. Never raises."""

from __future__ import annotations

import os
import subprocess
import sys

from tether import config


def enabled() -> bool:
    if os.environ.get("TETHER_NOTIFY") == "0" or sys.platform != "darwin":
        return False
    return bool(config.load_config().get("notify", True))


def notify(title: str, message: str) -> None:
    if not enabled():
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')[:200]  # noqa: E731
    script = f'display notification "{esc(message)}" with title "{esc(title)}"'
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass
