import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bump_version.py"
spec = importlib.util.spec_from_file_location("bump_version", SCRIPT)
bv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bv)


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "src" / "tether").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.4.0"\n')
    (tmp_path / "src" / "tether" / "__init__.py").write_text('"""doc"""\n\n__version__ = "0.4.0"\n')
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\nintro\n\n## [0.4.0] - 2026-11-12\n\n- old\n")
    return tmp_path


def test_bump(tree):
    bv.bump("0.5.0", root=tree, today="2026-12-01")
    assert 'version = "0.5.0"' in (tree / "pyproject.toml").read_text()
    assert '__version__ = "0.5.0"' in (tree / "src" / "tether" / "__init__.py").read_text()
    log = (tree / "CHANGELOG.md").read_text()
    assert log.index("## [0.5.0] - 2026-12-01") < log.index("## [0.4.0]")
    assert log.startswith("# Changelog\n\nintro")


def test_rejects_bad_and_duplicate(tree):
    with pytest.raises(SystemExit):
        bv.bump("v1", root=tree)
    with pytest.raises(SystemExit):
        bv.bump("0.4.0", root=tree)
