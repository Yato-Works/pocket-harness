"""Gate evaluation for the benchmark (Plan.md §11.5).

The gates are decided from the raw records, never from a summary that was
tuned to look good. Each verdict states the criterion, the measured numbers
and the decision, so a third party can recompute it.

* **G0 — Problem fit.** Continue only if more than half of the *eligible*
  baseline failures are attributable to O/M/L/S rather than W. ``R`` (upstream
  rejection) is excluded from the denominator and reported separately.
* **G1 — Demonstrated value.** Held-out completion must improve by at least
  +30 percentage points. Below +10 points the premise is re-evaluated; between
  +10 and +30 a single targeted diagnosis and a single predeclared retest are
  allowed.
* **Public release gate.** At least 40 tasks, a separate held-out slice, and
  published configurations, manifests, raw outcomes, failure examples and known
  limitations.
"""

from __future__ import annotations

from typing import Any

from pocket_harness.taxonomy import ADDRESSABLE

#: The thresholds of Plan.md §11.5.
G0_MAJORITY_THRESHOLD = 0.5
G1_PASS_THRESHOLD = 0.30
G1_STOP_THRESHOLD = 0.10
PUBLIC_RELEASE_TASK_COUNT = 40


def _is_upstream_rejection(record: dict[str, Any]) -> bool:
    """Return whether a record failed because the upstream refused the request.

    ``R`` must never enter the efficacy denominator (Plan.md §3), so this is
    checked from the failure class itself and not only from the run's
    eligibility flag.
    """
    if record.get("failure_class") == "R":
        return True
    signals = record.get("failure_signals") or []
    return "R" in signals


