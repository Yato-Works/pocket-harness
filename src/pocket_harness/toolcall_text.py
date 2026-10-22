"""Detect a tool call that a model delivered as *text* instead of as a call.

Some local models answer a tool request with the call written into the
assistant message (``content``) instead of the structured ``tool_calls`` field
of the OpenAI-compatible response. The client then has nothing to execute and
the task silently makes no progress.

This module recognises that shape **deterministically** so that the failure can
be classified and reported. It is used for *evidence only*: nothing in this
module ever executes a tool call or rewrites a client response. Turning a
text-delivered call into an executable one would be guessing the model's
intent, which Plan.md §1.3 forbids.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Tags the observed models use around an embedded call.
_TAG_RE = re.compile(
    r"<\s*(tool_call|tool|function_call|tooluse|tool_use)\s*>(.*?)<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

#: The first balanced JSON object in a string.
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _looks_like_tool_call(value: Any) -> bool:
    """Return whether a decoded value has the shape of a tool call."""
    if not isinstance(value, dict):
        return False
    name = value.get("name")
    if not isinstance(name, str) or not name:
        return False
    return "arguments" in value


def find_text_tool_call(content: str | None) -> dict[str, Any] | None:
    """Return the tool call embedded in an assistant message, if there is one.

    The rules are narrow and deterministic:

    1. the message must contain a tool-call tag or a JSON object;
    2. the decoded value must have a non-empty string ``name`` and an
       ``arguments`` member;
    3. nothing else is accepted — free text that merely mentions a tool name
       is not a tool call.

    Returns ``None`` when no such call is present.
    """
    if not content:
        return None
    text = content.strip()
    if not text:
        return None

    candidates: list[str] = []
    for match in _TAG_RE.finditer(text):
        candidates.append(match.group(2).strip())
    candidates.append(text)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            object_match = _OBJECT_RE.search(candidate)
            if not object_match:
                continue
            try:
                decoded = json.loads(object_match.group(0))
            except json.JSONDecodeError:
                continue
        if _looks_like_tool_call(decoded):
            return decoded
    return None


def describe_text_tool_call(content: str | None) -> dict[str, Any] | None:
    """Return a redacted description of a text-delivered tool call."""
    call = find_text_tool_call(content)
    if call is None:
        return None
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"_unparsed": True}
    return {
        "name": call.get("name"),
        "argument_keys": sorted(arguments) if isinstance(arguments, dict) else [],
        "delivered": "text",
    }


__all__ = ["describe_text_tool_call", "find_text_tool_call"]
