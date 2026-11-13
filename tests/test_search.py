import sqlite3

from tether.store import SCHEMA, Store
from tether.store.schema import MIGRATIONS, rebuild_fts


def titles(hits):
    out = []
    for h in hits:
        row = h[1]
        out.append(row["title"] if "title" in row.keys() else row["text"] if "text" in row.keys() else row["name"])
    return out


def test_best_match_ranks_first(store):
    store.add_memory("Notes", "we mention stripe once among many other words " * 3)
    store.add_memory("Stripe webhook", "stripe stripe stripe")
    hits = [r for k, r, _ in store.recall("stripe") if k == "memories"]
    assert hits[0]["title"] == "Stripe webhook"


def test_prefix_and_all_words_must_match(store):
    store.add_memory("Sailing app", "tides and knots")
    store.add_memory("Cooking app", "tides of flavour")
    assert titles(store.recall("sail")) == ["Sailing app"]
    assert titles(store.recall("tides knots")) == ["Sailing app"]
    assert len(store.recall("tides")) == 2


def test_substring_still_found_through_the_fallback(store):
    store.add_memory("Boat", "built from cedarwood planks")
    assert titles(store.recall("wood")) == ["Boat"]


def test_index_follows_edits_archiving_and_new_records(store):
    m = store.add_memory("Alpha idea", "original text")
    assert store.recall("original")
    store.update_memory(m["id"], body="replacement words")
    assert not store.recall("original")
    assert store.recall("replacement")
    store.archive_memory(m["id"])
    assert not store.recall("replacement")
    p = store.create_project("Shop", "sell mugs")
    store.add_decision(p["id"], "Use Stripe Checkout", "fastest to ship")
    kinds = {k for k, _, _ in store.recall("checkout")}
    assert "decisions" in kinds


def test_per_project_search_is_scoped_and_finds_closed_work(store):
    a = store.create_project("Alpha", "a")
    b = store.create_project("Beta", "b")
    t = store.add_task(a["id"], "Fix the invoice export")
    store.update_task(t["id"], status="done")
    store.add_task(b["id"], "Fix the invoice email")
    found = store.search(a["id"], "invoice")
    assert [r["title"] for k, r in found if k == "tasks"] == ["Fix the invoice export"]


def test_notes_are_searchable_but_other_events_are_not(store):
    p = store.create_project("Shop", "x")
    store.note(p["id"], "shipped the pricing page to staging")
    assert any(k == "events" for k, _ in store.search(p["id"], "pricing"))
    assert not any(k == "events" for k, _ in store.search(p["id"], "created"))


def test_awkward_queries_never_raise(store):
    store.add_memory("Quote", 'she said "hello" (twice) - and left')
    for q in ['"hello"', "(twice)", "-and", "a OR b", "col:umn", "*", "NEAR(x y)", "it's"]:
        store.recall(q)


def test_without_fts5_search_still_works(store):
    store.add_memory("Sailing app", "tides")
    store.db.execute("DROP TABLE search_idx")
    assert titles(store.recall("sailing")) == ["Sailing app"]


def test_upgrading_a_v5_database_builds_the_index(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for i, script in enumerate(MIGRATIONS[:4], start=2):
        con.executescript(f"BEGIN; {script}; PRAGMA user_version={i}; COMMIT;")
    con.execute("INSERT INTO memories (title, body, surface, created_at, updated_at) "
                "VALUES ('Old idea', 'predates the index', 'chat', 't', 't')")
    con.commit()
    con.close()
    s = Store(path)
    try:
        assert s.db.execute("PRAGMA user_version").fetchone()[0] >= 6
        assert titles(s.recall("predates")) == ["Old idea"]
    finally:
        s.close()


def test_rebuild_matches_the_trigger_maintained_index(store):
    p = store.create_project("Shop", "sell mugs")
    store.add_task(p["id"], "build page")
    store.add_memory("Idea", "body")
    before = sorted(map(tuple, store.db.execute("SELECT kind, ref, text FROM search_idx").fetchall()))
    rebuild_fts(store.db)
    after = sorted(map(tuple, store.db.execute("SELECT kind, ref, text FROM search_idx").fetchall()))
    assert before == after
