"""Is the local upstream reproducible for a multi-turn tool conversation?

The benchmark depends on this: if the upstream answers the same request with
different bytes, the three "seeds" are three samples of a noisy process and the
paired comparison loses its footing.
"""

import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:11434/v1"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "qwen2.5:7b-instruct"
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 4

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
    }
]

CONVERSATION = [
    {
        "role": "system",
        "content": "You are a careful coding assistant working inside a small Python project.",
    },
    {"role": "user", "content": "Read calc.py and fix it, then run the tests."},
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
        "content": "def add(a, b):\n    return a - b\n",
    },
]


def main() -> int:
    outputs: list[str] = []
    with httpx.Client(timeout=300.0) as client:
        for _ in range(ROUNDS):
            response = client.post(
                f"{BASE}/chat/completions",
                json={
                    "model": MODEL,
                    "messages": CONVERSATION,
                    "tools": TOOLS,
                    "max_tokens": 1024,
                },
            )
            message = response.json()["choices"][0]["message"]
            outputs.append(json.dumps(message, sort_keys=True))
    print(f"model: {MODEL}  rounds: {ROUNDS}")
    print(f"identical answers for the identical request: {len(set(outputs)) == 1}")
    for index, output in enumerate(outputs):
        print(f"  [{index}] {output[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
