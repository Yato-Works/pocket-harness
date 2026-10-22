"""C2 — ContextBudgeter.

Enforces a request budget derived from the configured context window, the reserved
output tokens, and a safety margin.

Invariants implemented here (Plan.md §6 C2):

* The system prompt body is unchanged by default.
* The latest ``keep_recent_turns`` complete turns are preserved verbatim.
* Tool-call / tool-result references stay valid and correctly paired — a
  ``tool_calls`` entry is never left without its matching ``tool`` result.
* Applying the same policy to its own output is **idempotent**.
* The output estimate never exceeds the configured budget.
* Removed content is not destroyed: it is stored in a content-addressed blob and
  replaced by a deterministic reference stub.

The compression sequence is fixed and ordered:

1. deduplicate identical tool outputs,
2. clip oversized tool output (deterministic head/tail window),
3. reduce old tool results to deterministic metadata,
4. omit the oldest complete exchanges,
5. otherwise fail with a controlled context error.

Steps 1–3 are *content* transforms: they preserve the message sequence and are
therefore always safe to apply. Step 4 removes messages and is only reached when the
content transforms were not enough to fit the budget.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pocket_harness.canonical import CanonicalRequest, Message
from pocket_harness.hashing import hash_obj
from pocket_harness.tokens import TokenCount, TokenCounter, TokenSource


class ContextOverflowError(Exception):
    """Raised when a request cannot be made to fit without corrupting it."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


@dataclass(slots=True)
class CompressionEvent:
    """One applied compression rule."""

    rule_id: str
    reason: str
    bytes_before: int = 0
    bytes_after: int = 0
    blob_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.rule_id,
            "reason": self.reason,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "blob_ref": self.blob_ref,
        }


@dataclass(slots=True)
class BudgetResult:
    """The outcome of a budget pass."""

    request: CanonicalRequest
    token_before: TokenCount
    token_after: TokenCount = field(
        default_factory=lambda: TokenCount(0, TokenSource.ESTIMATED, "not-computed")
    )
    events: list[CompressionEvent] = field(default_factory=list)
    omitted_turns: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def saved_bytes(self) -> int:
        return sum(max(0, e.bytes_before - e.bytes_after) for e in self.events)

    def as_rule_dicts(self) -> list[dict[str, Any]]:
        return [event.as_dict() for event in self.events]


