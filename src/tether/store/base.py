"""Shared attributes every store mixin relies on, so each module type-checks alone."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any


class StoreBase:
    path: Path
    db: sqlite3.Connection
    syncer: Any
    sync_error: str | None
    pulled_on_open: int
    _listeners: list[Any]

    if TYPE_CHECKING:
        # Methods live in sibling mixins; let each module call them without a circular import.
        def __getattr__(self, name: str) -> Any: ...
