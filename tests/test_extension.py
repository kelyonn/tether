import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_extension_bundle(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_extension.py")], check=True,
                   capture_output=True)
    with zipfile.ZipFile(ROOT / "dist" / "tether.mcpb") as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json"))
    assert {"manifest.json", "server/main.py", "pyproject.toml", "src/tether/server.py"} <= names
    assert manifest["icon"] in names                      # the Desktop extension has an icon
    assert manifest["server"]["entry_point"] in names
    from tether import __version__
    assert manifest["version"] == __version__
