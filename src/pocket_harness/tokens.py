"""Token counting with an explicit provenance label.

Plan.md §6 C2 requires that token counts always be labelled as **exact** or **estimated**,
and that the source of the count is recorded in the evidence event. A count without a
provenance label is not allowed to be used for a budget decision.

The resolution order is:

1. ``exact`` — the upstream's own tokenize/count endpoint, if the profile declares one.
2. ``exact`` — a locally installed tokenizer that matches the model, if one is configured.
3. ``estimated`` — a conservative heuristic, with the heuristic recorded in the label.

No network call is performed implicitly: step 1 is only used when the caller explicitly
provides a counting callback (the proxy can query it as part of an explicit `doctor`
style pre-flight, not inside the hot path).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")
#: A deliberately pessimistic ratio: code and CJK text routinely tokenize above 4 chars/token.
CHARS_PER_TOKEN = 3.0
#: Floor for the heuristic, so that tiny strings do not round down to zero tokens.
MIN_ESTIMATE = 1


class TokenSource(str, Enum):
    """Provenance of a token count."""

    EXACT = "exact"
    ESTIMATED = "estimated"


@dataclass(frozen=True, slots=True)
class TokenCount:
    """A token count plus its provenance.

    ``source`` and ``method`` must travel together: an estimate that is presented as
    exact would corrupt the context-budget decision (Plan.md §13 risk table).
    """

    value: int
    source: TokenSource
    method: str

    @property
    def is_exact(self) -> bool:
        return self.source is TokenSource.EXACT

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "source": self.source.value, "method": self.method}


class Tokenizer(Protocol):
    """Minimal tokenizer interface accepted by the budgeter."""

    def count(self, text: str) -> int: ...


def estimate_tokens(text: str) -> TokenCount:
    """Estimate the token count of ``text`` conservatively.

    The heuristic blends a whitespace-word estimate with a character-based estimate and
    takes the larger of the two, so it errs on the side of "the context is bigger than
    we think" — which is the safe direction for an overflow guard.
    """
    if not text:
        return TokenCount(0, TokenSource.ESTIMATED, "heuristic-empty")
    words = len(text.split())
    chars = len(text)
    by_chars = math.ceil(chars / CHARS_PER_TOKEN)
    estimate = max(words, by_chars, MIN_ESTIMATE)
    return TokenCount(estimate, TokenSource.ESTIMATED, f"heuristic(max(words,chars/{CHARS_PER_TOKEN:g}))")


class TokenCounter:
    """Resolves a token count using the configured resolution order."""

    def __init__(
        self,
        tokenizer: Tokenizer | None = None,
        tokenizer_method: str = "local-tokenizer",
        upstream_counter: Callable[[str], int] | None = None,
        upstream_method: str = "upstream-count-endpoint",
    ) -> None:
        self._tokenizer = tokenizer
        self._tokenizer_method = tokenizer_method
        self._upstream_counter = upstream_counter
        self._upstream_method = upstream_method

    def count_text(self, text: str) -> TokenCount:
        """Count tokens in a plain string."""
        if self._upstream_counter is not None:
            try:
                return TokenCount(int(self._upstream_counter(text)), TokenSource.EXACT, self._upstream_method)
            except Exception:  # noqa: BLE001 - fall through to the next source
                pass
        if self._tokenizer is not None:
            try:
                return TokenCount(int(self._tokenizer.count(text)), TokenSource.EXACT, self._tokenizer_method)
            except Exception:  # noqa: BLE001
                pass
        return estimate_tokens(text)

    def count_messages(self, messages: list[dict[str, Any]]) -> TokenCount:
        """Count the tokens of a message list.

        The per-message structural overhead of the chat format is included so that the
        estimate does not under-count by the number of messages.
        """
        parts: list[str] = []
        overhead = 0
        for message in messages:
            parts.append(_message_text(message))
            overhead += 4  # role + delimiters, per the chat format convention
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(str(part.get("text", "")))
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if isinstance(call, dict):
                        function = call.get("function") or {}
                        parts.append(str(function.get("name", "")))
                        parts.append(str(function.get("arguments", "")))
        if not parts:
            return TokenCount(0, TokenSource.ESTIMATED, "heuristic-empty")
        text = "\n".join(parts)
        base = self.count_text(text)
        total = base.value + overhead
        if base.is_exact:
            return TokenCount(total, TokenSource.EXACT, f"{base.method}+chat-overhead")
        return TokenCount(total, TokenSource.ESTIMATED, f"{base.method}+chat-overhead")


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return canonical_text(content)


def canonical_text(value: Any) -> str:
    """Render a JSON-compatible value as text for counting purposes."""
    from pocket_harness.hashing import canonical_json

    return canonical_json(value)
