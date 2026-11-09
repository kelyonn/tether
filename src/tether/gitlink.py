"""Close tasks from commit messages: `git commit -m "Add cart, fixes t12"` marks t12 done.

Installed as a post-commit hook. It must never block or fail a commit, so the hook
script ends in `|| true` and every error in the handler is swallowed.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

BEGIN = "# >>> tether post-commit >>>"
END = "# <<< tether post-commit <<<"

CLOSER_RE = re.compile(
    r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?|done|complete[sd]?)\s*:?\s+"
    r"((?:t#?\d+)(?:\s*(?:,|and|&)\s*t#?\d+)*)", re.IGNORECASE)
REF_RE = re.compile(r"t#?(\d+)", re.IGNORECASE)


def task_ids_in(message: str) -> list[int]:
    ids: list[int] = []
    for m in CLOSER_RE.finditer(message):
        for n in REF_RE.findall(m.group(1)):
            if int(n) not in ids:
                ids.append(int(n))
    return ids


def hooks_dir(repo: str) -> Path:
    out = subprocess.run(["git", "-C", repo, "rev-parse", "--git-path", "hooks"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise ValueError(f"{repo} is not a git repository")
    path = Path(out.stdout.strip())
    return path if path.is_absolute() else Path(repo) / path


def _block() -> str:
    cmd = f"{shlex.quote(sys.executable)} -m tether hook post-commit"
    return f"{BEGIN}\n{cmd} >/dev/null 2>&1 || true\n{END}\n"


def install(repo: str) -> str:
    hook = hooks_dir(repo) / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    text = hook.read_text() if hook.exists() else "#!/bin/sh\n"
    if BEGIN in text:
        text = _strip(text)
    if not text.endswith("\n"):
        text += "\n"
    hook.write_text(text + _block())
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(hook)


def uninstall(repo: str) -> bool:
    hook = hooks_dir(repo) / "post-commit"
    if not hook.exists() or BEGIN not in hook.read_text():
        return False
    rest = _strip(hook.read_text())
    if rest.strip() in ("", "#!/bin/sh"):
        hook.unlink()
    else:
        hook.write_text(rest)
    return True


def _strip(text: str) -> str:
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        if line.strip() == BEGIN:
            skipping = True
        elif line.strip() == END:
            skipping = False
        elif not skipping:
            out.append(line)
    return "".join(out)


def run_post_commit(cwd: str | None = None) -> list[str]:
    """Called by the hook. Returns the refs it closed (for tests and `--verbose`)."""
    from tether.store import Store, TetherError

    cwd = cwd or os.getcwd()
    log = subprocess.run(["git", "-C", cwd, "log", "-1", "--format=%h%x00%B"],
                         capture_output=True, text=True)
    if log.returncode != 0 or "\0" not in log.stdout:
        return []
    sha, _, message = log.stdout.partition("\0")
    ids = task_ids_in(message)
    if not ids:
        return []
    store = Store()
    row = store.project_for_path(cwd)
    if not row:
        return []
    closed = []
    for tid in ids:
        try:
            task = store.task(tid)
            if task["project_id"] != row["id"] or task["status"] in ("done", "dropped"):
                continue
            store.update_task(tid, status="done", note=f"commit {sha.strip()}", surface="code")
            closed.append(f"t{tid}")
        except TetherError:
            continue
    return closed
