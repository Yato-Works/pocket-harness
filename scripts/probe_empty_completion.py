"""What exactly makes the upstream answer with an empty message?

Half of the benchmark runs are lost to class R (empty completion). Before any
comparison is possible again, the trigger has to be known: is it the harness,
the request shape, or the upstream?

The probe drives the real model through the same conversation the benchmark
uses, captures the exact request that produced the empty answer, and then
replays it directly against the upstream, varying one thing at a time.
"""

from __future__ import annotations

import json

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
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the test command and return its exit status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# The conversation shape the benchmark produces: the listing, the user turn, a
# tool call, its result, and then the same call again.
BASE_CONVERSATION = [
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
            "Change the files that need changing, then run the tests."
        ),
    },
]

TOOL_RESULT = "exit code 1\nF. [100%]\n1 failed, 1 passed in 0.41s"


def call(name: str, arguments: str, call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def ask(client: httpx.Client, messages: list[dict], **extra) -> dict:
    body = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "max_tokens": 1024,
        "temperature": 0,
        "seed": 1,
    }
    body.update(extra)
    response = client.post(f"{BASE}/chat/completions", json=body, timeout=300.0)
    choices = response.json().get("choices") or []
    if not choices:
        return {"kind": "no_choices"}
    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if message.get("tool_calls"):
        kind = "tool_call"
    elif not content:
        kind = "empty"
    else:
        kind = "text"
    return {"kind": kind, "message": message}


def main() -> int:
    results = []
    with httpx.Client() as client:
        # 1) the base turn
        first = ask(client, BASE_CONVERSATION)
        results.append({"case": "first_turn", **first})

        # 2) after one tool call and its result
        after_one = BASE_CONVERSATION + [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [call("read_file", '{"path": "sequence.py"}', "call_1")],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": TOOL_RESULT},
        ]
        results.append({"case": "after_one_tool_result", **ask(client, after_one)})

        # 3) after the same call repeated twice
        after_two = after_one + [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [call("read_file", '{"path": "sequence.py"}', "call_2")],
            },
            {"role": "tool", "tool_call_id": "call_2", "name": "read_file", "content": TOOL_RESULT},
        ]
        results.append({"case": "after_two_repeats", **ask(client, after_two)})

        # 4) the same, but with a *different* second call
        after_two_different = after_one + [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [call("read_file", '{"path": "other.py"}', "call_2")],
            },
            {"role": "tool", "tool_call_id": "call_2", "name": "read_file", "content": TOOL_RESULT},
        ]
        results.append(
            {"case": "after_two_different_calls", **ask(client, after_two_different)}
        )

        # 5) the repeated case with a short tool result
        short_result = after_two[:-1] + [
            {"role": "tool", "tool_call_id": "call_2", "name": "read_file", "content": "ok"}
        ]
        results.append({"case": "two_repeats_short_result", **ask(client, short_result)})

        # 6) the repeated case without the tools
        results.append(
            {"case": "two_repeats_no_tools", **ask(client, after_two, tools=[])}
        )

        # 7) the repeated case with a small max_tokens
        results.append(
            {"case": "two_repeats_max_tokens_128", **ask(client, after_two, max_tokens=128)}
        )

        # 8) the repeated case with the result of a *successful* test run
        ok_result = after_two[:-1] + [
            {
                "role": "tool",
                "tool_call_id": "call_2",
                "name": "run_tests",
                "content": "exit code 0\n2 passed in 0.12s",
            }
        ]
        results.append({"case": "two_repeats_passing_result", **ask(client, ok_result)})

        # 9) three *different* tool results, each with a substantial body
        three_different = list(after_one)
        bodies = [
            "def one():\n    return 1\n",
            "def two():\n    return 2\n",
            "def three():\n    return 3\n",
        ]
        for index, body in enumerate(bodies):
            three_different += [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        call("read_file", json.dumps({"path": f"f{index}.py"}), f"c{index}")
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"c{index}",
                    "name": "read_file",
                    "content": body,
                },
            ]
        results.append({"case": "three_different_results", **ask(client, three_different)})

        # 10) two different results
        two_different = list(after_one)
        for index, body in enumerate(["def one():\n    return 1\n", "def two():\n    return 2\n"]):
            two_different += [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        call("read_file", json.dumps({"path": f"f{index}.py"}), f"c{index}")
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"c{index}",
                    "name": "read_file",
                    "content": body,
                },
            ]
        results.append({"case": "two_different_results", **ask(client, two_different)})

        # 11) three different results, each very short
        three_short = list(after_one)
        for index in range(3):
            three_short += [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        call("read_file", json.dumps({"path": f"f{index}.py"}), f"c{index}")
                    ],
                },
                {"role": "tool", "tool_call_id": f"c{index}", "name": "read_file", "content": "x = 1\n"},
            ]
        results.append({"case": "three_short_results", **ask(client, three_short)})

    for result in results:
        message = result.get("message") or {}
        content = (message.get("content") or "").strip()
        print(
            f"{result['case']:<34} {result['kind']:<10} "
            f"content={content[:60]!r} "
            f"calls={len(message.get('tool_calls') or [])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
