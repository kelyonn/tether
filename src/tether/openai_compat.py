"""Run the rule eval against an OpenAI-compatible chat API (NVIDIA NIM by default).

`evalkit` talks to the Anthropic SDK shape (`client.beta.messages.create`). This adapter
exposes the same call, translates messages and tools to the OpenAI chat format, and
translates the reply back, so scenarios and checks are unchanged. Stdlib only.

Free key: https://build.nvidia.com  ->  export NVIDIA_API_KEY=nvapi-...
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from types import SimpleNamespace as NS
from typing import Any

import anyio

from tether.errors import TetherError

NVIDIA_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "mistralai/mistral-large-4-1t-a52b"
_FINISH = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


def _get(o: Any, k: str, default: Any = None) -> Any:
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if m["role"] == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            for b in content:  # tool results
                out.append({"role": "tool", "tool_call_id": _get(b, "tool_use_id"),
                            "content": str(_get(b, "content", ""))})
            continue
        text = "".join(_get(b, "text", "") for b in content if _get(b, "type") == "text")
        calls = [{"id": _get(b, "id"), "type": "function",
                  "function": {"name": _get(b, "name"),
                               "arguments": json.dumps(_get(b, "input", {}))}}
                 for b in content if _get(b, "type") == "tool_use"]
        msg: dict = {"role": "assistant", "content": text or None}
        if calls:
            msg["tool_calls"] = calls
        out.append(msg)
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]


def from_openai_response(data: dict) -> NS:
    choice = data["choices"][0]
    msg = choice["message"]
    blocks = []
    if msg.get("content"):
        blocks.append(NS(type="text", text=msg["content"]))
    for c in msg.get("tool_calls") or []:
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        blocks.append(NS(type="tool_use", id=c["id"], name=c["function"]["name"], input=args))
    usage = data.get("usage") or {}
    stop = "tool_use" if any(b.type == "tool_use" for b in blocks) \
        else _FINISH.get(choice.get("finish_reason"), "end_turn")
    return NS(stop_reason=stop, content=blocks,
              usage=NS(input_tokens=usage.get("prompt_tokens", 0),
                       output_tokens=usage.get("completion_tokens", 0)))


class OpenAICompatClient:
    def __init__(self, api_key: str | None = None, base_url: str = NVIDIA_URL):
        self.api_key = api_key or os.environ.get("NVIDIA_API_KEY", "")
        if not self.api_key:
            raise TetherError("set NVIDIA_API_KEY (free key at https://build.nvidia.com)")
        self.base_url = base_url.rstrip("/")
        self.beta = NS(messages=NS(create=self._create))

    async def __aenter__(self) -> "OpenAICompatClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def _create(self, *, model: str, system: str, messages: list[dict],
                      tools: list[dict], max_tokens: int = 4096, **_ignored: Any) -> NS:
        body = {"model": model, "messages": to_openai_messages(system, messages),
                "tools": to_openai_tools(tools), "max_tokens": min(max_tokens, 4096),
                "temperature": 0}
        data = await anyio.to_thread.run_sync(self._post, body)
        return from_openai_response(data)

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", json.dumps(body).encode(),
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 4:
                    time.sleep(2 ** attempt)  # free tier rate limits
                    continue
                raise RuntimeError(f"HTTP {e.code} from {self.base_url}: "
                                   f"{e.read().decode(errors='replace')[:300]}") from e
        raise RuntimeError("unreachable")
