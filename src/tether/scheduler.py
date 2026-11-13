"""Runs `tether github` on a timer inside the live view, so no cron job is needed.

The live view is the long-running part of Tether (it is installed as a login service), so
it owns the schedule: a first sync shortly after it starts, then one every
`github_sync_hours` (default 3, so about eight a day). A failed run is retried sooner. The
"Sync now" button in the page wakes the same thread. Each run uses its own database
connection, so the page stays responsive while it talks to GitHub.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from tether import github
from tether.store import Store


class GithubScheduler:
    def __init__(self, hours: Callable[[], float] = github.sync_hours,
                 sync: Callable[[Store], Any] | None = None,
                 store_factory: Callable[[], Store] = Store,
                 first_delay: float = 30.0, retry_after: float = 900.0):
        self._hours, self._store_factory = hours, store_factory
        self._sync = sync or (lambda store: github.sync(store))
        self.first_delay, self.retry_after = first_delay, retry_after
        self._stop, self._wake = threading.Event(), threading.Event()
        self._busy = threading.Lock()
        self._thread: threading.Thread | None = None
        self.runs = 0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="tether-github", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run_now(self) -> bool:
        """Ask for a sync right away. False if one is already running."""
        if self._busy.locked():
            return False
        self._wake.set()
        return True

    def _once(self) -> bool:
        with self._busy:
            self.runs += 1
            try:
                store = self._store_factory()
            except Exception:
                return False
            try:
                self._sync(store)
                return True
            except Exception:
                return False            # sync() already recorded why, for the page to show
            finally:
                store.close()

    def _loop(self) -> None:
        delay = self.first_delay
        while not self._stop.is_set():
            woke = self._wake.wait(timeout=delay)
            if self._stop.is_set():
                break
            self._wake.clear()
            hours = self._hours()
            if hours <= 0 and not woke:
                delay = 3600.0              # turned off: look at the setting again in an hour
                continue
            ok = self._once()
            every = max(60.0, hours * 3600) if hours > 0 else 3600.0
            delay = every if ok else min(self.retry_after, every)
