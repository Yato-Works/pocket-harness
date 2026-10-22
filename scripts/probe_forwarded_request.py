"""Compare the request the proxy forwards with the raw client request."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from pocket_harness.config import Config
from pocket_harness.proxy import create_app
from pocket_harness.state import StateStore

PAYLOAD = {
    "model": "m",
    "messages": [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Fix the failing test."},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ],
    "max_tokens": 1024,
}


def upstream(request: httpx.Request) -> httpx.Response:
    print("=== forwarded upstream body ===")
    print(json.dumps(json.loads(request.content.decode("utf-8")), indent=2, sort_keys=True))
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
    config.state.root = str(Path(__file__).resolve().parent.parent / ".e2e" / "probe-state")
    config.upstream.base_url = "http://upstream.test/v1"
    config.upstream.model = "m"
    config.upstream.context_length = 8192
    store = StateStore(config.state.root)
    app = create_app(config, store=store)
    app.state.service.transport = httpx.MockTransport(upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    ) as client:
        await client.post("/v1/chat/completions", json=PAYLOAD)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
