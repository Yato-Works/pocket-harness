"""Is the upstream sensitive to byte-level changes in an equivalent request?

The benchmark sends the *canonical* request through the proxy, so the bytes
differ from the client's request even when the meaning is identical. If the
upstream's answer depends on the exact bytes, then **every** proxy
intervention perturbs the outcome, and the proxy can never be transparent
against this upstream.

This probe sends two semantically identical requests that differ only in the
whitespace of the tool arguments, and compares the answers.
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
            "sequence.py\ntests/test_sequence.py\n\n"
            "The tests in this project fail. Read the source, work out what is wrong "
            "with head(), and fix it so that every test passes. Do not modify the tests."
        ),
    },
]


def variant(arguments: str) -> dict:
    """The same conversation with the tool arguments spelled differently."""
    return {
        "model": MODEL,
        "messages": CONVERSATION
        + [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": arguments},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": "def head(values, count):\n    return values[: count - 1]\n",
            },
        ],
        "tools": TOOLS,
        "max_tokens": 1024,
        "temperature": 0,
        "seed": 1,
    }


def describe(data: dict) -> str:
    message = ((data.get("choices") or [{}])[0]).get("message") or {}
    calls = message.get("tool_calls") or []
    if calls:
        return "tool_call:" + calls[0]["function"]["name"] + ":" + calls[0]["function"]["arguments"][:60]
    return "text:" + (message.get("content") or "")[:80]


def main() -> int:
    variants = {
        "compact": '{"path":"sequence.py"}',
        "spaced": '{"path": "sequence.py"}',
        "sorted": '{"path": "sequence.py"}',
    }
    with httpx.Client(timeout=300.0) as client:
        answers = {}
        for name, arguments in variants.items():
            response = client.post(
                f"{BASE}/chat/completions", json=variant(arguments), timeout=300.0
            )
            answers[name] = describe(response.json())
        for name, answer in answers.items():
            print(f"  {name:<10} {answer}")
    print(f"\nidentical answers: {len(set(answers.values())) == 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
