"""Does the seed change the answer, or is it ignored?

Plan.md §11.2 wants three *predeclared seeds* per task and condition. That is
only meaningful if the seed changes what the model produces. This probe sends
the same request with different seeds under greedy decoding and reports
whether the answers differ.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:11434/v1"
SEEDS = [1, 2, 3]
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 3

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
        "content": (
            "You are a careful coding assistant working inside a small Python project."
        ),
    },
    {
        "role": "user",
        "content": "Read calc.py, fix add() so it returns the sum, then run the tests.",
    },
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


def normalize(message: dict[str, Any]) -> str:
    data = json.loads(json.dumps(message))
    for call in data.get("tool_calls") or []:
        call["id"] = "<id>"
    return json.dumps(data, sort_keys=True)


def main() -> int:
    with httpx.Client() as client:
        try:
            models = client.get(f"{BASE}/models", timeout=30).json()["data"]
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
            return 1
        for entry in models:
            model = entry["id"]
            per_seed: dict[int, set[str]] = {}
            for seed in SEEDS:
                answers: set[str] = set()
                for _ in range(ROUNDS):
                    body = {
                        "model": model,
                        "messages": CONVERSATION,
                        "tools": TOOLS,
                        "max_tokens": 512,
                        "temperature": 0,
                        "seed": seed,
                    }
                    try:
                        response = client.post(
                            f"{BASE}/chat/completions", json=body, timeout=300.0
                        )
                        choices = response.json().get("choices") or []
                    except Exception:  # noqa: BLE001
                        continue
                    if not choices:
                        continue
                    answers.add(normalize(choices[0].get("message") or {}))
                per_seed[seed] = answers
            stable = all(len(answers) <= 1 for answers in per_seed.values())
            distinct_seeds = len({next(iter(answers)) for answers in per_seed.values() if answers})
            print(
                json.dumps(
                    {
                        "model": model,
                        "stable_per_seed": stable,
                        "distinct_answers_across_seeds": distinct_seeds,
                        "per_seed_distinct": {str(k): len(v) for k, v in per_seed.items()},
                    }
                ),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
