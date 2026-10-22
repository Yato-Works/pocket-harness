"""Probe the real capability profile of a local upstream.

Plan.md §4.1 requires a *tested* capability profile rather than an assumption of
full OpenAI API parity. This script asks a real upstream for a tool call in several
ways and records exactly what came back, so the profile is evidence, not a guess.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

TOOL = {
    "type": "function",
    "function": {
        "name": "pocket_doctor_probe",
        "description": "Report the current time.",
        "parameters": {
            "type": "object",
            "properties": {"timezone": {"type": "string"}},
            "required": ["timezone"],
        },
    },
}

PROMPT = (
    "Call the pocket_doctor_probe tool with timezone set to UTC. "
    "Do not answer in text; only make the tool call."
)


def _post(url: str, body: dict[str, Any], timeout: float = 300.0) -> dict[str, Any]:
    """POST JSON to ``url`` and return the decoded response."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _describe(content: Any) -> str:
    """Return a short description of a message content field."""
    if content is None:
        return "None"
    text = str(content)
    return text[:160]


def probe_openai(base: str, model: str, repeats: int = 1) -> dict[str, Any]:
    """Probe the OpenAI-compatible endpoint for structured tool calls."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": [TOOL],
        "tool_choice": {"type": "function", "function": {"name": "pocket_doctor_probe"}},
        "max_tokens": 128,
    }
    result: dict[str, Any] = {"model": model, "endpoint": "openai"}
    structured = 0
    as_text = 0
    empty = 0
    errors = 0
    contents: list[str] = []
    for _ in range(max(1, repeats)):
        try:
            data = _post(f"{base}/v1/chat/completions", body)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            result.setdefault("error", f"{type(exc).__name__}: {exc}")
            continue
        message = (data.get("choices") or [{}])[0].get("message") or {}
        calls = message.get("tool_calls")
        content = message.get("content")
        if calls:
            structured += 1
            result["tool_calls"] = calls
        elif isinstance(content, str) and '"arguments"' in content:
            as_text += 1
        elif not (content or "").strip():
            empty += 1
        if len(contents) < 2:
            contents.append(_describe(content))
    result["repeats"] = max(1, repeats)
    result["structured_tool_calls"] = structured
    result["tool_call_as_text"] = as_text
    result["empty_completion"] = empty
    result["errors"] = errors
    result["structured_rate"] = round(structured / max(1, repeats), 3)
    result["content"] = contents[0] if contents else ""
    return result


def probe_native(base: str, model: str, repeats: int = 1) -> dict[str, Any]:
    """Probe Ollama's native endpoint for structured tool calls."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": [TOOL],
        "stream": False,
    }
    result: dict[str, Any] = {"model": model, "endpoint": "native"}
    structured = 0
    as_text = 0
    empty = 0
    errors = 0
    contents: list[str] = []
    for _ in range(max(1, repeats)):
        try:
            data = _post(f"{base}/api/chat", body)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            result.setdefault("error", f"{type(exc).__name__}: {exc}")
            continue
        message = data.get("message") or {}
        calls = message.get("tool_calls")
        content = message.get("content")
        if calls:
            structured += 1
            result["tool_calls"] = calls
        elif isinstance(content, str) and '"arguments"' in content:
            as_text += 1
        elif not (content or "").strip():
            empty += 1
        if len(contents) < 2:
            contents.append(_describe(content))
    result["repeats"] = max(1, repeats)
    result["structured_tool_calls"] = structured
    result["tool_call_as_text"] = as_text
    result["empty_completion"] = empty
    result["errors"] = errors
    result["structured_rate"] = round(structured / max(1, repeats), 3)
    result["content"] = contents[0] if contents else ""
    return result


def main() -> int:
    """Probe every model and print the results as JSON."""
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:11434"
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    try:
        models = json.load(urllib.request.urlopen(f"{base}/v1/models", timeout=10))["data"]
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1

    results = []
    for entry in models:
        name = entry["id"]
        results.append(probe_openai(base, name, repeats))
        results.append(probe_native(base, name, repeats))
        print(json.dumps(results[-2]), flush=True)
        print(json.dumps(results[-1]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
