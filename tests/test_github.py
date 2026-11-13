import json
import subprocess

import pytest

from tether import github
from tether.cli import main
from tether.github import Drift, Local, Remote
from tether.store import TetherError


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    p = tmp_path / "shop"
    p.mkdir()
    git(p, "init", "-q", "-b", "main")
    git(p, "config", "user.email", "t@example.com")
    git(p, "config", "user.name", "T")
    git(p, "remote", "add", "origin", "https://github.com/kelyonn/shop.git")
    (p / "a.txt").write_text("a")
    git(p, "add", ".")
    git(p, "commit", "-q", "-m", "first")
    return p


def head(path):
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def repo_node(oid="a" * 40, *, private=False, perm="ADMIN", branch=True, issues=(), prs=()):
    commit = {"oid": oid, "committedDate": "2026-11-09T10:00:00Z", "messageHeadline": "Rewrite the README"}
    return {"nameWithOwner": "kelyonn/shop", "isPrivate": private, "isArchived": False,
            "pushedAt": "2026-11-09T10:00:00Z", "viewerPermission": perm, "description": "d",
            "defaultBranchRef": {"name": "main", "target": commit},
            "ref": {"target": commit} if branch else None,
            "issues": {"totalCount": len(issues), "nodes": list(issues)},
            "pullRequests": {"totalCount": len(prs), "nodes": list(prs)}}


def fake_gh(node, compare=None, viewer="kelyonn"):
    def gh(args):
        if args[:2] == ["api", "graphql"]:
            return 0, json.dumps({"data": {"viewer": {"login": viewer}, "r0": node}})
        if args[0] == "api" and "/compare/" in args[1]:
            return (0, json.dumps(compare)) if compare is not None else (1, "")
        return 1, ""
    return gh


def issue(n, title, author="kelyonn"):
    return {"number": n, "title": title, "url": f"https://github.com/kelyonn/shop/issues/{n}",
            "author": {"login": author}}


def bound(store, repo):
    return store.create_project("Shop", "sell mugs", str(repo))


# -- reading the local clone -----------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/kelyonn/tether.git", ("kelyonn", "tether")),
    ("git@github.com:kelyonn/tether.git", ("kelyonn", "tether")),
    ("https://github.com/kelyonn/tether", ("kelyonn", "tether")),
    ("https://gitlab.com/x/y.git", None), ("", None)])
def test_parse_remote(url, expected):
    assert github.parse_remote(url) == expected


def test_local_info_reads_the_clone(repo):
    (repo / "b.txt").write_text("b")
    info = github.local_info("shop", str(repo))
    assert (info.owner, info.repo, info.branch, info.uncommitted) == ("kelyonn", "shop", "main", 1)
    assert info.head == head(repo)


def test_local_info_ignores_non_repos_and_other_hosts(tmp_path, repo):
    assert github.local_info("x", str(tmp_path / "missing")) is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert github.local_info("x", str(plain)) is None
    git(repo, "remote", "set-url", "origin", "https://gitlab.com/a/b.git")
    assert github.local_info("x", str(repo)) is None


# -- drift ----------------------------------------------------------------------------

def local(repo):
    return github.local_info("shop", str(repo))


def remote(node):
    return github._remote(node)


def test_drift_states(repo):
    loc = local(repo)
    assert github.drift(fake_gh(None), loc, remote(repo_node(loc.head))) == Drift("in_sync")
    assert github.drift(fake_gh(None), loc, remote(repo_node(branch=False))).state == "branch_missing"
    behind = github.drift(fake_gh(None, [0, 4]), loc, remote(repo_node("b" * 40)))
    assert (behind.state, behind.ahead, behind.behind) == ("behind", 0, 4)
    ahead = github.drift(fake_gh(None, [2, 0]), loc, remote(repo_node("b" * 40)))
    assert (ahead.state, ahead.ahead) == ("ahead", 2)
    assert github.drift(fake_gh(None, [1, 1]), loc, remote(repo_node("b" * 40))).state == "diverged"


def test_commits_github_has_never_seen_count_as_unpushed(repo):
    first = head(repo)
    (repo / "c.txt").write_text("c")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "second")
    (repo / "d.txt").write_text("d")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "third")
    d = github.drift(fake_gh(None, compare=None), local(repo), remote(repo_node(first)))
    assert (d.state, d.ahead) == ("ahead", 2)


# -- talking to gh ----------------------------------------------------------------------

def test_fetch_reads_repos_and_the_viewer(repo):
    viewer, out = github.fetch(fake_gh(repo_node(), viewer="kelyonn"), [local(repo)])
    assert viewer == "kelyonn" and out["shop"].full == "kelyonn/shop" and not out["shop"].private


