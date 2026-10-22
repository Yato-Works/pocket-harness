"""Is the local upstream reproducible, and under which settings?

The benchmark cannot separate the proxy's effect from the upstream's noise
unless the upstream answers the same request the same way. This probe measures
that directly, per model and per setting, and records three things the
evaluation needs:

* how often the *same* request produces the *same* answer,
* which tool-call format the upstream actually returns,
* how often the upstream returns an empty completion.

The settings are the ones a benchmark could plausibly pin: the default
sampling, greedy decoding (temperature 0), and greedy decoding with a fixed
seed.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Any

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:11434/v1"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
ONLY = sys.argv[3] if len(sys.argv) > 3 else ""

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

# The conversation shape the benchmark uses: a system prompt, a user turn, a
# tool call and a tool result. A short single-turn prompt was reproducible on
# this upstream; the multi-turn shape is where the divergence showed up.
CONVERSATION = [
    {
        "role": "system",
        "content": (
            "You are a careful coding assistant working inside a small Python project. "
            "Use the provided tools to inspect and change the project, then run the tests."
        ),
    },
    {"role": "user", "content": "Read calc.py, fix add() so it returns the sum, then run the tests."},
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

SETTINGS: list[dict[str, Any]] = [
    {"name": "default", "body": {}},
    {"name": "temperature_0", "body": {"temperature": 0}},
    {"name": "temperature_0_seed_42", "body": {"temperature": 0, "seed": 42}},
]


def classify(message: dict[str, Any]) -> str:
    if message.get("tool_calls"):
        return "structured_tool_call"
    content = message.get("content") or ""
    if not str(content).strip():
        return "empty"
    if '"arguments"' in str(content):
        return "tool_call_as_text"
    return "prose"


def normalize(message: dict[str, Any]) -> str:
    """Return the answer without the parts the upstream randomises.

    Ollama generates a random tool-call id for every call. Counting that as a
    difference would report "not reproducible" for an answer that is in fact
    identical, so the id is replaced by a placeholder.
    """
    data = json.loads(json.dumps(message))
    for call in data.get("tool_calls") or []:
        call["id"] = "<id>"
    return json.dumps(data, sort_keys=True)


def probe(client: httpx.Client, model: str, setting: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": CONVERSATION,
        "tools": TOOLS,
        "max_tokens": 512,
    }
    body.update(setting["body"])
    answers: list[str] = []
    formats: Counter[str] = Counter()
    errors = 0
    for _ in range(ROUNDS):
        try:
            response = client.post(f"{BASE}/chat/completions", json=body, timeout=300.0)
        except httpx.HTTPError:
            errors += 1
            continue
        if response.status_code != 200:
            errors += 1
            continue
        choices = response.json().get("choices") or []
        if not choices:
            formats["no_choices"] += 1
            continue
        message = choices[0].get("message") or {}
        formats[classify(message)] += 1
        answers.append(normalize(message))
    identical = len(set(answers))
    return {
        "model": model,
        "setting": setting["name"],
        "request_body": {k: v for k, v in setting["body"].items()},
        "rounds": ROUNDS,
        "errors": errors,
        "distinct_answers": identical,
        "reproducible": identical <= 1 and errors == 0,
        "formats": dict(sorted(formats.items())),
        "empty_rate": round(formats.get("empty", 0) / max(1, ROUNDS), 3),
    }


def main() -> int:
    with httpx.Client() as client:
        try:
            models = client.get(f"{BASE}/models", timeout=30).json()["data"]
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
            return 1
        results = []
        for entry in models:
            name = entry["id"]
            if ONLY and ONLY not in name:
                continue
            for setting in SETTINGS:
                result = probe(client, name, setting)
                results.append(result)
                print(json.dumps(result), flush=True)
    reproducible = [r for r in results if r["reproducible"]]
    print(
        json.dumps(
            {
                "summary": {
                    "probes": len(results),
                    "reproducible": len(reproducible),
                    "settings_that_work": sorted(
                        {r["setting"] for r in reproducible}
                    ),
                    "models_that_work": sorted({r["model"] for r in reproducible}),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
