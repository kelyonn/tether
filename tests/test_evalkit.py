"""The eval harness, driven by a scripted fake model (no API calls)."""

from types import SimpleNamespace as NS

import anyio

from tether import evalkit


class FakeClient:
    """Plays back a fixed list of responses; records every request."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kw):
        # snapshot: the harness keeps appending to the same messages list
        self.requests.append({**kw, "messages": list(kw["messages"])})
        return self.script.pop(0)


def tool_use(name, inp, i="t1"):
    return NS(stop_reason="tool_use", usage=NS(input_tokens=100, output_tokens=20),
              content=[NS(type="tool_use", id=i, name=name, input=inp)])


def done(text="ok"):
    return NS(stop_reason="end_turn", usage=NS(input_tokens=50, output_tokens=5),
              content=[NS(type="text", text=text)])


SCN = {"name": "s", "surface": "chat",
       "turns": ["reading app idea: swipe to save quotes"],
       "checks": [{"kind": "memory_saved", "contains": ["quote"], "rule": "autosave"},
                  {"kind": "no_project_writes", "rule": "no-guessing"}]}


def test_passing_run_and_request_shape():
    client = FakeClient([tool_use("remember", {"title": "Reading app", "body": "save quotes",
                                               "summary": "quotes"}), done()])
    r = anyio.run(evalkit.run_scenario, SCN, client)
    assert r.passed and r.tool_calls == ["remember"] and r.input_tokens == 150
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert req["output_config"] == {"effort": "medium"}
    assert any(t["name"] == "remember" for t in req["tools"])
    assert "Tether" in req["system"]
    # the tool result went back to the model in the same conversation
    assert client.requests[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_failing_run_and_summary():
    r = anyio.run(evalkit.run_scenario, SCN, FakeClient([done("sure")]))
    assert not r.passed
    s = evalkit.summarize([r])
    assert s["rules"]["autosave"] == {"passed": 0, "total": 1, "rate": 0.0}
    assert s["rules"]["no-guessing"]["rate"] == 1.0
    assert "FAIL" in evalkit.report([r], s)


def test_refusal_is_an_error_not_a_crash():
    refusal = NS(stop_reason="refusal", usage=None, content=[])
    r = anyio.run(evalkit.run_scenario, SCN, FakeClient([refusal]))
    assert r.error and "refusal" in r.error and not r.passed


def test_code_scenarios_get_the_session_start_pack():
    scn = next(s for s in evalkit.load_scenarios() if s["name"] == "task_ticked")
    client = FakeClient([tool_use("update_task", {"task": "t1", "status": "done",
                                                  "note": "24 cases"}), done()])
    r = anyio.run(evalkit.run_scenario, scn, client)
    assert "Add pytest coverage" in client.requests[0]["system"]  # pack injected like the hook
    assert r.passed, r.checks


def test_every_shipped_scenario_is_well_formed():
    kinds = {"memory_saved", "memory_count", "decision", "superseded", "tasks_added",
             "task_status", "handoff", "status_changed", "no_writes", "no_project_writes",
             "no_text"}
    for scn in evalkit.load_scenarios():
        assert scn["turns"] and scn["checks"]
        assert {c["kind"] for c in scn["checks"]} <= kinds, scn["name"]
        # seeding works for every scenario
        r = anyio.run(evalkit.run_scenario, scn, FakeClient([done()] * len(scn["turns"])))
        assert r.error is None, (scn["name"], r.error)
