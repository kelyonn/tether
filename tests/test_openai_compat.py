"""The OpenAI-format adapter used to run the eval against NVIDIA (no network)."""

import json
from types import SimpleNamespace as NS

import anyio

from tether import openai_compat as oc


def test_messages_round_trip_to_openai_format():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": [NS(type="text", text="ok"),
                                              NS(type="tool_use", id="c1", name="remember",
                                                 input={"title": "x"})]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                          "content": "saved m1", "is_error": False}]}]
    out = oc.to_openai_messages("SYS", msgs)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[2]["tool_calls"][0]["function"] == {"name": "remember",
                                                   "arguments": json.dumps({"title": "x"})}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "saved m1"}


def test_response_translation_and_bad_arguments():
    data = {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "remember", "arguments": "{oops"}}]}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    r = oc.from_openai_response(data)
    assert r.stop_reason == "tool_use" and r.content[0].input == {}
    assert (r.usage.input_tokens, r.usage.output_tokens) == (7, 3)
    done = oc.from_openai_response({"choices": [{"finish_reason": "stop",
                                                 "message": {"content": "done"}}]})
    assert done.stop_reason == "end_turn" and done.content[0].text == "done"


def test_client_sends_translated_request(monkeypatch):
    c = oc.OpenAICompatClient(api_key="k")
    seen = {}

    def fake_post(body):
        seen.update(body)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "hi"}}]}
    monkeypatch.setattr(c, "_post", fake_post)
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]
    r = anyio.run(lambda: c.beta.messages.create(
        model="m", system="s", messages=[{"role": "user", "content": "x"}], tools=tools,
        effort="ignored", fallbacks="default", output_config={}))
    assert r.stop_reason == "end_turn"
    assert seen["tools"][0]["function"]["name"] == "t" and seen["model"] == "m"
