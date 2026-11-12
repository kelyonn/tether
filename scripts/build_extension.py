"""Build dist/tether.mcpb, the one-click Claude Desktop extension (a zip with a manifest).

    python scripts/build_extension.py
"""
import re
import sys
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
version = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M).group(1)
manifest = (root / "extension" / "manifest.json").read_text()
manifest = re.sub(r'"version": "[^"]+"', f'"version": "{version}"', manifest, count=1)
launcher = ('import sys\nfrom pathlib import Path\n'
            'sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))\n'
            'from tether.cli import main\n'
            'sys.exit(main(["serve"]))\n')
out = root / "dist" / "tether.mcpb"
out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("manifest.json", manifest)
    z.writestr("server/main.py", launcher)
    z.write(root / "pyproject.toml", "pyproject.toml")
    z.write(root / "README.md", "README.md")
    z.write(root / "LICENSE", "LICENSE")
    for p in sorted((root / "src" / "tether").rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            z.write(p, p.relative_to(root).as_posix())
print(f"built {out} (version {version})")
sys.exit(0)