def test_fetch_marks_unreachable_repos(repo):
    gh = lambda a: (0, json.dumps({"data": {"viewer": {"login": "k"}, "r0": None}}))
    _, out = github.fetch(gh, [local(repo)])
    assert out["shop"] is None


def test_fetch_explains_a_failed_login(repo):
    with pytest.raises(TetherError, match="gh auth login"):
        github.fetch(lambda a: (1, ""), [local(repo)])


def test_missing_gh_cli_is_explained(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda name: None)
    monkeypatch.setattr(github, "GH_LOCATIONS", ())
    with pytest.raises(TetherError, match="was not found"):
        github.run_gh(["api", "user"])


# -- updating Tether --------------------------------------------------------------------

def sync_once(store, repo, node, compare=None, **kw):
    return github.sync(store, "shop", gh=fake_gh(node, compare), **kw)


def test_status_and_brief_come_from_github(store, repo):
    p = bound(store, repo)
    sync_once(store, repo, repo_node(head(repo)))
    row = store.project(p["id"])
    assert 'GitHub main: 2026-11-09 "Rewrite the README"' in row["status"] and "clone matches GitHub" in row["status"]
    assert row["brief"].startswith("GitHub: kelyonn/shop (public; your access: admin)")


def test_status_reports_drift_and_uncommitted_files(store, repo):
    p = bound(store, repo)
    (repo / "wip.txt").write_text("x")
    sync_once(store, repo, repo_node("b" * 40), compare=[0, 2])
    st = store.project(p["id"])["status"]
    assert "clone is 2 behind GitHub" in st and "1 uncommitted file(s)" in st


def test_a_brief_you_wrote_is_never_overwritten(store, repo):
    p = bound(store, repo)
    store.update_project(p["id"], brief="My own notes about the shop")
    sync_once(store, repo, repo_node(head(repo)))
    assert store.project(p["id"])["brief"] == "My own notes about the shop"


def titles(store, p):
    return sorted(t["title"] for t in store.tasks(p["id"]))


def test_unpushed_work_and_missing_branches_become_tasks(store, repo):
    p = bound(store, repo)
    sync_once(store, repo, repo_node(branch=False))
    assert titles(store, p) == ["Push branch main to GitHub"]
    sync_once(store, repo, repo_node("b" * 40), compare=[3, 0])
    assert "Push 3 unpushed commit(s) to GitHub" in titles(store, p)


def test_small_gaps_are_not_tasks_but_big_ones_are(store, repo):
    p = bound(store, repo)
    sync_once(store, repo, repo_node("b" * 40), compare=[0, 2])
    assert titles(store, p) == []
    sync_once(store, repo, repo_node("b" * 40), compare=[0, 14])
    assert titles(store, p) == ["Pull 14 new commit(s) from GitHub"]


def test_issues_and_prs_in_your_own_repo_are_tasks(store, repo):
    p = bound(store, repo)
    node = repo_node(head(repo), issues=[issue(3, "Cart bug", "someone")],
                     prs=[{"number": 9, "title": "Add tests", "url": "u", "isDraft": True,
                           "author": {"login": "kelyonn"}}])
    sync_once(store, repo, node)
    assert titles(store, p) == ["#3: Cart bug", "PR #9: Add tests (draft)"]


def test_in_someone_elses_repo_only_your_own_items_count(store, repo):
    p = bound(store, repo)
    node = repo_node(head(repo), perm="READ", issues=[issue(3, "Theirs", "other"), issue(4, "Mine", "kelyonn")])
    sync_once(store, repo, node)
    assert titles(store, p) == ["#4: Mine"]


def test_running_twice_changes_nothing_and_resolved_things_close(store, repo):
    p = bound(store, repo)
    node = repo_node(head(repo), issues=[issue(3, "Cart bug")])
    sync_once(store, repo, node)
    again = sync_once(store, repo, node)
    assert again[0].lines == []                                          # nothing changed
    assert len(store.tasks(p["id"])) == 1
    sync_once(store, repo, repo_node(head(repo)))                          # issue closed on GitHub
    assert store.tasks(p["id"]) == []
    done = list(store.db.execute("SELECT * FROM tasks WHERE project_id=?", (p["id"],)))
    assert done[0]["status"] == "done" and done[0]["note"] == "resolved on GitHub"
    sync_once(store, repo, node)                                           # reopened on GitHub
    assert titles(store, p) == ["#3: Cart bug"]


