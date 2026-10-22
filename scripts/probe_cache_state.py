"""Does the upstream's answer depend on the server's cache state?

The benchmark's runs are not reproducible even with a pinned temperature and
seed. One candidate cause is the server-side prompt cache: if the answer
depends on what the server processed before, then the "same" request is only
the same when the server's history is the same too.

The probe compares three orders of the same two requests.
"""

from __future__ import annotations

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


def conversation(topic: str) -> list[dict]:
    return [
        {"role": "system", "content": "You are a careful coding assistant."},
        {"role": "user", "content": f"{topic}. Read calc.py and fix add()."},
    ]


def normalize(message: dict) -> str:
    data = json.loads(json.dumps(message))
    for call in data.get("tool_calls") or []:
        call["id"] = "<id>"
    return json.dumps(data, sort_keys=True)


def ask(client: httpx.Client, topic: str) -> str:
    body = {
        "model": MODEL,
        "messages": conversation(topic),
        "tools": TOOLS,
        "max_tokens": 256,
        "temperature": 0,
        "seed": 7,
    }
    response = client.post(f"{BASE}/chat/completions", json=body, timeout=300.0)
    choices = response.json().get("choices") or []
    if not choices:
        return "<no choices>"
    return normalize(choices[0].get("message") or {})


def main() -> int:
    with httpx.Client() as client:
        isolated = [ask(client, "alpha") for _ in range(ROUNDS)]
        interleaved = []
        for _ in range(ROUNDS):
            ask(client, "beta")
            interleaved.append(ask(client, "alpha"))
        # The same request again, after a pause of other traffic.
        after = ask(client, "alpha")
    print(
        json.dumps(
            {
                "model": MODEL,
                "rounds": ROUNDS,
                "isolated_distinct": len(set(isolated)),
                "interleaved_distinct": len(set(interleaved)),
                "interleaved_matches_isolated": set(interleaved) == set(isolated),
                "after_other_traffic_matches": after == isolated[0],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
