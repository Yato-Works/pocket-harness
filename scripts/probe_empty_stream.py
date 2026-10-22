"""Stream the raw generation of a request that came back empty.

The model generated 65 tokens that ended up in neither ``content`` nor
``tool_calls``. Streaming the same request shows what those tokens were.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

NATIVE = "http://127.0.0.1:11434/api/chat"


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else ".e2e/empty-probe/failing-request.json")
    body = json.loads(path.read_text(encoding="utf-8"))
    model = body.get("model", "qwen2.5:7b-instruct")

    messages = []
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
        messages.append(entry)

    chunks: list[str] = []
    tool_chunks: list[str] = []
    with httpx.Client(timeout=300.0) as client:
        with client.stream(
            "POST",
            NATIVE,
            json={
                "model": model,
                "messages": messages,
                "tools": body.get("tools") or [],
                "stream": True,
                "options": {"temperature": 0, "seed": body.get("seed", 1)},
            },
        ) as response:
            for line in response.iter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                message = data.get("message") or {}
                if message.get("content"):
                    chunks.append(message["content"])
                for call in message.get("tool_calls") or []:
                    tool_chunks.append(json.dumps(call.get("function") or {}))
                if data.get("done"):
                    print("done_reason:", data.get("done_reason"))
                    print("eval_count:", data.get("eval_count"))
    print("--- streamed content chunks ---")
    print(repr("".join(chunks))[:1500])
    print("--- streamed tool call chunks ---")
    print(repr("".join(tool_chunks))[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
