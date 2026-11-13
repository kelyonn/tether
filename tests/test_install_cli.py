import io
import json
import os

import pytest

from tether import install
from tether.cli import main


def test_desktop_install_merges_and_backs_up():
    path = install.desktop_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    assert install.install_desktop().action == "added"
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and "other" in data["mcpServers"]
    assert data["mcpServers"]["tether"]["env"] == {"TETHER_SURFACE": "desktop"}
    assert list(path.parent.glob("*.tether-backup-*"))
    assert install.install_desktop().action == "unchanged"
    install.uninstall_desktop()
    assert "tether" not in json.loads(path.read_text())["mcpServers"]


def test_corrupt_config_is_never_overwritten():
    path = install.desktop_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(ValueError):
        install.install_desktop()
    assert path.read_text() == "{not json"


def test_code_install_without_cli_and_hook_idempotent(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    assert install.install_code().action == "added"
    entry = json.loads(install.claude_code_config_path().read_text())["mcpServers"]["tether"]
    assert entry["type"] == "stdio" and entry["env"]["TETHER_SURFACE"] == "code"

    settings = install.claude_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
    install.install_hook()
    install.install_hook()
    groups = json.loads(settings.read_text())["hooks"]["SessionStart"]
    assert len(groups) == 2  # the user's hook plus exactly one of ours
    install.uninstall_hook()
    groups = json.loads(settings.read_text())["hooks"]["SessionStart"]
    assert groups == [{"hooks": [{"type": "command", "command": "echo mine"}]}]


def test_cli_flow_and_hook(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    monkeypatch.chdir(proj)
    assert main(["init", "--goal", "Test goal", "--path", str(proj)]) == 0
    assert main(["add", "First task", "--for", "code", "-P", "1"]) == 0
    assert main(["decide", "Use pytest", "--why", "standard"]) == 0
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(proj / "src")})))
    assert main(["hook", "session-start"]) == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Test goal" in ctx and "First task" in ctx and "Use pytest" in ctx

    # A folder with no project produces no output, so unrelated sessions stay clean.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    assert main(["hook", "session-start"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_ctx_for_chat_and_apply(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["init", "demo", "--unbound"])
    capsys.readouterr()
    main(["ctx", "--for", "chat"])
    assert "TETHER UPDATE demo" in capsys.readouterr().out

    block = tmp_path / "update.txt"
    block.write_text("TETHER UPDATE demo\nTASK code: From chat\nEND\n")
    assert main(["apply", str(block)]) == 0
    capsys.readouterr()
    main(["tasks", "-p", "demo"])
    assert "From chat" in capsys.readouterr().out


def test_mirror_updates_on_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init", "mirrored", "--unbound"])
    drive = tmp_path / "drive"
    assert main(["mirror", "--dir", str(drive)]) == 0
    main(["add", "Mirror me"])
    text = (drive / "mirrored.md").read_text()
    assert "Mirror me" in text
    assert "mirrored" in (drive / "README.md").read_text()


def test_git_hook_closes_tasks(tmp_path, monkeypatch):
    import subprocess

    from tether import gitlink
    from tether.store import Store

    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True, text=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    monkeypatch.chdir(repo)
    assert main(["init", "repo", "--path", str(repo)]) == 0
    for title in ("one", "two", "three"):
        main(["add", title])
    assert main(["git", "install", "--repo", str(repo)]) == 0
    monkeypatch.setenv("PATH", os.environ["PATH"])
    git("commit", "--allow-empty", "-q", "-m", "Ship cart, fixes t1 and t2; mentions t3")
    store = Store()
    status = {t["id"]: t["status"] for t in store.tasks(1, open_only=False)}
    assert status == {1: "done", 2: "done", 3: "todo"}
    assert "commit" in store.task(1)["note"]
    assert gitlink.task_ids_in("Closes: t5, t6 & t7") == [5, 6, 7]
    assert gitlink.task_ids_in("see t9 for details") == []
    assert main(["git", "uninstall", "--repo", str(repo)]) == 0
    assert not (repo / ".git/hooks/post-commit").exists()


def test_git_hook_preserves_existing_hook(tmp_path):
    import subprocess

    from tether import gitlink
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    hook = tmp_path / ".git/hooks/post-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n")
    gitlink.install(str(tmp_path))
    gitlink.install(str(tmp_path))  # idempotent
    assert hook.read_text().count(gitlink.BEGIN) == 1 and "echo mine" in hook.read_text()
    gitlink.uninstall(str(tmp_path))
    assert hook.read_text() == "#!/bin/sh\necho mine\n"


def test_status_flags_stale_install(monkeypatch):
    path = install.desktop_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"tether": {"command": "/gone/python", "args": []}}}))
    assert [r.action for r in install.status() if r.client == "Claude Desktop"] == ["stale"]


def test_deep_doctor(capsys):
    assert main(["doctor", "--deep"]) in (0, 1)  # 1 only because nothing is installed here
    out = capsys.readouterr().out
    assert "FAIL" not in out and out.count("ok  ") >= 5


def test_status_sees_desktop_extension(tmp_path, monkeypatch):
    import json

    from tether import install
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("{}")
    (tmp_path / "extensions-installations.json").write_text(
        json.dumps({"extensions": {"local.mcpb.someone.tether-mcp": {}}}))
    monkeypatch.setattr(install, "desktop_config_path", lambda: cfg)
    desk = next(r for r in install.status() if r.client == "Claude Desktop")
    assert desk.action == "installed" and "extension" in desk.detail
