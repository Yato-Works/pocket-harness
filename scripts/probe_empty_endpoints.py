"""Ask the native and the OpenAI-compatible endpoint the same captured request.

The benchmark loses half of its runs to empty answers. The OpenAI-compatible
answer carries an empty ``content`` while the usage reports that tokens were
generated. This probe sends the *captured* request to both endpoints to find
out whether the model produced nothing or the protocol layer dropped it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

NATIVE = "http://127.0.0.1:11434/api/chat"
OPENAI = "http://127.0.0.1:11434/v1"


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else ".e2e/empty-probe/failing-request.json")
    body = json.loads(path.read_text(encoding="utf-8"))
    model = body.get("model", "qwen2.5:7b-instruct")

    native_messages = []
    for message in body["messages"]:
        entry = {"role": message["role"], "content": message.get("content") or ""}
        if message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                arguments = call["function"].get("arguments") or "{}"
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                calls.append(
                    {
                        "function": {
                            "name": call["function"]["name"],
                            "arguments": arguments,
                        }
                    }
                )
            entry["tool_calls"] = calls
        native_messages.append(entry)

    with httpx.Client(timeout=300.0) as client:
        native = client.post(
            NATIVE,
            json={
                "model": model,
                "messages": native_messages,
                "tools": body.get("tools") or [],
                "stream": False,
                "options": {"temperature": body.get("temperature", 0), "seed": body.get("seed", 1)},
            },
        )
        ndata = native.json()
        nmessage = ndata.get("message") or {}
        print("=== native /api/chat ===")
        print("  status:", native.status_code)
        print("  done_reason:", ndata.get("done_reason"))
        print("  eval_count:", ndata.get("eval_count"))
        print("  content:", repr((nmessage.get("content") or "")[:400]))
        print("  tool_calls:", json.dumps(nmessage.get("tool_calls") or [])[:400])

        compat = client.post(
            f"{OPENAI}/chat/completions",
            json={**body, "model": model},
        )
        cdata = compat.json()
        cmessage = ((cdata.get("choices") or [{}])[0]).get("message") or {}
        print("\n=== openai /v1/chat/completions ===")
        print("  status:", compat.status_code)
        print("  usage:", json.dumps(cdata.get("usage")))
        print("  finish_reason:", (cdata.get("choices") or [{}])[0].get("finish_reason"))
        print("  content:", repr((cmessage.get("content") or "")[:400]))
        print("  tool_calls:", json.dumps(cmessage.get("tool_calls") or [])[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
