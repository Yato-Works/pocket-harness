"""Show what C3 repairs on a real upstream response."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from pocket_harness.config import load_config
from pocket_harness.proxy import create_app
from pocket_harness.state import StateStore

BASE = "http://127.0.0.1:11434/v1"
MODEL = "qwen2.5:7b-instruct"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write UTF-8 text to a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the test command and return its exit status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

CONVERSATION = [
    {
        "role": "system",
        "content": (
            "You are a careful coding assistant working inside a small Python project. "
            "Use the provided tools to inspect and change the project, then run the tests."
        ),
    },
    {
        "role": "user",
        "content": (
            "These are the files in the workspace (relative paths):\n"
            "calc.py\ntests/test_calc.py\n\n"
            "Read calc.py, fix add() so that it returns the sum, then run the tests."
        ),
    },
]


async def main() -> int:
    config = load_config("bench/bench-local.toml")
    config.state.root = str(Path(".e2e") / "repair-probe")
    store = StateStore(config.state.root)

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                f"{BASE}/chat/completions",
                json={**payload, "model": MODEL, "temperature": 0, "seed": 1},
            )
        return response

    app = create_app(config, store=store)
    app.state.service.transport = httpx.MockTransport(upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": CONVERSATION,
                "tools": TOOLS,
                "max_tokens": 1024,
                "temperature": 0,
                "seed": 1,
            },
        )
    print("client status:", response.status_code)
    body = response.json()
    for call in (body["choices"][0]["message"].get("tool_calls") or []):
        print("client received:", call["function"]["name"], call["function"]["arguments"][:120])

    run_id = store.list_runs()[0]["run_id"]
    for event in store.iter_events(run_id):
        if event["event"] != "toolcall.validated":
            continue
        payload = event["payload"]
        print(f"\nchecked={payload['checked']} repaired={payload['repaired']} failed={payload['failed']}")
        for outcome in payload["outcomes"]:
            print(
                f"  {outcome['tool']:<12} ok={outcome['ok']} text_changed={outcome.get('text_changed')} "
                f"reasons={outcome['reasons']} diff={outcome['diff']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
