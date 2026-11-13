"""Keep Tether's picture of each project in step with GitHub.

`tether github sync` reads the real state from GitHub through the `gh` CLI (which you
have already logged in to): the latest commit, open issues and pull requests, and whether
your local clone is ahead of, behind or missing from GitHub. It then updates each bound
project's status and brief and keeps a few tasks current ("pull 14 commits", "push this
branch", open issues and PRs). Nothing is ever written to GitHub or to your repositories.

Tasks it manages have an `ext_key` starting with `gh:`, so re-running is safe: new things
are added, ones that no longer apply are closed, and tasks you dropped stay dropped.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tether.errors import TetherError
from tether.store import Store

# gh runner: (args) -> (exit code, stdout). Swapped out in tests.
Gh = Callable[[list[str]], tuple[int, str]]
REMOTE_RE = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")
WRITE_ACCESS = {"WRITE", "MAINTAIN", "ADMIN"}
CHUNK = 20
PULL_TASK_AT = 3            # being 1-2 commits behind is noted in the status, not made a task
BRIEF_PREFIX = "GitHub:"


@dataclass
class Local:
    slug: str
    path: str
    owner: str
    repo: str
    branch: str
    head: str
    uncommitted: int


@dataclass
class Remote:
    full: str
    private: bool
    archived: bool
    permission: str
    pushed_at: str
    default_branch: str
    description: str
    branch_exists: bool
    commit_oid: str            # head of the local branch's counterpart (or the default branch)
    commit_date: str
    commit_headline: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    prs: list[dict[str, Any]] = field(default_factory=list)
    issue_total: int = 0
    pr_total: int = 0


@dataclass
class Drift:
    state: str                 # in_sync | behind | ahead | diverged | branch_missing
    ahead: int = 0             # commits here that GitHub does not have
    behind: int = 0            # commits on GitHub that are not here


@dataclass
class Report:
    slug: str
    lines: list[str] = field(default_factory=list)


def run_gh(args: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise TetherError("the GitHub CLI (`gh`) is not installed. Install it from "
                          "https://cli.github.com and run `gh auth login`.") from e
    except subprocess.TimeoutExpired as e:
        raise TetherError("GitHub did not answer in time. Try again in a moment.") from e
    return p.returncode, p.stdout


def _git(path: str, *args: str) -> str:
    try:
        p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def parse_remote(url: str) -> tuple[str, str] | None:
    m = REMOTE_RE.search(url.strip())
    return (m.group(1), m.group(2)) if m else None


def local_info(slug: str, path: str) -> Local | None:
    """What the clone at `path` looks like; None if it is not a git repo with a GitHub remote."""
    if not Path(path).is_dir() or not (Path(path) / ".git").exists():
        return None
    remote = parse_remote(_git(path, "remote", "get-url", "origin"))
    if not remote:
        return None
    return Local(slug, path, remote[0], remote[1], _git(path, "branch", "--show-current"),
                 _git(path, "rev-parse", "HEAD"),
                 len(_git(path, "status", "--short").splitlines()))


_FRAG = """nameWithOwner isPrivate isArchived pushedAt viewerPermission description
 defaultBranchRef{name target{... on Commit{oid committedDate messageHeadline}}}
 ref(qualifiedName:"refs/heads/%s"){target{... on Commit{oid committedDate messageHeadline}}}
 issues(states:OPEN,first:50,orderBy:{field:UPDATED_AT,direction:DESC}){totalCount
   nodes{number title url author{login} labels(first:3){nodes{name}}}}
 pullRequests(states:OPEN,first:30){totalCount nodes{number title url isDraft author{login}}}"""


def fetch(gh: Gh, locals_: list[Local]) -> tuple[str, dict[str, Remote | None]]:
    """One GraphQL round trip per CHUNK repos. Returns (your GitHub login, slug -> Remote)."""
    viewer = ""
    out: dict[str, Remote | None] = {}
    for start in range(0, len(locals_), CHUNK):
        batch = locals_[start:start + CHUNK]
        body = "".join(
            f'r{i}:repository(owner:{json.dumps(it.owner)},name:{json.dumps(it.repo)})'
            f'{{{_FRAG % (it.branch or "main")}}}\n' for i, it in enumerate(batch))
        code, text = gh(["api", "graphql", "-f", f"query=query{{viewer{{login}}\n{body}}}"])
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        if code != 0 and not data.get("data"):
            raise TetherError("could not read from GitHub. Check `gh auth status`; if you are "
                              "logged out, run `gh auth login`.")
        d = data.get("data") or {}
        viewer = (d.get("viewer") or {}).get("login", viewer)
        for i, it in enumerate(batch):
            out[it.slug] = _remote(d.get(f"r{i}"))
    return viewer, out


def _remote(g: dict[str, Any] | None) -> Remote | None:
    if not g:
        return None
    default = g.get("defaultBranchRef") or {}
    ref = g.get("ref")
    c = ((ref or {}).get("target")) or (default.get("target")) or {}
    return Remote(
        full=g["nameWithOwner"], private=bool(g["isPrivate"]), archived=bool(g["isArchived"]),
        permission=g.get("viewerPermission") or "READ", pushed_at=(g.get("pushedAt") or "")[:10],
        default_branch=default.get("name") or "", description=g.get("description") or "",
        branch_exists=bool(ref), commit_oid=c.get("oid", ""),
        commit_date=(c.get("committedDate") or "")[:10], commit_headline=c.get("messageHeadline", ""),
        issues=(g.get("issues") or {}).get("nodes") or [], prs=(g.get("pullRequests") or {}).get("nodes") or [],
        issue_total=(g.get("issues") or {}).get("totalCount", 0),
        pr_total=(g.get("pullRequests") or {}).get("totalCount", 0))


def drift(gh: Gh, local: Local, remote: Remote) -> Drift:
    if not remote.branch_exists:
        return Drift("branch_missing")
    if remote.commit_oid == local.head:
        return Drift("in_sync")
    code, text = gh(["api", f"repos/{remote.full}/compare/{remote.commit_oid}...{local.head}",
                     "--jq", "[.ahead_by,.behind_by]|@json"])
    if code == 0:
        try:
            ahead, behind = json.loads(text)
        except ValueError:
            return Drift("diverged")
        return Drift("diverged" if ahead and behind else "ahead" if ahead else "behind",
                     int(ahead), int(behind))
    # GitHub has never seen our head commit, so these are commits that were never pushed.
    n = _git(local.path, "rev-list", "--count", f"{remote.commit_oid}..HEAD")
    return Drift("ahead", int(n) if n.isdigit() else 1, 0)


def status_line(local: Local, remote: Remote, dr: Drift) -> str:
    head = (f'GitHub {local.branch}: {remote.commit_date} "{remote.commit_headline[:90]}"'
            if remote.branch_exists else
            f'GitHub {remote.default_branch}: {remote.commit_date} "{remote.commit_headline[:90]}"')
    here = []
    if dr.state == "branch_missing":
        here.append(f"branch {local.branch} is not on GitHub")
    elif dr.state == "in_sync":
        here.append("clone matches GitHub")
    else:
        if dr.behind:
            here.append(f"clone is {dr.behind} behind GitHub")
        if dr.ahead:
            here.append(f"{dr.ahead} commit(s) not pushed")
    if local.uncommitted:
        here.append(f"{local.uncommitted} uncommitted file(s)")
    return (head + " · " + ", ".join(here))[:400]


def brief_line(remote: Remote) -> str:
    vis = "private" if remote.private else "public"
    extra = ", archived" if remote.archived else ""
    return (f"{BRIEF_PREFIX} {remote.full} ({vis}{extra}; your access: {remote.permission.lower()}). "
            f"Default branch {remote.default_branch}, last push {remote.pushed_at}. "
            f"{remote.issue_total} open issue(s), {remote.pr_total} open PR(s).")


def apply(store: Store, local: Local, remote: Remote | None, dr: Drift | None, viewer: str,
          dry_run: bool = False) -> Report:
    rep = Report(local.slug)
    project = store.find_project(local.slug)
    if project is None:
        rep.lines.append("not in Tether")
        return rep
    if remote is None or dr is None:
        rep.lines.append(f"{local.owner}/{local.repo}: not reachable on GitHub (private, renamed or deleted?)")
        return rep
    pid = project["id"]
    status = status_line(local, remote, dr)
    brief = brief_line(remote)
    changes: dict[str, Any] = {}
    if status != project["status"]:
        changes["status"] = status
    if brief != project["brief"] and (not project["brief"] or project["brief"].startswith(BRIEF_PREFIX)):
        changes["brief"] = brief
    if changes:
        rep.lines.append("updated " + " and ".join(changes))
        if not dry_run:
            store.update_project(pid, surface="github", **changes)
    mine = remote.permission in WRITE_ACCESS
    want: dict[str, tuple[str, str, int]] = {}
    if dr.state == "branch_missing":
        want["gh:branch"] = (f"Push branch {local.branch} to GitHub",
                             f"{local.branch} exists only on this Mac, so GitHub has no copy of it.", 1)
    if dr.ahead:
        want["gh:ahead"] = (f"Push {dr.ahead} unpushed commit(s) to GitHub",
                            f"{remote.full} does not have {dr.ahead} commit(s) from {local.branch}.", 1)
    if dr.behind >= PULL_TASK_AT and mine:
        extra = (f" You also have {local.uncommitted} uncommitted file(s); commit or stash them first."
                 if local.uncommitted else "")
        want["gh:behind"] = (f"Pull {dr.behind} new commit(s) from GitHub",
                             f"{remote.full} is {dr.behind} commit(s) ahead of this clone.{extra}", 2)
    for kind, nodes, label in (("issue", remote.issues, ""), ("pr", remote.prs, "PR ")):
        for n in nodes:
            if not mine and (n.get("author") or {}).get("login") != viewer:
                continue                       # someone else's repo: only what you opened
            title = f"{label}#{n['number']}: {n['title']}" if label else f"#{n['number']}: {n['title']}"
            if n.get("isDraft"):
                title += " (draft)"
            want[f"gh:{kind}:{n['number']}"] = (title, n["url"], 2)
    for key, (title, detail, prio) in want.items():
        if dry_run:
            rep.lines.append(f"task: {title}")
            continue
        try:
            _, how = store.sync_external_task(pid, key, title, detail, prio, "any")
        except TetherError:
            rep.lines.append(f"skipped {key} (its text looks like a secret)")
            continue
        if how != "unchanged":
            rep.lines.append(f"{how} task: {title}")
    if not dry_run:
        closed = store.close_external_tasks(pid, "gh:", set(want), "resolved on GitHub")
        if closed:
            rep.lines.append(f"closed {closed} task(s) that are resolved")
    return rep


def sync(store: Store, only: str | None = None, dry_run: bool = False,
         gh: Gh | None = None) -> list[Report]:
    """Reconcile every bound project with GitHub. Returns one Report per project."""
    gh = gh or run_gh
    locals_: list[Local] = []
    for p in store.projects():
        if only and p["slug"] != only:
            continue
        if p["root_path"]:
            info = local_info(p["slug"], p["root_path"])
            if info:
                locals_.append(info)
    if not locals_:
        raise TetherError("no bound project with a GitHub remote" + (f" named {only!r}" if only else "") + ".")
    viewer, remotes = fetch(gh, locals_)
    reports = []
    for loc in locals_:
        rem = remotes.get(loc.slug)
        dr = drift(gh, loc, rem) if rem else None
        reports.append(apply(store, loc, rem, dr, viewer, dry_run))
    return reports
