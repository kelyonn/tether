"""Write docs/TOOLS.md from the server's tool definitions."""

from pathlib import Path

from tether.tooldocs import render

path = Path(__file__).resolve().parent.parent / "docs" / "TOOLS.md"
path.write_text(render(), encoding="utf-8")
print(f"wrote {path}")
