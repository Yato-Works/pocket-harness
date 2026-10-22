"""Bisect the failing conversation one message at a time.

The coarse variants (all tool results, no tool results, shortened results)
did not isolate the trigger. This removes exactly one message at a time and
reports which removal makes the empty answer disappear.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:11434/v1"


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
        print(f"baseline answer kind: {kind_of(base_message)}")

        for index in range(len(messages)):
            variant = dict(body)
            variant["messages"] = messages[:index] + messages[index + 1 :]
            data = client.post(f"{BASE}/chat/completions", json=variant).json()
            message = ((data.get("choices") or [{}])[0]).get("message") or {}
            role = messages[index].get("role")
            preview = str(messages[index].get("content") or "")[:50].replace("\n", " ")
            calls = len(messages[index].get("tool_calls") or [])
            print(
                f"  without message {index} ({role}, calls={calls}, {preview!r}): "
                f"{kind_of(message)}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
