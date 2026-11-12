"""Set the package version everywhere and stub a CHANGELOG section.

    python scripts/bump_version.py 0.5.0
"""
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _sub(path: Path, pattern: str, repl: str) -> None:
    text = path.read_text()
    new, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    if n != 1:
        raise SystemExit(f"could not find version in {path}")
    path.write_text(new)


def bump(version: str, root: Path = ROOT, today: str | None = None) -> None:
    if not SEMVER.match(version):
        raise SystemExit(f"version must look like 1.2.3, got {version!r}")
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text()
    if f"## [{version}]" in text:
        raise SystemExit(f"CHANGELOG already has a section for {version}")
    _sub(root / "pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"')
    _sub(root / "src" / "tether" / "__init__.py", r'^__version__ = "[^"]+"', f'__version__ = "{version}"')
    stub = f"## [{version}] - {today or date.today().isoformat()}\n\n### Added\n\n- \n\n"
    m = re.search(r"^## \[", text, re.M)
    if m:
        text = text[: m.start()] + stub + text[m.start():]
    else:
        text = text.rstrip("\n") + "\n\n" + stub
    changelog.write_text(text)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    bump(sys.argv[1])
    print(f"bumped to {sys.argv[1]}; fill in CHANGELOG.md, then commit and tag v{sys.argv[1]}")
