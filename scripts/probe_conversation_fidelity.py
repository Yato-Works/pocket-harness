"""Compare a multi-turn conversation before and after the proxy."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from pocket_harness.config import Config
from pocket_harness.proxy import create_app
from pocket_harness.state import StateStore

CONVERSATION = {
    "model": "m",
    "messages": [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Fix the failing test."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "calc.py"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "read_file",
            "content": "def add(a, b): return a - b",
        },
        {"role": "user", "content": "Now fix it."},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
        }
    ],
    "max_tokens": 1024,
}

forwarded: list[dict] = []


def upstream(request: httpx.Request) -> httpx.Response:
    forwarded.append(json.loads(request.content.decode("utf-8")))
    return httpx.Response(
        200,
        json={
            "id": "1",
            "object": "chat.completion",
            "model": "m",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {},
        },
    )


async def main() -> int:
    config = Config()
    config.state.root = str(Path(__file__).resolve().parent.parent / ".e2e" / "probe-state2")
    config.upstream.base_url = "http://upstream.test/v1"
    config.upstream.model = "m"
    config.upstream.context_length = 8192
    store = StateStore(config.state.root)
    app = create_app(config, store=store)
    app.state.service.transport = httpx.MockTransport(upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    ) as client:
        await client.post("/v1/chat/completions", json=CONVERSATION)

    raw = json.loads(json.dumps(CONVERSATION))
    after = forwarded[0]
    print("=== client request ===")
    print(json.dumps(raw, indent=2, sort_keys=True))
    print("\n=== forwarded request ===")
    print(json.dumps(after, indent=2, sort_keys=True))
    print("\n=== difference ===")
    print("messages equal:", raw["messages"] == after["messages"])
    if raw["messages"] != after["messages"]:
        print("client  :", json.dumps(raw["messages"], indent=2))
        print("forwarded:", json.dumps(after["messages"], indent=2))
    print("tools equal:", raw["tools"] == after.get("tools"))
    print("max_tokens:", raw["max_tokens"], "->", after.get("max_tokens"))
    print("extra keys:", sorted(set(after) - set(raw)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
