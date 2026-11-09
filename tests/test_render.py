from tether.render import context_pack, detail, mirror_markdown


def test_pack_hides_closed_and_superseded(store, project):
    pid = project["id"]
    done = store.add_task(pid, "OLD FINISHED TASK")
    store.update_task(done["id"], status="done")
    store.add_task(pid, "open task")
    d = store.add_decision(pid, "OLD DECISION")
    store.add_decision(pid, "new decision", supersedes=d["id"])
    pack = context_pack(store, pid, "code")
    assert "open task" in pack and "new decision" in pack
    assert "OLD FINISHED TASK" not in pack.split("## Open tasks")[1]
    assert "OLD DECISION" not in pack.split("## Decisions")[1]
    assert "1 closed and hidden" in pack


def test_pack_puts_my_tasks_and_handoffs_first(store, project):
    pid = project["id"]
    store.add_task(pid, "design thing", for_surface="design")
    store.add_task(pid, "code thing", for_surface="code")
    store.add_handoff(pid, "code", "Mockup ready", "body", surface="design")
    store.add_handoff(pid, "design", "Not for code", surface="chat")
    pack = context_pack(store, pid, "code")
    assert pack.index("## Handed to you (1)") < pack.index("## Open tasks")
    assert "Not for code" not in pack.split("## Handed to you")[1].split("##")[0]
    assert pack.index("code thing") < pack.index("design thing")


def test_since_last_visit_shows_only_other_surfaces(store, project):
    pid = project["id"]
    context_pack(store, pid, "code")  # first visit marks seen
    store.add_task(pid, "added by chat", surface="chat")
    store.add_task(pid, "added by code itself", surface="code")
    pack = context_pack(store, pid, "code")
    section = pack.split("## Since code was last here")[1].split("##")[0]
    assert "added by chat" in section and "added by code itself" not in section
    again = context_pack(store, pid, "code")
    assert "Since code was last here" not in again


def test_pack_respects_budget(store, project):
    pid = project["id"]
    for i in range(80):
        store.add_task(pid, f"task number {i} with a reasonably long descriptive title here",
                       for_surface="design" if i % 2 else "code")
        store.add_decision(pid, f"decision {i} " + "x" * 80, why="because " + "y" * 80)
    pack = context_pack(store, pid, "code", budget_tokens=800)
    assert len(pack) <= 800 * 4 * 1.25  # shrinks to (or near) the floor of each section
    assert "more" in pack  # tells the model there is more to fetch


def test_detail_and_mirror(store, project):
    pid = project["id"]
    d1 = store.add_decision(pid, "A", "why a")
    d2 = store.add_decision(pid, "B", supersedes=d1["id"])
    assert f"replaces d{d1['id']}" in detail(store, f"d{d2['id']}")
    md = mirror_markdown(store, pid)
    assert "# My App" in md and "TETHER UPDATE" in md
