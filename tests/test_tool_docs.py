from pathlib import Path

from tether.tooldocs import render


def test_tools_reference_is_up_to_date():
    path = Path(__file__).resolve().parent.parent / "docs" / "TOOLS.md"
    assert path.read_text(encoding="utf-8") == render(), (
        "docs/TOOLS.md is out of date: run `python scripts/gen_tool_docs.py` and commit it")


def test_every_tool_is_documented_with_its_parameters():
    text = render()
    for name in ("context", "remember", "recall", "record_decision", "add_tasks", "create_handoff"):
        assert f"## `{name}`" in text
    assert "| `project` |" in text
