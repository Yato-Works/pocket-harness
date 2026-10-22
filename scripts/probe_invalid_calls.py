"""Show the tool calls the model produces that fail validation.

The benchmark records 26-36 schema-invalid tool calls per condition. This
probe asks the model directly and prints the arguments it produced, so the
C3 opportunity can be characterised instead of guessed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from pocket_harness.agent import SYSTEM_PROMPT, TOOL_SCHEMAS
from pocket_harness.components.tool_call_repair import ToolCallRepairer

BASE = "http://127.0.0.1:11434/v1"
MODEL = "qwen2.5:7b-instruct"

SCHEMAS = {
    tool["function"]["name"]: tool["function"]["parameters"] for tool in TOOL_SCHEMAS
}

CONVERSATIONS = [
    [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "These are the files in the workspace (relative paths):\n"
                "sequence.py\ntests/test_sequence.py\n\n"
                "The tests in this project fail. Read the source, work out what is wrong "
                "with head(), and fix it so that every test passes. Do not modify the tests."
            ),
        },
    ],
    [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "These are the files in the workspace (relative paths):\n"
                "calc.py\ntests/test_calc.py\n\n"
                "The project has one failing test. Run the test suite, find out what is "
                "wrong, and fix the source code so that every test passes."
            ),
        },
    ],
]


def main() -> int:
    repairer = ToolCallRepairer(SCHEMAS)
    with httpx.Client(timeout=300.0) as client:
        for index, messages in enumerate(CONVERSATIONS):
            response = client.post(
                f"{BASE}/chat/completions",
                json={
                    "model": MODEL,
                    "messages": messages,
                    "tools": TOOL_SCHEMAS,
                    "max_tokens": 1024,
                    "temperature": 0,
                    "seed": 1,
                },
            )
            data = response.json()
            message = ((data.get("choices") or [{}])[0]).get("message") or {}
            calls = message.get("tool_calls") or []
            print(f"--- conversation {index}: {len(calls)} tool call(s)")
            for call in calls:
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                outcome = repairer.repair(name, arguments)
                print(f"    {name}: {arguments[:200]}")
                print(
                    f"      ok={outcome.ok} changed={outcome.changed} "
                    f"text_changed={outcome.text_changed} error={outcome.error_class}"
                )
                for reason in outcome.reasons:
                    print(f"      reason: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
