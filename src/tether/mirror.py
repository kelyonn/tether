"""Markdown mirror: one file per project in a folder you choose (a Google Drive or
iCloud folder works), so Chat and Design can read the state without MCP."""

from __future__ import annotations

from pathlib import Path

from tether import config
from tether.render import ago, mirror_markdown
from tether.store import Store


def mirror_dir() -> Path | None:
    d = config.load_config().get("mirror_dir")
    return Path(d).expanduser() if d else None


def write_project(store: Store, pid: int, directory: Path | None = None) -> Path | None:
    directory = directory or mirror_dir()
    if not directory:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    p = store.project(pid)
    path = directory / f"{p['slug']}.md"
    _atomic_write(path, mirror_markdown(store, pid))
    _write_index(store, directory)
    return path


def write_all(store: Store, directory: Path | None = None) -> list[Path]:
    directory = directory or mirror_dir()
    if not directory:
        return []
    return [path for row in store.projects() if (path := write_project(store, row["id"], directory))]


def attach(store: Store) -> None:
    """Re-render a project's mirror file after every write, if a mirror is configured."""
    def on_change(pid: int) -> None:
        try:
            write_project(store, pid)
        except OSError:
            pass  # A missing Drive folder must never break a tool call.
    store.on_change(on_change)


def _write_index(store: Store, directory: Path) -> None:
    lines = ["# Tether projects", ""]
    for r in store.projects():
        lines.append(f"- [{r['name']}]({r['slug']}.md): {r['goal'] or 'no goal set'} · "
                     f"{r['open_tasks']} open tasks · active {ago(r['last_active'])}")
    _atomic_write(directory / "README.md", "\n".join(lines) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    tmp.replace(path)
