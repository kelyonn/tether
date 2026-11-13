"""The SQLite store: projects, decisions, tasks, handoffs, memories and an event log.

One file is shared by every process (the Code server, the Desktop server, the CLI,
hooks), so it runs in WAL mode with a busy timeout and every write is one short
transaction. The class is assembled from focused mixins, one module each.
"""

from __future__ import annotations

from tether.errors import TetherError  # noqa: F401
from tether.store.archive import ArchiveMixin
from tether.store.core import CoreMixin
from tether.store.memories import MemoriesMixin
from tether.store.projects import ProjectsMixin
from tether.store.records import RecordsMixin
from tether.store.schema import *
from tether.store.search import SearchMixin


class Store(CoreMixin, ProjectsMixin, RecordsMixin, MemoriesMixin, ArchiveMixin, SearchMixin):
    """Everything Tether remembers. See the mixins for the methods."""
