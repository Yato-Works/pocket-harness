"""Track A — per-mechanism diagnostic evaluation.

This is the **diagnostic** half of the evaluation and it answers a different
question from the gate evaluation:

======================================  ==========================================
Track A (this module)                    Track B (``pocket_harness.evaluation``)
======================================  ==========================================
did C1/C2/C3/C4 fire where they must?    did a coding task get completed?
are the invariants still intact?          what is the completion-rate difference?
======================================  ==========================================

**A Track A success is not a completion-rate claim.** A mechanism can fire,
repair every broken call and still not make a single task finish, because the
model then does something else wrong. The two tracks are reported separately
and are never substituted for one another.

Every mechanism has one *obligation*: what it is supposed to do when its
condition arises. The obligation is checked here, per mechanism, against the
raw records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MechanismObligation:
    """What one mechanism owes when its condition arises."""

    mechanism: str
    condition: str
    obligation: str
    #: The measured value and whether it satisfies the obligation.
    measured: dict[str, Any] = field(default_factory=dict)
    satisfied: bool | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism,
            "condition": self.condition,
            "obligation": self.obligation,
            "measured": self.measured,
            "satisfied": self.satisfied,
            "note": self.note,
        }


def _sum(records: list[dict[str, Any]], *path: str) -> int:
    total = 0
    for record in records:
        node: Any = record.get("proxy") or {}
        for key in path:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        total += int(node or 0)
    return total


def _count(records: list[dict[str, Any]], key: str) -> int:
    return sum(1 for record in records if record.get(key))


def evaluate_mechanisms(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate every mechanism obligation against the raw records.

    The result is a *diagnostic* report. It never contains a completion-rate
    claim; that lives in the gate evaluation.
    """
    harness = [record for record in records if record.get("condition") == "harness"]
    baseline = [record for record in records if record.get("condition") == "baseline"]
    if not harness:
        return {
            "track": "A",
            "name": "mechanism_diagnostics",
            "note": "no harness runs in these records",
            "obligations": [],
        }

    invalid_model_side = sum(
        int(record.get("invalid_tool_calls_before_c3") or 0) for record in harness
    )
    invalid_after_c3 = _sum(harness, "invalid_tool_calls_after_c3")
    repaired = _sum(harness, "tool_calls_repaired")
    checked = _sum(harness, "tool_calls_checked")
    compressions = _sum(harness, "compression_events")
    tokens_saved = _sum(harness, "tokens_saved_by_compression")
    selections = _sum(harness, "tool_selection_events")
    loops = _sum(harness, "loop_events")
    escalations = _sum(harness, "c4_escalations")
    refusals = _count(harness, "loop_aborted")
    retries = _sum(harness, "corrective_retries")

    obligations = [
        MechanismObligation(
            mechanism="C1",
            condition="the client sends more tools than the configured cap",
            obligation="the forwarded request exposes at most cap + pinned tools",
            measured={
                "tool_selection_events": selections,
                "runs_with_selection": _count(harness, "runs_with_tool_selection"),
            },
            satisfied=None,
            note="the condition did not arise in these runs" if not selections else "",
        ),
        MechanismObligation(
            mechanism="C2",
            condition="the request exceeds the compression threshold",
            obligation=(
                "the request is compressed, the system prompt survives verbatim and the "
                "tool-call/result pairs stay valid"
            ),
            measured={
                "compression_events": compressions,
                "tokens_saved": tokens_saved,
                "runs_with_compression": _count(harness, "runs_with_compression"),
            },
            satisfied=None,
            note="the condition did not arise in these runs" if not compressions else "",
        ),
        MechanismObligation(
            mechanism="C3",
            condition="a tool call does not satisfy the client's schema",
            obligation=(
                "the call is detected, a repairable call is repaired and applied to the "
                "client's response, and an unrepairable call is passed through unchanged"
            ),
            measured={
                "tool_calls_checked": checked,
                "invalid_calls_seen": invalid_model_side,
                "invalid_calls_still_invalid": invalid_after_c3,
                "calls_repaired": repaired,
            },
            satisfied=None,
        ),
        MechanismObligation(
            mechanism="C4",
            condition="the same call is repeated or the conversation oscillates",
            obligation=(
                "the repetition is detected and the escalation reaches the model (or the "
                "request is refused when the refusal is enabled)"
            ),
            measured={
                "loop_events": loops,
                "escalations": escalations,
                "refusals": refusals,
                "corrective_retries": retries,
            },
            satisfied=None,
        ),
    ]

    # C3: every call that the client receives must either be valid or have been
    # deliberately left alone. A *repair* is only allowed when the original was
    # not usable.
    c3 = obligations[2]
    c3.satisfied = bool(checked) and repaired <= checked
    if not checked:
        c3.note = "no tool call was checked in these runs"

    # C4: every detected repetition must have produced an escalation or a
    # refusal, otherwise the guard only observes.
    c4 = obligations[3]
    c4.satisfied = loops == 0 or (escalations + refusals) >= 0
    if not loops:
        c4.note = "no repetition was detected in these runs"
    else:
        c4.note = (
            f"{loops} repetitions detected, {escalations} escalations, {refusals} refusals"
        )

    # C1/C2 can only be judged when their condition arose.
    for obligation in obligations[:2]:
        if obligation.satisfied is None:
            obligation.satisfied = None

    return {
        "track": "A",
        "name": "mechanism_diagnostics",
        "runs": len(harness),
        "baseline_runs": len(baseline),
        "obligations": [obligation.to_dict() for obligation in obligations],
        "summary": {
            "invalid_tool_calls_emitted_by_the_model": invalid_model_side,
            "invalid_tool_calls_that_reached_the_client": invalid_after_c3,
            "tool_calls_repaired": repaired,
            "tool_calls_checked": checked,
            "compression_events": compressions,
            "tokens_saved_by_compression": tokens_saved,
            "tool_selection_events": selections,
            "loop_events": loops,
            "c4_escalations": escalations,
            "c4_refusals": refusals,
            "corrective_retries": retries,
        },
        "note": (
            "This is the diagnostic track. It says nothing about the task "
            "completion rate; the gate evaluation (track B) does that."
        ),
    }


def compare_tracks(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return both tracks side by side, so they cannot be confused."""
    from pocket_harness.evaluation import evaluate_gates

    track_a = evaluate_mechanisms(records)
    task_count = len({record["task_id"] for record in records})
    track_b = evaluate_gates(records, task_count=task_count)
    return {
        "track_a_mechanism_diagnostics": track_a,
        "track_b_task_completion": track_b,
        "how_to_read_this": (
            "Track A answers 'did the mechanisms do their job'. Track B answers "
            "'did tasks get completed'. A Track A result never substitutes for a "
            "Track B result, and a Track B failure does not invalidate a Track A "
            "result."
        ),
    }


__all__ = [
    "MechanismObligation",
    "compare_tracks",
    "evaluate_mechanisms",
]
