"""Locations and small persisted settings.

Everything lives under TETHER_HOME (default ~/.tether):
  tether.db     the SQLite store
  config.json   user settings (mirror_dir, current project, ...)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SURFACES = ("code", "desktop", "cowork", "chat", "design", "cli", "any")


def home() -> Path:
    return Path(os.environ.get("TETHER_HOME", Path.home() / ".tether")).expanduser()


def db_path() -> Path:
    return home() / "tether.db"


def config_path() -> Path:
    return home() / "config.json"


def load_config() -> dict[str, Any]:
    try:
        return json.loads(config_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(path)


def current_surface() -> str:
    """The surface this process serves, set per client by `tether install`."""
    s = os.environ.get("TETHER_SURFACE", "").strip().lower()
    return s if s in SURFACES else "any"