def _eligible_baseline_failures(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Baseline failures that are eligible for the efficacy denominator."""
    return [
        record
        for record in records
        if record.get("condition") == "baseline"
        and record.get("eligible", True)
        and not record.get("passed")
        and not _is_upstream_rejection(record)
    ]


def evaluate_g0(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate G0 — problem fit."""
    failures = _eligible_baseline_failures(records)
    attributable = [
        record
        for record in failures
        if (record.get("failure_class") or "") in ADDRESSABLE
        or any(signal in ADDRESSABLE for signal in (record.get("failure_signals") or []))
    ]
    residual = [record for record in failures if record not in attributable]
    excluded = [
        record
        for record in records
        if record.get("condition") == "baseline"
        and (not record.get("eligible", True) or _is_upstream_rejection(record))
    ]
    upstream_rejected = [
        record
        for record in records
        if record.get("condition") == "baseline" and _is_upstream_rejection(record)
    ]

    ratio = (len(attributable) / len(failures)) if failures else None
    if not failures:
        verdict = "not_evaluable"
        reason = "there were no eligible baseline failures to classify"
    elif ratio is not None and ratio > G0_MAJORITY_THRESHOLD:
        verdict = "pass"
        reason = (
            f"{len(attributable)} of {len(failures)} eligible baseline failures are "
            f"attributable to O/M/L/S ({ratio:.0%})"
        )
    else:
        verdict = "fail"
        reason = (
            f"only {len(attributable)} of {len(failures)} eligible baseline failures are "
            f"attributable to O/M/L/S ({ratio:.0%}); the majority is W"
        )

    return {
        "gate": "G0",
        "name": "problem_fit",
        "criterion": (
            "more than half of the eligible baseline failures are attributable to "
            "O/M/L/S rather than W; R is excluded from the denominator"
        ),
        "verdict": verdict,
        "reason": reason,
        "eligible_baseline_failures": len(failures),
        "attributable_to_omlsl": len(attributable),
        "residual_w": len(residual),
        "attributable_ratio": round(ratio, 4) if ratio is not None else None,
        "threshold": G0_MAJORITY_THRESHOLD,
        "excluded_upstream_failures": len(excluded),
        "upstream_rejections_reported_separately": len(upstream_rejected),
        "examples": [
            {
                "task_id": record["task_id"],
                "seed": record["seed"],
                "failure_class": record.get("failure_class"),
                "reasons": record.get("failure_reasons"),
            }
            for record in (attributable + residual)[:10]
        ],
    }


def evaluate_g1(records: list[dict[str, Any]], *, split: str = "held-out") -> dict[str, Any]:
    """Evaluate G1 — demonstrated value on the held-out split."""
    subset = [record for record in records if record.get("split") == split]
    if not subset:
        subset = list(records)

    def completion(condition: str) -> tuple[int, int]:
        eligible = [
            record
            for record in subset
            if record["condition"] == condition and record.get("eligible", True)
        ]
        return sum(1 for record in eligible if record["passed"]), len(eligible)

    baseline_ok, baseline_n = completion("baseline")
    harness_ok, harness_n = completion("harness")
    if not baseline_n or not harness_n:
        return {
            "gate": "G1",
            "name": "demonstrated_value",
            "criterion": "held-out completion improves by at least +30 percentage points",
            "verdict": "not_evaluable",
            "reason": "one of the conditions has no eligible runs",
            "split": split,
        }

    baseline_rate = baseline_ok / baseline_n
    harness_rate = harness_ok / harness_n
    difference = harness_rate - baseline_rate

    if difference >= G1_PASS_THRESHOLD:
        verdict = "pass"
        reason = f"held-out completion improved by {difference:+.1%} (>= +30 points)"
    elif difference < G1_STOP_THRESHOLD:
        verdict = "stop"
        reason = (
            f"held-out completion improved by only {difference:+.1%} (< +10 points): "
            "stop feature expansion and re-evaluate the premise"
        )
    else:
        verdict = "diagnose"
        reason = (
            f"held-out completion improved by {difference:+.1%}, which is between "
            "+10 and +30 points: one targeted diagnosis and one predeclared retest only"
        )

    return {
        "gate": "G1",
        "name": "demonstrated_value",
        "criterion": "held-out completion improves by at least +30 percentage points",
        "verdict": verdict,
        "reason": reason,
        "split": split,
        "baseline": {"passed": baseline_ok, "runs": baseline_n, "rate": round(baseline_rate, 4)},
        "harness": {"passed": harness_ok, "runs": harness_n, "rate": round(harness_rate, 4)},
        "difference": round(difference, 4),
        "pass_threshold": G1_PASS_THRESHOLD,
        "stop_threshold": G1_STOP_THRESHOLD,
    }


def evaluate_public_release_gate(
    records: list[dict[str, Any]],
    *,
    task_count: int,
    splits: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Evaluate the public release gate.

    The gate counts the **whole** task suite (Plan.md §11.5: "expand to at
    least 40 tasks, keep a separate held-out slice"), so the caller passes the
    size of the complete suite and the number of tasks per split.
    """
    split_counts = splits or {
        str(split): len(
            {r["task_id"] for r in records if str(r.get("split")) == str(split)}
        )
        for split in {r.get("split") for r in records}
    }
    has_held_out = any("held" in str(split) for split in split_counts)
    meets_size = task_count >= PUBLIC_RELEASE_TASK_COUNT
    return {
        "gate": "public_release",
        "name": "public_release",
        "criterion": (
            "at least 40 tasks, a separate held-out slice, and published "
            "configurations, manifests, raw outcomes, failure examples and limitations"
        ),
        "verdict": "pass" if meets_size and has_held_out else "fail",
        "reason": (
            f"the task suite has {task_count} tasks "
            f"({', '.join(f'{k}: {v}' for k, v in sorted(split_counts.items()))}); "
            f"the gate requires at least {PUBLIC_RELEASE_TASK_COUNT} and a separate "
            "held-out slice"
        ),
        "task_count": task_count,
        "splits": split_counts,
        "has_separate_held_out_slice": has_held_out,
    }


def evaluate_gates(
    records: list[dict[str, Any]],
    *,
    task_count: int,
    splits: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Evaluate every gate and return the verdicts."""
    return {
        "gates": [
            evaluate_g0(records),
            evaluate_g1(records),
            evaluate_public_release_gate(
                records, task_count=task_count, splits=splits
            ),
        ],
        "note": (
            "the verdicts are computed from the raw records; no run was excluded "
            "from the aggregates except runs that were marked ineligible with a "
            "recorded reason"
        ),
    }


__all__ = [
    "G0_MAJORITY_THRESHOLD",
    "G1_PASS_THRESHOLD",
    "G1_STOP_THRESHOLD",
    "PUBLIC_RELEASE_TASK_COUNT",
    "evaluate_g0",
    "evaluate_g1",
    "evaluate_gates",
    "evaluate_public_release_gate",
]
