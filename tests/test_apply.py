from tether import apply

BLOCK = """
Sure, here's the summary of what we decided.

```
TETHER UPDATE my-app
DECISION: Use Stripe Checkout | why: less PCI scope
TASK code P1: Wire checkout webhook :: verify signature
TASK design: Billing empty state
DONE t{tid}: shipped
STATUS: Billing designed
GOAL: Paid plan by December
HANDOFF code: Billing spec :: Final layout and copy :: https://a.example, https://b.example
NOTE: Users want annual pricing
FROBNICATE: nonsense
END
```
Anything else after END is ignored.
DECISION: not applied
"""


def test_parse_and_apply_block(store, project):
    pid = project["id"]
    t = store.add_task(pid, "old task")
    parsed = apply.parse(BLOCK.format(tid=t["id"]))
    assert parsed.project == "my-app"
    assert len(parsed.ops) == 8
    assert len(parsed.errors) == 1 and "FROBNICATE" in parsed.errors[0]

    results = apply.apply(store, pid, parsed, "chat")
    assert all(r.startswith("ok") for r in results), results

    p = store.project(pid)
    assert p["status"] == "Billing designed" and p["goal"] == "Paid plan by December"
    assert store.task(t["id"])["status"] == "done"
    open_titles = {x["title"]: x for x in store.tasks(pid)}
    assert open_titles["Wire checkout webhook"]["priority"] == 1
    assert open_titles["Wire checkout webhook"]["detail"] == "verify signature"
    assert open_titles["Billing empty state"]["for_surface"] == "design"
    d = store.decisions(pid)[0]
    assert (d["text"], d["why"], d["surface"]) == ("Use Stripe Checkout", "less PCI scope", "chat")
    h = store.handoffs(pid)[0]
    assert h["to_surface"] == "code" and "b.example" in h["links"]


def test_failed_op_does_not_stop_others(store, project):
    parsed = apply.parse("DONE t999\nTASK: still added")
    results = apply.apply(store, project["id"], parsed, "chat")
    assert results[0].startswith("err") and results[1].startswith("ok")


def test_replaces_and_headerless(store, project):
    pid = project["id"]
    d = store.add_decision(pid, "Old")
    parsed = apply.parse(f"- DECISION replaces d{d['id']}: New one")
    assert parsed.project is None
    apply.apply(store, pid, parsed, "design")
    assert [x["text"] for x in store.decisions(pid)] == ["New one"]


def test_inbox_parses_drive_escaped_front_matter():
    from tether import inboxfmt
    text = ("\\---  \ntitle: Streakly  \nkind: idea  \ntags: habit, local\\-first  \n"
            "summary: One tap a day.  \n\\---  \nBody line.  \n")
    item = inboxfmt.parse(text, "streakly.md")
    assert item["title"] == "Streakly" and item["kind"] == "idea"
    assert item["tags"] == "habit, local-first" and item["summary"] == "One tap a day."
    assert item["body"] == "Body line."
