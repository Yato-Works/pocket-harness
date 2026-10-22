"""Statistics for the benchmark report (Plan.md §11.4).

The plan requires 95% Wilson intervals for the completion rate, a paired test
for the binary paired outcomes, and predeclared rules about what is counted.
Everything here is a pure function of the raw records so the report can be
recomputed from the raw data.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Interval:
    """A confidence interval for a proportion."""

    point: float
    low: float
    high: float
    successes: int
    trials: int
    method: str = "wilson"

    def to_dict(self) -> dict[str, Any]:
        return {
            "point": round(self.point, 4),
            "low": round(self.low, 4),
            "high": round(self.high, 4),
            "successes": self.successes,
            "trials": self.trials,
            "method": self.method,
        }


def wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> Interval:
    """Return the 95% Wilson score interval for a binomial proportion."""
    if trials <= 0:
        return Interval(point=0.0, low=0.0, high=0.0, successes=successes, trials=0)
    phat = successes / trials
    denominator = 1 + (z**2) / trials
    centre = phat + (z**2) / (2 * trials)
    spread = z * math.sqrt((phat * (1 - phat) / trials) + (z**2) / (4 * trials**2))
    return Interval(
        point=phat,
        low=max(0.0, (centre - spread) / denominator),
        high=min(1.0, (centre + spread) / denominator),
        successes=successes,
        trials=trials,
    )


def _exact_binomial_two_sided(k: int, n: int) -> float:
    """Return the exact two-sided binomial p-value for ``k`` successes out of ``n``.

    The two-sided p-value is twice the smaller tail (capped at 1.0), which is
    the standard exact test for a paired binary outcome.
    """
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2**n)
    return min(1.0, 2.0 * tail)


@dataclass(frozen=True)
class PairedTest:
    """The result of an exact McNemar test on the discordant pairs."""

    name: str
    statistic: str
    b: int  # baseline succeeded, harness failed
    c: int  # baseline failed, harness succeeded
    discordant: int
    p_value: float
    method: str = "exact_mcnemar_binomial"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "statistic": self.statistic,
            "baseline_only_success": self.b,
            "harness_only_success": self.c,
            "discordant_pairs": self.discordant,
            "p_value": round(self.p_value, 6),
            "method": self.method,
        }


def exact_mcnemar(pairs: Sequence[tuple[bool, bool]]) -> PairedTest:
    """Return the exact McNemar test for paired binary outcomes.

    ``pairs`` holds ``(baseline_success, harness_success)`` tuples. Only the
    discordant pairs carry information; the test is the exact binomial test on
    the number of pairs that moved in the harness's favour.
    """
    b = sum(1 for base, harness in pairs if base and not harness)
    c = sum(1 for base, harness in pairs if not base and harness)
    discordant = b + c
    if discordant == 0:
        p_value = 1.0
    else:
        p_value = _exact_binomial_two_sided(min(b, c), discordant)
    return PairedTest(
        name="exact_mcnemar",
        statistic="b+c",
        b=b,
        c=c,
        discordant=discordant,
        p_value=p_value,
    )


def sign_test(pairs: Sequence[tuple[bool, bool]]) -> PairedTest:
    """Return the task-level sign test on the discordant pairs."""
    return exact_mcnemar(pairs)


def paired_difference(
    pairs: Sequence[tuple[bool, bool]],
) -> dict[str, Any]:
    """Return the paired completion-rate difference and the per-pair outcomes."""
    if not pairs:
        return {
            "pairs": 0,
            "baseline_successes": 0,
            "harness_successes": 0,
            "difference": None,
            "both": 0,
            "neither": 0,
            "outcomes": [],
        }
    baseline = sum(1 for base, _ in pairs if base)
    harness = sum(1 for _, harn in pairs if harn)
    both = sum(1 for base, harn in pairs if base and harn)
    neither = sum(1 for base, harn in pairs if not base and not harn)
    return {
        "pairs": len(pairs),
        "baseline_successes": baseline,
        "harness_successes": harness,
        "difference": round((harness - baseline) / len(pairs), 4),
        "both": both,
        "neither": neither,
        "outcomes": [
            {
                "baseline": bool(base),
                "harness": bool(harn),
                "change": "both" if base == harn else ("harness_only" if harn else "baseline_only"),
            }
            for base, harn in pairs
        ],
    }


def percentile(values: Sequence[float], q: float) -> float | None:
    """Return the ``q`` quantile (0..1) of ``values`` using linear interpolation."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[int(position)])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def latency_summary(values: Sequence[float]) -> dict[str, Any]:
    """Return p50/p95/mean for a set of durations."""
    if not values:
        return {"count": 0, "p50": None, "p95": None, "mean": None, "total": None}
    return {
        "count": len(values),
        "p50": round(percentile(values, 0.50) or 0.0, 4),
        "p95": round(percentile(values, 0.95) or 0.0, 4),
        "mean": round(sum(values) / len(values), 4),
        "total": round(sum(values), 4),
    }


__all__ = [
    "Interval",
    "PairedTest",
    "exact_mcnemar",
    "latency_summary",
    "paired_difference",
    "percentile",
    "sign_test",
    "wilson_interval",
]
