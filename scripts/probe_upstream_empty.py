"""Probe how often the local upstream answers with an empty message.

An empty completion with no tool call means the client cannot make progress.
This measures the rate for the model under test, with the same request shape
the benchmark uses.
"""

import json
import sys
from collections import Counter

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:11434/v1"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "qwen2.5:7b-instruct"
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 12

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

PROMPT = (
    "The file calc.py contains a function add(a, b) that returns a - b. "
    "Fix it so that it returns the sum, then run the tests. Use the provided tools only."
)


def main() -> int:
    counts: Counter[str] = Counter()
    samples: list[dict] = []
    with httpx.Client(timeout=300.0) as client:
        for _index in range(ROUNDS):
            response = client.post(
                f"{BASE}/chat/completions",
                json={
                    "model": MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a careful coding assistant working inside a "
                            "small Python project. Use the provided tools to inspect and change "
                            "the project, then run the tests.",
                        },
                        {"role": "user", "content": PROMPT},
                    ],
                    "tools": TOOLS,
                    "max_tokens": 1024,
                },
            )
            if response.status_code != 200:
                counts[f"http_{response.status_code}"] += 1
                continue
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                counts["no_choices"] += 1
                continue
            message = choices[0].get("message") or {}
            calls = message.get("tool_calls") or []
            content = message.get("content") or ""
            if calls:
                counts["structured_tool_calls"] += 1
            elif content.strip():
                counts["text_only"] += 1
                if '"name"' in content and '"arguments"' in content:
                    counts["text_tool_call"] += 1
                    if len(samples) < 3:
                        samples.append({"content": content[:400]})
            else:
                counts["empty"] += 1
                if len(samples) < 3:
                    samples.append({"raw": json.dumps(data)[:600]})
    print(f"model: {MODEL}")
    print(f"rounds: {ROUNDS}")
    for key, value in sorted(counts.items()):
        print(f"  {key:<24} {value}")
    for sample in samples:
        print("\nsample:", json.dumps(sample, indent=2)[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
