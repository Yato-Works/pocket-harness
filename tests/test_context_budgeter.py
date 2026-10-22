"""Tests for the C2 ContextBudgeter.

These cover the invariants that Plan.md §6 C2 calls out explicitly: the system prompt
is unchanged, recent turns are preserved, tool-call/result pairing stays valid, the
result is idempotent, and the output never exceeds the budget.
"""

from __future__ import annotations

from pocket_harness.canonical import CanonicalRequest, Message
from pocket_harness.components.context_budgeter import ContextBudgeter, ContextOverflowError


def make_budgeter(context_length: int = 1024, **kwargs) -> ContextBudgeter:
    """Return a budgeter with a small context window."""
    params = {
        "context_length": context_length,
        "reserved_output_tokens": 64,
        "safety_margin_tokens": 0,
        "max_tool_output_bytes": 256,
        "keep_recent_turns": 2,
    }
    params.update(kwargs)
    return ContextBudgeter(**params)


def user(text: str) -> Message:
    """Return a user message."""
    return Message(role="user", content=text)


def system(text: str) -> Message:
    """Return a system message."""
    return Message(role="system", content=text)


def test_no_compression_below_the_threshold() -> None:
    """A small request is left untouched."""
    budgeter = make_budgeter(context_length=8192)
    request = CanonicalRequest(model="m", messages=[user("hello")])
    result = budgeter.apply(request)
    assert result.events == []
    assert result.token_after.value == result.token_before.value


def test_unknown_context_window_is_reported() -> None:
    """An unknown context window produces a warning, not silent assumption."""
    budgeter = ContextBudgeter(context_length=None)
    request = CanonicalRequest(model="m", messages=[user("hello")])
    result = budgeter.apply(request)
    assert result.warnings
    assert "context window unknown" in result.warnings[0]


def test_system_prompt_is_unchanged() -> None:
    """The system prompt body survives compression verbatim."""
    budgeter = make_budgeter(context_length=2048, keep_recent_turns=1)
    messages = [system("SYSTEM PROMPT " * 40)]
    for i in range(10):
        messages.append(user(f"message {i} " + "x" * 200))
    request = CanonicalRequest(model="m", messages=messages)
    result = budgeter.apply(request)
    assert result.request.messages[0].role == "system"
    assert result.request.messages[0].content == messages[0].content


def test_the_system_prompt_survives_the_omission_of_old_exchanges() -> None:
    """Plan.md §6 C2: the system prompt must never be silently deleted.

    The omission path is only reached when clipping and dedup were not enough,
    so the test forces it: every turn is far too large to fit.
    """
    budgeter = make_budgeter(context_length=2048, keep_recent_turns=1)
    messages = [system("SYSTEM PROMPT")]
    for index in range(6):
        messages.append(user(f"question {index} " + "x" * 1500))
        messages.append(Message(role="assistant", content="answer " + "y" * 1500))
    request = CanonicalRequest(model="m", messages=messages)
    result = budgeter.apply(request)
    assert any(event.rule_id == "C2.omit_old_exchange" for event in result.events)
    assert result.request.messages[0].role == "system"
    assert result.request.messages[0].content == "SYSTEM PROMPT"


def test_result_stays_within_budget() -> None:
    """The output never exceeds the usable budget."""
    budgeter = make_budgeter(context_length=2048, keep_recent_turns=1)
    request = CanonicalRequest(model="m", messages=[user("x" * 5000)])
    result = budgeter.apply(request)
    assert result.token_after.value <= budgeter.usable_budget()


def test_budget_is_idempotent() -> None:
    """Applying the policy to its own output changes nothing further."""
    budgeter = make_budgeter(context_length=2048, keep_recent_turns=1)
    messages = [user("x" * 5000), user("y" * 3000), user("z" * 100)]
    request = CanonicalRequest(model="m", messages=messages)
    first = budgeter.apply(request)
    second = budgeter.apply(first.request)
    assert second.token_after.value == first.token_after.value
    assert second.events == []



def test_recent_turns_are_preserved() -> None:
    """The most recent user turns survive compression."""
    budgeter = make_budgeter(context_length=2048, keep_recent_turns=2)
    messages = [user("old " + "x" * 8000), user("recent one"), user("recent two")]
    request = CanonicalRequest(model="m", messages=messages)
    result = budgeter.apply(request)
    contents = [m.content for m in result.request.messages if m.role == "user"]
    assert "recent one" in contents
    assert "recent two" in contents


def test_tool_call_result_pairing_is_preserved() -> None:
    """A tool result is never dropped while its tool call remains."""
    budgeter = make_budgeter(context_length=2048, keep_recent_turns=1)
    messages = [
        user("start"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                {"id": "call-1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
            ],
        ),
        Message(role="tool", tool_call_id="call-1", content="result body " * 100),
        user("next"),
    ]
    request = CanonicalRequest(model="m", messages=messages)
    result = budgeter.apply(request)
    call_ids = {c.id for m in result.request.messages for c in (m.tool_calls or []) if c.id}
    result_ids = {m.tool_call_id for m in result.request.messages if m.role == "tool"}
    assert call_ids == result_ids


def test_duplicate_tool_output_is_deduplicated() -> None:
    """Identical tool outputs are collapsed into a reference stub."""
    budgeter = make_budgeter(context_length=2048, max_tool_output_bytes=8192, keep_recent_turns=1)
    body = "identical output " * 50
    messages = [
        user("go"),
        Message(role="tool", tool_call_id="c1", content=body),
        Message(role="tool", tool_call_id="c2", content=body),
    ]
    request = CanonicalRequest(model="m", messages=messages)
    result = budgeter.apply(request)
    rule_ids = [e.rule_id for e in result.events]
    assert "C2.dedupe" in rule_ids
    assert result.request.messages[2].content != body
    assert "C2.reference" in result.request.messages[2].content


def test_oversized_tool_output_is_clipped() -> None:
    """An oversized tool output is clipped to the configured limit."""
    budgeter = make_budgeter(context_length=2048, max_tool_output_bytes=256, keep_recent_turns=1)
    body = "A" * 5000
    messages = [user("go"), Message(role="tool", tool_call_id="c1", content=body)]
    request = CanonicalRequest(model="m", messages=messages)
    result = budgeter.apply(request)
    clipped = result.request.messages[1].content
    assert len(clipped.encode("utf-8")) < len(body.encode("utf-8"))
    assert "bytes omitted" in clipped


def test_overflow_raises_a_controlled_error() -> None:
    """An impossible request fails loudly instead of being corrupted."""
    budgeter = make_budgeter(context_length=200, keep_recent_turns=5)
    request = CanonicalRequest(model="m", messages=[user("x" * 100000)])
    try:
        result = budgeter.apply(request)
    except ContextOverflowError as exc:
        assert exc.details["budget"] >= 0
    else:
        assert result.token_after.value <= budgeter.usable_budget()


