"""Does the upstream's answer depend on what the server processed before?

The benchmark runs the two conditions one after the other on the same upstream.
If the answer depends on the server's history (the prompt cache), then the
second condition is systematically different from the first -- which would
make every paired comparison biased by the run order.

This probe sends the same request: alone, immediately again, and after an
unrelated request.
"""

from __future__ import annotations

import httpx

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
    }
]

TARGET = {
    "model": MODEL,
    "messages": [
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
                "sequence.py\ntests/test_sequence.py\n\n"
                "The tests in this project fail. Read the source, work out what is wrong "
                "with head(), and fix it so that every test passes. Do not modify the tests."
            ),
        },
    ],
    "tools": TOOLS,
    "max_tokens": 1024,
    "temperature": 0,
    "seed": 1,
}

OTHER = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "You are a careful coding assistant."},
        {"role": "user", "content": "List three colours of the rainbow. Answer briefly."},
    ],
    "tools": TOOLS,
    "max_tokens": 64,
    "temperature": 0,
    "seed": 1,
}


def describe(data: dict) -> str:
    message = ((data.get("choices") or [{}])[0]).get("message") or {}
    calls = message.get("tool_calls") or []
    if calls:
        return "tool_call:" + calls[0]["function"]["name"] + ":" + calls[0]["function"][
            "arguments"
        ][:70]
    return "text:" + (message.get("content") or "")[:90]


def main() -> int:
    with httpx.Client(timeout=300.0) as client:
        first = describe(client.post(f"{BASE}/chat/completions", json=TARGET, timeout=300.0).json())
        again = describe(client.post(f"{BASE}/chat/completions", json=TARGET, timeout=300.0).json())
        client.post(f"{BASE}/chat/completions", json=OTHER, timeout=300.0)
        after_other = describe(
            client.post(f"{BASE}/chat/completions", json=TARGET, timeout=300.0).json()
        )
        third = describe(client.post(f"{BASE}/chat/completions", json=TARGET, timeout=300.0).json())

    print("  first           :", first)
    print("  again           :", again)
    print("  after other work:", after_other)
    print("  third           :", third)
    print()
    print(f"  repeat is stable          : {first == again == third}")
    print(f"  unrelated traffic changes it: {first != after_other}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
