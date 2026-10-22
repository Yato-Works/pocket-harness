"""Can the client recover from an empty answer?

The upstream sometimes answers with a generation that decodes into nothing
(class R). The client sees an empty message. A real agent can say "your last
answer was empty, continue" — that is a client behaviour, not a proxy one, and
it applies identically to both conditions.

This probe asks whether such a nudge recovers the answer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:11434/v1"

NUDGES = {
    "continue": "Your last response was empty. Please continue with the tool call.",
    "short_continue": "Your last response was empty. Continue.",
    "state_the_task": (
        "Your last response was empty. State what you are doing and make the next "
        "tool call."
    ),
    "repeat_the_call": (
        "Your last response was empty. Repeat the tool call you were about to make."
    ),
}


def kind_of(message: dict) -> str:
    if message.get("tool_calls"):
        return "tool_call"
    content = (message.get("content") or "").strip()
    return "text" if content else "empty"


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else ".e2e/empty-probe/failing-request.json")
    body = json.loads(path.read_text(encoding="utf-8"))
    messages = body["messages"]

    with httpx.Client(timeout=300.0) as client:
        baseline = client.post(f"{BASE}/chat/completions", json=body).json()
        base_message = ((baseline.get("choices") or [{}])[0]).get("message") or {}
        print(f"without a nudge: {kind_of(base_message)}")

        for name, nudge in NUDGES.items():
            variant = dict(body)
            variant["messages"] = list(messages) + [{"role": "user", "content": nudge}]
            data = client.post(f"{BASE}/chat/completions", json=variant).json()
            message = ((data.get("choices") or [{}])[0]).get("message") or {}
            usage = data.get("usage") or {}
            print(f"  nudge {name:<18} -> {kind_of(message):<10} "
                  f"completion_tokens={usage.get('completion_tokens')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