def test_a_task_you_dropped_stays_dropped(store, repo):
    p = bound(store, repo)
    node = repo_node(head(repo), issues=[issue(3, "Cart bug")])
    sync_once(store, repo, node)
    store.update_task(store.tasks(p["id"])[0]["id"], status="dropped")
    sync_once(store, repo, node)
    assert store.tasks(p["id"]) == []


def test_dry_run_changes_nothing(store, repo):
    p = bound(store, repo)
    reports = sync_once(store, repo, repo_node(branch=False), dry_run=True)
    assert any("updated" in line for line in reports[0].lines)
    assert store.project(p["id"])["status"] == "" and store.tasks(p["id"]) == []


def test_unreachable_repo_is_reported_not_fatal(store, repo):
    bound(store, repo)
    gh = lambda a: (0, json.dumps({"data": {"viewer": {"login": "k"}, "r0": None}}))
    reports = github.sync(store, "shop", gh=gh)
    assert "not reachable" in reports[0].lines[0]


def test_projects_without_a_github_remote_are_skipped(store, tmp_path):
    store.create_project("Plain", "g", str(tmp_path))
    with pytest.raises(TetherError, match="no bound project"):
        github.sync(store, gh=fake_gh(None))


def test_the_cli_command(store, repo, capsys, monkeypatch):
    bound(store, repo)
    monkeypatch.setattr(github, "run_gh", fake_gh(repo_node(head(repo))))
    assert main(["github", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "shop" in out and "Dry run" in out


# -- store helpers ----------------------------------------------------------------------

def test_external_tasks_are_keyed_and_reopen(store):
    p = store.create_project("Shop", "g")
    t, how = store.sync_external_task(p["id"], "gh:issue:1", "Fix it", "d")
    assert how == "added" and t["ext_key"] == "gh:issue:1"
    assert store.sync_external_task(p["id"], "gh:issue:1", "Fix it", "d")[1] == "unchanged"
    assert store.sync_external_task(p["id"], "gh:issue:1", "Fix it now", "d")[1] == "updated"
    assert store.close_external_tasks(p["id"], "gh:", set(), "gone") == 1
    assert store.sync_external_task(p["id"], "gh:issue:1", "Fix it now", "d")[1] == "reopened"
    assert store.close_external_tasks(p["id"], "gh:", {"gh:issue:1"}, "gone") == 0


def test_closing_only_touches_the_matching_prefix(store):
    p = store.create_project("Shop", "g")
    store.add_task(p["id"], "my own task")
    store.sync_external_task(p["id"], "gh:issue:1", "Fix it")
    store.close_external_tasks(p["id"], "gh:", set(), "gone")
    assert [t["title"] for t in store.tasks(p["id"])] == ["my own task"]


def test_local_dataclass_defaults():
    assert Local("a", "/p", "o", "r", "main", "h", 0).uncommitted == 0
    assert Remote.__dataclass_fields__["issues"].default_factory is list


# -- finding gh when the PATH is bare (a login service) ----------------------------------

def fake_exe(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


def test_gh_is_found_in_the_usual_places_even_when_path_is_bare(tmp_path, monkeypatch):
    exe = fake_exe(tmp_path / "gh")
    monkeypatch.setattr(github.shutil, "which", lambda name: None)         # launchd: nothing on PATH
    monkeypatch.setattr(github, "GH_LOCATIONS", ("/nowhere/gh", exe))
    assert github.find_gh() == exe


def test_a_configured_path_wins_over_everything(tmp_path, monkeypatch):
    from tether import config
    mine = fake_exe(tmp_path / "my-gh")
    other = fake_exe(tmp_path / "other-gh")
    config.save_config({"github_gh_path": mine})
    monkeypatch.setattr(github.shutil, "which", lambda name: other)
    assert github.find_gh() == mine


def test_path_is_used_when_there_is_no_config(tmp_path, monkeypatch):
    exe = fake_exe(tmp_path / "gh")
    monkeypatch.setattr(github.shutil, "which", lambda name: exe)
    monkeypatch.setattr(github, "GH_LOCATIONS", ())
    assert github.find_gh() == exe


def test_a_missing_gh_says_where_to_get_it(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda name: None)
    monkeypatch.setattr(github, "GH_LOCATIONS", ("/nowhere/gh",))
    with pytest.raises(TetherError, match=r"cli\.github\.com"):
        github.find_gh()


def test_run_gh_uses_the_resolved_path(tmp_path, monkeypatch):
    exe = fake_exe(tmp_path / "gh")
    monkeypatch.setattr(github, "find_gh", lambda: exe)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert github.run_gh(["api", "user"]) == (0, "ok")
    assert seen["cmd"] == [exe, "api", "user"]
