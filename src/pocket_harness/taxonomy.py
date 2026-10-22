"""The failure taxonomy of Plan.md §3, and the classification of a run.

The taxonomy is the operational part of the evaluation plan: the gates in
§11.5 are decided on the *distribution* of these classes, so the classification
must be derived from the recorded trace and not from an opinion about the
model.

===== ============================ ==================================================
Code  Name                         Operational definition used here
===== ============================ ==================================================
``O`` context overflow            The request exceeded the configured budget, or the
                                   upstream rejected it as too long, or the proxy
                                   answered with a context error.
``M`` malformed tool call         A tool call could not be parsed or did not
                                   validate against the client's schema.
``L`` loop / repetition           A deterministic repeated-call or repeated-error
                                   rule fired.
``S`` stopped before completion   The step, wall-clock or task budget was exhausted.
``W`` wrong semantic edit         The task failed and none of O/M/L/S/R explains it.
``R`` upstream rejection          Rate limit, quota, authentication, unsupported
                                   capability, provider outage or transport error.
===== ============================ ==================================================

``R`` is deliberately kept apart from the model-quality classes: an upstream
that is unavailable must not be counted as a model failure (Plan.md §3).

A run may exhibit more than one signal. ``primary_class`` applies a documented
precedence so that a run contributes to exactly one denominator, while
``signals`` keeps every observed signal for diagnosis.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

#: The classes of Plan.md §3.
CLASSES = ("O", "M", "L", "S", "W", "R")

#: Classes that count as "attributable to a proxy-addressable cause" for G0.
ADDRESSABLE = ("O", "M", "L", "S")

#: Precedence used to pick the primary class of a run.
#: R wins because it makes the run ineligible for the efficacy denominator;
#: then O, M, L, S are the causes the proxy can act on; W is the residual.
PRECEDENCE = ("R", "O", "M", "L", "S", "W")

#: Human readable names, used in reports.
CLASS_NAMES = {
    "O": "context_overflow",
    "M": "malformed_tool_call",
    "L": "loop",
    "S": "stopped_before_completion",
    "W": "wrong_semantic_edit",
    "R": "upstream_rejection",
}

#: HTTP status codes that mean "the upstream refused the request" (class R).
_UPSTREAM_REJECTION_STATUSES = frozenset({401, 402, 403, 408, 429, 500, 502, 503, 504, 529})

#: Substrings that identify a context-length rejection in an upstream error.
_CONTEXT_ERROR_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
    "context window",
)


def is_context_error(message: str) -> bool:
    """Return whether an upstream error message describes a context overflow."""
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _CONTEXT_ERROR_MARKERS)


@dataclass
class Classification:
    """The outcome classification of a single run."""

    primary_class: str
    signals: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    eligible: bool = True

    @property
    def name(self) -> str:
        return CLASS_NAMES.get(self.primary_class, self.primary_class)

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_class": self.primary_class,
            "primary_name": self.name,
            "signals": list(self.signals),
            "reasons": list(self.reasons),
            "eligible": self.eligible,
        }


def _add(signals: list[str], reasons: list[str], code: str, reason: str) -> None:
    if code not in signals:
        signals.append(code)
    reasons.append(reason)


def classify_run(
    *,
    task_passed: bool,
    run_completed: bool,
    upstream_error: str | None = None,
    upstream_status: int | None = None,
    context_overflow: bool = False,
    invalid_tool_calls: int = 0,
    loop_events: int = 0,
    budget_exhausted: bool = False,
    step_limit: int | None = None,
    wall_limit: bool = False,
    tool_call_delivered_as_text: bool = False,
    empty_completion: bool = False,
    extra_signals: Iterable[str] = (),
) -> Classification:
    """Classify one run from the facts the trace contains.

    ``upstream_error`` is the error text of a failed upstream call (if any),
    ``upstream_status`` its HTTP status. A run that never reached the upstream
    is not class R.

    ``tool_call_delivered_as_text`` means the upstream answered with a tool
    call written into the message text instead of the structured field. That is
    a malformed tool call as delivered (M). ``empty_completion`` means the
    upstream returned nothing usable, which is an upstream failure (R) and must
    not be counted as a model mistake.
    """
    signals: list[str] = []
    reasons: list[str] = []

    if upstream_error is not None:
        if context_overflow or is_context_error(upstream_error):
            _add(signals, reasons, "O", f"the upstream rejected the request: {upstream_error}")
        elif upstream_status is not None and upstream_status in _UPSTREAM_REJECTION_STATUSES:
            _add(signals, reasons, "R", f"the upstream rejected the request (HTTP {upstream_status})")
        else:
            _add(signals, reasons, "R", f"the upstream request failed: {upstream_error}")
    elif context_overflow:
        _add(signals, reasons, "O", "the request did not fit the configured context window")

    if tool_call_delivered_as_text:
        _add(
            signals,
            reasons,
            "M",
            "the upstream delivered a tool call as text instead of a structured tool call",
        )
    if empty_completion:
        _add(signals, reasons, "R", "the upstream returned an empty completion")

    if invalid_tool_calls > 0:
        _add(
            signals,
            reasons,
            "M",
            f"{invalid_tool_calls} tool call(s) did not validate against the client's schema",
        )
    if loop_events > 0:
        _add(signals, reasons, "L", f"the loop guard fired {loop_events} time(s)")
    if budget_exhausted:
        if step_limit:
            detail = "the step limit"
        elif wall_limit:
            detail = "the wall-clock limit"
        else:
            detail = "the task budget"
        _add(signals, reasons, "S", f"the run stopped because {detail} was exhausted")

    for code in extra_signals:
        if code in CLASSES and code not in signals:
            signals.append(code)
            reasons.append(f"the run reported the {code} signal")

    if not task_passed and not signals:
        _add(
            signals,
            reasons,
            "W",
            "the task failed and no proxy-addressable cause was recorded",
        )

    primary = "W"
    for code in PRECEDENCE:
        if code in signals:
            primary = code
            break
    if not signals or task_passed:
        # The task passed: there is no failure class. Observed signals are
        # still reported, but a successful run is not a failure.
        primary = ""

    eligible = "R" not in signals or task_passed
    return Classification(
        primary_class=primary,
        signals=signals,
        reasons=reasons,
        eligible=eligible,
    )


def tally(classifications: Iterable[Classification]) -> dict[str, int]:
    """Count the primary classes."""
    counts = {code: 0 for code in CLASSES}
    for item in classifications:
        if item.primary_class:
            counts[item.primary_class] = counts.get(item.primary_class, 0) + 1
    return counts


def signal_tally(classifications: Iterable[Classification]) -> dict[str, int]:
    """Count every observed signal (a run may contribute to several)."""
    counts = {code: 0 for code in CLASSES}
    for item in classifications:
        for code in item.signals:
            counts[code] = counts.get(code, 0) + 1
    return counts


__all__ = [
    "ADDRESSABLE",
    "CLASSES",
    "CLASS_NAMES",
    "Classification",
    "PRECEDENCE",
    "classify_run",
    "is_context_error",
    "signal_tally",
    "tally",
]
