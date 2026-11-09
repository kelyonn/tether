import pytest

from tether.store import Store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own store, config and fake $HOME, so nothing touches the
    real ~/.tether or real Claude config files."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TETHER_HOME", str(tmp_path / "tether"))
    monkeypatch.setenv("TETHER_NOTIFY", "0")
    monkeypatch.delenv("TETHER_SURFACE", raising=False)
    monkeypatch.delenv("TETHER_PROJECT", raising=False)
    monkeypatch.delenv("TETHER_CWD", raising=False)
    return tmp_path


@pytest.fixture
def store():
    s = Store()
    yield s
    s.close()


@pytest.fixture
def project(store, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    return store.create_project("My App", "Ship the MVP", str(root))
