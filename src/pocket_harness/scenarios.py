"""Deterministic mechanism scenarios — the diagnostic half of the evaluation.

The task benchmark measures *completion* (track B). It cannot say whether C1,
C2, C3 or C4 worked, because their conditions may simply not occur in the
tasks the model happens to attempt.

These scenarios create each condition **on purpose**, with a scripted upstream
and a scripted client, and check the mechanism's obligation:

``c1-tool-cap``       the client sends more tools than the cap
``c2-context-budget`` the conversation does not fit the budget
``c3-repairable``     the upstream returns a call with broken arguments
``c3-unrepairable``   the upstream returns a call with missing arguments
``c4-repeat``         the client repeats the same call
``c4-oscillation``    the client alternates between two calls
``client-error``      the client answers an unusable call (client obligation)
``overhead``          the proxy's own transformation cost

No model and no network are involved, so the result is reproducible by
construction. A scenario that passes says nothing about task completion; it
says that the mechanism did its job when its condition arose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pocket_harness.config import Config
from pocket_harness.mechanism_eval import MechanismObligation

MAX_TOKENS = 64


def _tool(name: str, description: str = "a tool") -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }


def _chat_body(
    content: str = "ok", tool_calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "m",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def _call(name: str, arguments: str, call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


@dataclass
class Scenario:
    """One deterministic scenario."""

    name: str
    mechanism: str
    condition: str
    obligation: str
    #: The client request.
    payload: dict[str, Any]
    #: What the scripted upstream answers.
    upstream: dict[str, Any]
    #: How the proxy must be configured for the scenario.
    config_overrides: dict[str, Any] = field(default_factory=dict)
    #: What the scenario expects the proxy to have done.
    expect: dict[str, Any] = field(default_factory=dict)


def _many_tools_payload(tool_count: int = 12) -> dict[str, Any]:
    return {
        "model": "m",
        "messages": [{"role": "user", "content": "read the file"}],
        "tools": [_tool(f"tool_{index}") for index in range(tool_count)],
    }


def _long_conversation(
    old_turns: int = 3, recent_turns: int = 4, size: int = 4000
) -> list[dict[str, Any]]:
    """A conversation that is over the budget but still compressible.

    The *old* turns are huge -- those are what C2 has to remove -- and the
    recent turns are small, because C2 must keep them verbatim.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYSTEM PROMPT"}
    ]
    for index in range(old_turns):
        messages.append({"role": "user", "content": f"old question {index} " + "x" * size})
        messages.append(
            {
                "role": "assistant",
                "content": "old answer " + "y" * size,
                "tool_calls": [_call("read_file", json.dumps({"path": f"old{index}.py"}))],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": "z" * size,
            }
        )
    for index in range(recent_turns):
        messages.append({"role": "user", "content": f"recent question {index}"})
        messages.append(
            {
                "role": "assistant",
                "content": "recent answer",
                "tool_calls": [_call("read_file", json.dumps({"path": f"new{index}.py"}))],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": "small result",
            }
        )
    return messages


def _repeated_history(count: int = 3) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "read the file"},
    ]
    for index in range(count):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_call("read_file", json.dumps({"path": "a.py"}), f"c{index}")],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"c{index}",
                "name": "read_file",
                "content": "contents",
            }
        )
    return messages


def _oscillation_history() -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "read the file"},
    ]
    for index, name in enumerate(["read_file", "list_files", "read_file", "list_files"]):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_call(name, json.dumps({"path": "a.py"}), f"c{index}")],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"c{index}",
                "name": name,
                "content": "contents",
            }
        )
    return messages


def build_scenarios() -> list[Scenario]:
    """Return the deterministic scenario set."""
    return [
        Scenario(
            name="c1-tool-cap",
            mechanism="C1",
            condition="the client sends 12 tools and the cap is 6",
            obligation="at most 6 tools are forwarded and the rule is recorded",
            payload=_many_tools_payload(12),
            upstream=_chat_body("ok"),
            config_overrides={"tools": {"max_non_pinned": 6}},
            expect={"rule_prefixes": ["C1.select"], "max_forwarded_tools": 6},
        ),
        Scenario(
            name="c2-context-budget",
            mechanism="C2",
            condition="the conversation does not fit the configured budget",
            obligation=(
                "the request is compressed, the system prompt is unchanged and the "
                "tool-call/result pairs stay valid"
            ),
            payload={
                "model": "m",
                "messages": _long_conversation(),
                "tools": [_tool("read_file")],
            },
            upstream=_chat_body("ok"),
            config_overrides={"upstream": {"context_length": 4096}},
            expect={"rule_prefixes": ["C2"], "system_prompt_unchanged": True, "pairs_valid": True},
        ),
        Scenario(
            name="c3-repairable",
            mechanism="C3",
            condition="the upstream returns a call with a fenced, trailing-comma argument",
            obligation="the call is repaired and the client receives the repaired arguments",
            payload={
                "model": "m",
                "messages": [{"role": "user", "content": "read a.py"}],
                "tools": [_tool("read_file")],
            },
            upstream=_chat_body(
                tool_calls=[_call("read_file", '```json\n{"path": "a.py",}\n```')]
            ),
            expect={"repaired": 1, "client_arguments": {"path": "a.py"}},
        ),
        Scenario(
            name="c3-unrepairable",
            mechanism="C3",
            condition="the upstream returns a call with no arguments at all",
            obligation=(
                "the call is reported as a failure and passed through unchanged; nothing "
                "is invented"
            ),
            payload={
                "model": "m",
                "messages": [{"role": "user", "content": "read a.py"}],
                "tools": [_tool("read_file")],
            },
            upstream=_chat_body(tool_calls=[_call("read_file", "{}")]),
            expect={"failed": 1, "repaired": 0, "client_arguments": {}},
        ),
        Scenario(
            name="c4-repeat",
            mechanism="C4",
            condition="the request history contains the same call three times",
            obligation="the repetition is detected and the escalation reaches the model",
            payload={
                "model": "m",
                "messages": _repeated_history(3),
                "tools": [_tool("read_file")],
            },
            upstream=_chat_body("ok"),
            expect={"rule_prefixes": ["C4.repeat_warning"], "warning_in_request": True},
        ),
        Scenario(
            name="c4-oscillation",
            mechanism="C4",
            condition="the request history alternates between two calls",
            obligation="the oscillation is detected and the escalation reaches the model",
            payload={
                "model": "m",
                "messages": _oscillation_history(),
                "tools": [_tool("read_file"), _tool("list_files")],
            },
            upstream=_chat_body("ok"),
            expect={"rule_prefixes": ["C4.repeat_warning"], "warning_in_request": True},
        ),
    ]


def apply_overrides(config: Config, overrides: dict[str, Any]) -> Config:
    """Apply the scenario specific configuration."""
    for section, values in overrides.items():
        target = getattr(config, section, None)
        if target is None:
            continue
        for key, value in values.items():
            setattr(target, key, value)
    return config


__all__ = [
    "MechanismObligation",
    "Scenario",
    "apply_overrides",
    "build_scenarios",
]
