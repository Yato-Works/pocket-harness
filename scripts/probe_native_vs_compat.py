"""Compare the native Ollama endpoint with the OpenAI-compatible one.

The empty answers are reported with ``completion_tokens: 65`` — the model
generated something, but the OpenAI-compatible response carried an empty
``content`` and no ``tool_calls``. This probe asks the native endpoint for the
same conversation to find out what the model actually produced.
"""

from __future__ import annotations

import json

import httpx

NATIVE = "http://127.0.0.1:11434/api/chat"
OPENAI = "http://127.0.0.1:11434/v1"
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


def build_conversation() -> list[dict]:
    return [
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
                "The tests in this project fail. Read the source, work out what is wrong with "
                "head(), and fix it so that every test passes. Do not modify the tests."
            ),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "sequence.py"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "read_file",
            "content": (
                '"""Sequence helpers."""\n\n\ndef head(values, count):\n'
                '    """Return the first ``count`` items of the sequence."""\n'
                "    if count <= 0:\n        return []\n    return values[: count - 1]\n"
            ),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "tests/test_sequence.py"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "name": "read_file",
            "content": (
                "from sequence import head\n\n\n"
                "def test_head_returns_the_requested_count():\n"
                "    assert head([1, 2, 3, 4], 2) == [1, 2]\n\n\n"
                "def test_head_of_a_short_sequence():\n"
                "    assert head([1], 5) == [1]\n\n\n"
                "def test_head_of_zero():\n    assert head([1, 2], 0) == []\n"
            ),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_3",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "conftest.py"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_3",
            "name": "read_file",
            "content": (
                '"""Make the project modules importable from the tests."""\n\n'
                "import os\nimport sys\n\nsys.path.insert(0, os.path.dirname(__file__))\n"
            ),
        },
    ]


def main() -> int:
    conversation = build_conversation()
    with httpx.Client(timeout=300.0) as client:
        native = client.post(
            NATIVE,
            json={
                "model": MODEL,
                "messages": conversation,
                "tools": TOOLS,
                "stream": False,
                "options": {"temperature": 0, "seed": 1},
            },
        )
        data = native.json()
        message = data.get("message") or {}
        print("=== native /api/chat ===")
        print("  done_reason:", data.get("done_reason"))
        print("  eval_count:", data.get("eval_count"), "prompt:", data.get("prompt_eval_count"))
        print("  content:", repr((message.get("content") or "")[:300]))
        print("  tool_calls:", json.dumps(message.get("tool_calls") or [])[:300])

        compat = client.post(
            f"{OPENAI}/chat/completions",
            json={
                "model": MODEL,
                "messages": conversation,
                "tools": TOOLS,
                "max_tokens": 1024,
                "temperature": 0,
                "seed": 1,
            },
        )
        cdata = compat.json()
        cmessage = ((cdata.get("choices") or [{}])[0]).get("message") or {}
        print("\n=== openai /v1/chat/completions ===")
        print("  usage:", json.dumps(cdata.get("usage")))
        print("  content:", repr((cmessage.get("content") or "")[:300]))
        print("  tool_calls:", json.dumps(cmessage.get("tool_calls") or [])[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
