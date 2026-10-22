"""C4 — LoopGuard and task budget.

A deterministic safety backstop, *not* a security sandbox (Plan.md §6 C4).

Signals:

* the same tool name + normalized arguments repeated N times consecutively with an
  identical output or a repeated error,
* an A-B-A-B oscillation between two call fingerprints,
* the same normalized error repeated N times,
* a hard task step limit or wall-clock limit.

Escalation is limited to appending a deterministic warning or returning a synthetic
tool result. The guard never changes temperature, seed, or model, never modifies tool
permissions, and never silently swallows a failure.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pocket_harness.hashing import hash_obj


class LoopSignal(str, Enum):
    """The kind of loop condition that was detected."""

    REPEATED_CALL = "repeated_call"
    OSCILLATION = "oscillation"
    REPEATED_ERROR = "repeated_error"
    STEP_LIMIT = "step_limit"
    WALL_CLOCK_LIMIT = "wall_clock_limit"


@dataclass(slots=True)
class LoopEvent:
    """One detected loop condition."""

    signal: LoopSignal
    detail: str
    step: int
    fingerprint: str | None = None
    action: str = "warn"

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal.value,
            "detail": self.detail,
            "step": self.step,
            "fingerprint": self.fingerprint,
            "action": self.action,
        }


@dataclass(slots=True)
class LoopState:
    """The observable state of the guard for one task."""

    step: int = 0
    started_at: float = field(default_factory=time.monotonic)
    events: list[LoopEvent] = field(default_factory=list)
    call_fingerprints: deque[str] = field(default_factory=lambda: deque(maxlen=64))
    error_fingerprints: deque[str] = field(default_factory=lambda: deque(maxlen=32))
    warned: bool = False

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at


def normalize_arguments(arguments_text: str) -> str:
    """Normalize a tool-call argument string for fingerprinting.

    Whitespace and key order must not create a false "different call", but neither may
    a semantically different call be collapsed into the same fingerprint.
    """
    import json

    if not arguments_text:
        return ""
    try:
        value = json.loads(arguments_text)
    except json.JSONDecodeError:
        return arguments_text.strip()
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def call_fingerprint(tool_name: str, arguments_text: str) -> str:
    """Return the fingerprint of a tool call."""
    return hash_obj({"tool": tool_name, "args": normalize_arguments(arguments_text)})


def error_fingerprint(error_text: str) -> str:
    """Return the fingerprint of a tool error."""
    import re

    # Strip volatile substrings (paths, numbers, timestamps, addresses) so that the
    # same underlying error is recognized across turns.
    normalized = re.sub(r"\d+", "#", error_text or "")
    normalized = re.sub(r"[0-9a-f]{8,}", "#", normalized)
    normalized = re.sub(r"[A-Za-z]:[\\/][^\s'\"]+", "<path>", normalized)
    return hash_obj(normalized.strip().lower())


class LoopGuard:
    """Deterministic loop / task-budget detector."""

    def __init__(
        self,
        repeated_call_threshold: int = 3,
        max_steps_per_task: int = 40,
        max_wall_seconds: int = 1800,
        detect_oscillation: bool = True,
    ) -> None:
        self.repeated_call_threshold = repeated_call_threshold
        self.max_steps_per_task = max_steps_per_task
        self.max_wall_seconds = max_wall_seconds
        self.detect_oscillation = detect_oscillation

    def observe_call(self, state: LoopState, tool_name: str, arguments_text: str) -> list[LoopEvent]:
        """Record a tool call and return any newly detected loop signals."""
        state.step += 1
        events: list[LoopEvent] = []

        if state.step > self.max_steps_per_task:
            event = LoopEvent(
                signal=LoopSignal.STEP_LIMIT,
                detail=f"task exceeded the hard step limit ({self.max_steps_per_task})",
                step=state.step,
                action="stop",
            )
            events.append(event)
            state.events.append(event)
            return events

        elapsed = state.elapsed
        if elapsed > self.max_wall_seconds:
            event = LoopEvent(
                signal=LoopSignal.WALL_CLOCK_LIMIT,
                detail=f"task exceeded the wall-clock limit ({self.max_wall_seconds}s)",
                step=state.step,
                action="stop",
            )
            events.append(event)
            state.events.append(event)
            return events

        fingerprint = call_fingerprint(tool_name, arguments_text)
        state.call_fingerprints.append(fingerprint)

        threshold = max(2, self.repeated_call_threshold)
        if len(state.call_fingerprints) >= threshold:
            recent = list(state.call_fingerprints)[-threshold:]
            if len(set(recent)) == 1:
                event = LoopEvent(
                    signal=LoopSignal.REPEATED_CALL,
                    detail=(
                        f"the same tool call was repeated {threshold} times consecutively "
                        f"(tool={tool_name})"
                    ),
                    step=state.step,
                    fingerprint=fingerprint,
                    action="warn",
                )
                events.append(event)
                state.events.append(event)

        # The oscillation window is independent of the repeat threshold: it always
        # looks at the last four fingerprints so that an A-B-A-B pattern is caught
        # even when the repeat threshold is larger than four.
        if self.detect_oscillation and len(state.call_fingerprints) >= 4:
            pattern = list(state.call_fingerprints)[-4:]
            if pattern[0] == pattern[2] and pattern[1] == pattern[3]:
                event = LoopEvent(
                    signal=LoopSignal.OSCILLATION,
                    detail="an A-B-A-B oscillation between two call fingerprints was detected",
                    step=state.step,
                    fingerprint=fingerprint,
                    action="warn",
                )
                events.append(event)
                state.events.append(event)

        return events

    def observe_error(self, state: LoopState, error_text: str) -> list[LoopEvent]:
        """Record a tool error and return any newly detected loop signals."""
        fingerprint = error_fingerprint(error_text)
        state.error_fingerprints.append(fingerprint)
        threshold = max(2, self.repeated_call_threshold)
        events: list[LoopEvent] = []
        if len(state.error_fingerprints) >= threshold:
            recent = list(state.error_fingerprints)[-threshold:]
            if len(set(recent)) == 1:
                event = LoopEvent(
                    signal=LoopSignal.REPEATED_ERROR,
                    detail=f"the same error was repeated {threshold} times consecutively",
                    step=state.step,
                    fingerprint=fingerprint,
                    action="warn",
                )
                events.append(event)
                state.events.append(event)
        return events

    def warning_text(self, events: list[LoopEvent]) -> str:
        """Return the deterministic warning to append to the next request."""
        if not events:
            return ""
        parts = [f"[pocket:C4] {event.detail}" for event in events]
        parts.append(
            "[pocket:C4] Change the approach or stop; repeating the identical call "
            "will not make progress."
        )
        return "\n".join(parts)
