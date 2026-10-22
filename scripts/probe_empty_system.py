"""Which part of the system prompt makes the model go silent?

The bisect showed that the empty answer needs the system message, the task
message and at least two substantial tool results. This varies the system
prompt to see whether it is the *persona* or merely the *presence* of a system
message.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:11434/v1"

VARIANTS = {
    "as_captured": None,
    "short_persona": "You are a careful coding assistant.",
    "without_the_tool_instruction": (
        "You are a careful coding assistant working inside a small Python project."
    ),
    "instruction_first": (
        "Read the code, change what is wrong, then run the tests.\n"
        "You are a careful coding assistant working inside a small Python project. "
        "Use the provided tools to inspect and change the project, then run the tests."
    ),
    "no_system_message": "",
    "assistant_persona": (
        "You are an expert Python developer. Use the provided tools to inspect and change "
        "the project, then run the tests."
    ),
    "positive_framing": (
        "You are a careful coding assistant working inside a small Python project. "
        "Use the provided tools to inspect the files that are listed, change what needs "
        "changing, and then run the tests."
    ),
    "without_negatives": (
        "You are a careful coding assistant working inside a small Python project. "
        "Use the provided tools to inspect and change the project, then run the tests."
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
    captured_system = messages[0].get("content") or ""

    with httpx.Client(timeout=300.0) as client:
        for name, system in VARIANTS.items():
            variant = dict(body)
            if system is None:
                variant["messages"] = list(messages)
            elif system == "":
                variant["messages"] = messages[1:]
            else:
                variant["messages"] = [dict(messages[0], content=system)] + list(messages[1:])
            data = client.post(f"{BASE}/chat/completions", json=variant).json()
            message = ((data.get("choices") or [{}])[0]).get("message") or {}
            usage = data.get("usage") or {}
            print(
                f"  {name:<28} -> {kind_of(message):<10} "
                f"completion_tokens={usage.get('completion_tokens')}"
            )
    print(f"\ncaptured system prompt: {captured_system!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