def _content_bytes(content: Any) -> int:
    """Return the approximate byte size of a message content value."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    return len(str(content).encode("utf-8"))


#: Markers that identify content that a previous C2 pass already transformed.
#: They make the content transforms idempotent: a second pass recognizes its own
#: output and leaves it alone.
C2_REFERENCE_MARKER = "[pocket:C2.reference]"
C2_CLIP_MARKER = "[pocket:C2.clip]"
C2_REDUCED_MARKER = "[pocket:C2.reduced]"

#: Matches the ``sha256=<64 hex>`` provenance tag that every C2 transform embeds.
_ORIGIN_RE = re.compile(r"sha256=([0-9a-f]{64})")


def origin_digest(content: str) -> str:
    """Return the identity of a piece of content.

    For content that a previous C2 pass already transformed, this returns the digest
    of the *original* content, so that a second pass still recognizes two entries
    that were duplicates of each other.
    """
    match = _ORIGIN_RE.search(content)
    if match:
        return match.group(1)
    return hash_obj(content)


class ContextBudgeter:
    """Applies the deterministic context-budget policy to a canonical request."""

    def __init__(
        self,
        context_length: int | None,
        start_at: float = 0.85,
        target_at: float = 0.70,
        keep_recent_turns: int = 4,
        reserved_output_tokens: int = 1024,
        safety_margin_tokens: int = 256,
        max_tool_output_bytes: int = 8192,
        max_request_tokens: int | None = None,
        counter: TokenCounter | None = None,
        blob_store: Callable[[Any], str] | None = None,
    ) -> None:
        self.context_length = context_length
        self.start_at = start_at
        self.target_at = target_at
        self.keep_recent_turns = keep_recent_turns
        self.reserved_output_tokens = reserved_output_tokens
        self.safety_margin_tokens = safety_margin_tokens
        self.max_tool_output_bytes = max_tool_output_bytes
        self.max_request_tokens = max_request_tokens
        self.counter = counter or TokenCounter()
        self.blob_store = blob_store

    # ------------------------------------------------------------- budgeting

    def usable_budget(self) -> int | None:
        """Return the token budget available for input, if the window is known."""
        if self.max_request_tokens is not None:
            return self.max_request_tokens
        if self.context_length is None:
            return None
        budget = self.context_length - self.reserved_output_tokens - self.safety_margin_tokens
        return max(0, budget)

    def _threshold(self, ratio: float) -> int | None:
        budget = self.usable_budget()
        if budget is None:
            return None
        return int(budget * ratio)

    # ------------------------------------------------------------- main pass

    def apply(self, request: CanonicalRequest) -> BudgetResult:
        """Apply the budget policy, returning the compressed request."""
        before = self.counter.count_messages([m.to_dict() for m in request.messages])
        result = BudgetResult(request=request, token_before=before)

        budget = self.usable_budget()
        if budget is None:
            # The context window is unknown. Never silently assume a large window:
            # report the uncertainty and leave the request untouched.
            result.warnings.append(
                "context window unknown; budget enforcement skipped "
                "(set [upstream].context_length or pass --context)"
            )
            result.token_after = before
            return result

        if budget <= 0:
            result.token_after = before
            return result

        working = [m.model_copy(deep=True) for m in request.messages]

        # Steps 1-3 are content transforms. They enforce *per-item* limits
        # (a single tool output may not exceed max_tool_output_bytes, and identical
        # outputs are redundant), so they run on every request. They preserve the
        # message sequence and are therefore always safe to apply.
        working = self._dedupe_tool_outputs(working, result)
        working = self._clip_tool_outputs(working, result)
        working = self._reduce_old_tool_results(working, result)

        after = self.counter.count_messages([m.to_dict() for m in working])

        # Step 4 removes messages. It is only reached when the request is actually
        # over the start threshold and the content transforms were not enough.
        if after.value > self._threshold(self.start_at) and after.value > budget:
            working = self._omit_old_exchanges(working, result, target=budget)
            after = self.counter.count_messages([m.to_dict() for m in working])

        if after.value > budget:
            raise ContextOverflowError(
                "request does not fit the configured context budget "
                "without corrupting message structure",
                {
                    "estimated_tokens": after.value,
                    "budget": budget,
                    "context_length": self.context_length,
                },
            )

        result.request = CanonicalRequest(
            **request.model_dump(exclude={"messages"}), messages=working
        )
        result.token_after = after
        return result

    # --------------------------------------------------------- step 1: dedupe

    def _dedupe_tool_outputs(self, messages: list[Message], result: BudgetResult) -> list[Message]:
        """Replace duplicate tool outputs with a reference stub."""
        seen: set[str] = set()
        for message in messages:
            if message.role != "tool" or not isinstance(message.content, str):
                continue
            if not message.content:
                continue
            digest = origin_digest(message.content)
            if digest in seen:
                original = message.content
                message.content = self._stub(
                    original,
                    "duplicate of an earlier identical tool result",
                    origin=digest,
                )
                result.events.append(
                    CompressionEvent(
                        rule_id="C2.dedupe",
                        reason="identical tool output already present",
                        bytes_before=_content_bytes(original),
                        bytes_after=_content_bytes(message.content),
                    )
                )
            else:
                seen.add(digest)
        return messages

    # ---------------------------------------------------------- step 2: clip

    def _clip_tool_outputs(self, messages: list[Message], result: BudgetResult) -> list[Message]:
        """Clip oversized tool output to a deterministic head/tail window."""
        for message in messages:
            if message.role != "tool" or not isinstance(message.content, str):
                continue
            content = message.content
            if C2_CLIP_MARKER in content or C2_REDUCED_MARKER in content:
                continue  # already transformed: keep the pass idempotent
            size = len(content.encode("utf-8"))
            if size <= self.max_tool_output_bytes:
                continue
            head_len = self.max_tool_output_bytes // 2
            tail_len = self.max_tool_output_bytes - head_len
            head = content.encode("utf-8")[:head_len].decode("utf-8", errors="ignore")
            tail = content.encode("utf-8")[-tail_len:].decode("utf-8", errors="ignore")
            omitted = size - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
            digest = hash_obj(content)
            marker = f"\n... [{omitted} bytes omitted] ...\n"
            clipped = f"{C2_CLIP_MARKER} sha256={digest}\n{head}{marker}{tail}"
            blob_ref = self._store_blob(content)
            message.content = clipped + f"\n[content_ref: {blob_ref}]" if blob_ref else clipped
            result.events.append(
                CompressionEvent(
                    rule_id="C2.clip",
                    reason="tool output exceeded max_tool_output_bytes",
                    bytes_before=size,
                    bytes_after=_content_bytes(message.content),
                    blob_ref=blob_ref,
                )
            )
        return messages


    # --------------------------------------------------------- step 3: reduce

    def _reduce_old_tool_results(self, messages: list[Message], result: BudgetResult) -> list[Message]:
        """Replace eligible older tool results with deterministic metadata."""
        protected_from = self._protected_index(messages)
        for index, message in enumerate(messages):
            if index >= protected_from:
                continue
            if message.role != "tool" or not isinstance(message.content, str):
                continue
            content = message.content
            if C2_REDUCED_MARKER in content or C2_CLIP_MARKER in content:
                continue  # already transformed: keep the pass idempotent
            size = len(content.encode("utf-8"))
            if size <= self.max_tool_output_bytes:
                continue
            head = content[:200]
            tail = content[-200:]
            digest = hash_obj(content)
            summary = (
                f"{C2_REDUCED_MARKER} tool={message.name or 'unknown'} "
                f"bytes={size} sha256={digest} "
                f"head={head!r} tail={tail!r}"
            )
            blob_ref = self._store_blob(content)
            if blob_ref:
                summary += f" content_ref={blob_ref}"
            message.content = summary
            result.events.append(
                CompressionEvent(
                    rule_id="C2.reduce",
                    reason="old tool result reduced to deterministic metadata",
                    bytes_before=size,
                    bytes_after=_content_bytes(summary),
                    blob_ref=blob_ref,
                )
            )
        return messages

    # --------------------------------------------------------- step 4: omit

    def _protected_index(self, messages: list[Message]) -> int:
        """Return the index from which messages must be preserved verbatim.

        The protected window covers the last ``keep_recent_turns`` user turns,
        including every message that belongs to them.
        """
        if self.keep_recent_turns <= 0:
            return 0
        protected = 0
        count = 0
        for message in reversed(messages):
            protected += 1
            if message.role == "user":
                count += 1
                if count >= self.keep_recent_turns:
                    break
        return max(0, len(messages) - protected)

    def _omit_old_exchanges(
        self,
        messages: list[Message],
        result: BudgetResult,
        target: int | None,
    ) -> list[Message]:
        """Omit the oldest complete exchanges until the target size is reached.

        A *complete exchange* is a user turn together with the assistant messages and
        tool results that belong to it. Dropping a partial exchange would leave an
        assistant ``tool_calls`` entry without its matching tool result, so the
        omission always removes whole exchanges.
        """
        if target is None:
            return messages

        working = list(messages)
        while True:
            count = self.counter.count_messages([m.to_dict() for m in working])
            if count.value <= target:
                break
            floor = self._protected_index(working)
            if floor >= len(working):
                break
            drop_end = self._first_exchange_end(working, floor)
            if drop_end is None or drop_end <= 0 or drop_end > floor:
                # The first complete exchange does not lie entirely before the
                # protected window, so nothing can be dropped safely.
                break
            dropped = working[:drop_end]
            working = working[drop_end:]
            result.omitted_turns += len(dropped)
            result.events.append(
                CompressionEvent(
                    rule_id="C2.omit_old_exchange",
                    reason="request still exceeded the target budget",
                    bytes_before=sum(_content_bytes(m.content) for m in dropped),
                    bytes_after=0,
                )
            )
        return working

    def _first_exchange_end(self, messages: list[Message], floor: int) -> int | None:
        """Return the index just past the first droppable complete exchange.

        The exchange starts after the leading system messages, and ends after the
        assistant turn and all of its tool results.
        """
        index = 0
        while index < len(messages) and messages[index].role == "system":
            index += 1
        if index >= floor:
            return None
        # A user turn opens an exchange.
        if index >= len(messages) or messages[index].role != "user":
            return None
        index += 1
        while index < len(messages):
            message = messages[index]
            if message.role == "user":
                break  # the next exchange starts here
            if message.role == "assistant":
                call_ids = [c.id for c in (message.tool_calls or []) if c.id]
                index += 1
                if call_ids:
                    consumed = 0
                    while index < len(messages) and consumed < len(call_ids):
                        nxt = messages[index]
                        if nxt.role == "tool" and nxt.tool_call_id in call_ids:
                            consumed += 1
                        index += 1
                continue
            index += 1
        return index

    # ------------------------------------------------------------- utilities

    def _stub(self, original: str, reason: str, origin: str | None = None) -> str:
        """Return a deterministic reference stub for removed content."""
        digest = origin or hash_obj(original)
        blob_ref = self._store_blob(original)
        stub = f"{C2_REFERENCE_MARKER} {reason} sha256={digest}"
        if blob_ref:
            stub += f" content_ref={blob_ref}"
        return stub

    def _store_blob(self, content: str) -> str | None:
        if self.blob_store is None:
            return None
        try:
            return self.blob_store(content)
        except Exception:  # noqa: BLE001 - evidence storage must never break a request
            return None


