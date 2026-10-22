"""Why does one condition lose to the other? (diagnosis helper)

Pairs the outcomes of the two conditions per (task, seed) and classifies the
transitions, so that a difference can be attributed to a mechanism instead of
being reported as a number.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data["records"]
    index = {
        (r["task_id"], r["condition"], r["seed"]): r for r in records
    }
    keys = sorted({(r["task_id"], r["seed"]) for r in records})

    transitions: Counter[str] = Counter()
    detail: dict[str, Counter[str]] = {}
    for task_id, seed in keys:
        base = index.get((task_id, "baseline", seed))
        harn = index.get((task_id, "harness", seed))
        if base is None or harn is None:
            continue
        if not base.get("eligible", True) or not harn.get("eligible", True):
            transitions["ineligible_pair"] += 1
            continue
        base_pass = bool(base.get("passed"))
        harn_pass = bool(harn.get("passed"))
        if base_pass and harn_pass:
            label = "both_pass"
        elif not base_pass and not harn_pass:
            label = "both_fail"
        elif base_pass:
            label = "baseline_only"
        else:
            label = "harness_only"
        transitions[label] += 1
        detail.setdefault(label, Counter())
        detail[label][str(base.get("failure_class") or "-")] += 1
        detail[label][str(harn.get("failure_class") or "-")] += 1

    print(f"pairs: {len(keys)}")
    for label, count in sorted(transitions.items()):
        print(f"  {label:<16} {count}")
    print()
    for label in sorted(detail):
        print(f"{label}: {dict(detail[label])}")

    # Which mechanism was active in the runs that only one condition passed?
    print("\nmechanisms in the runs the two conditions disagree about:")
    for task_id, seed in keys:
        base = index.get((task_id, "baseline", seed))
        harn = index.get((task_id, "harness", seed))
        if not base or not harn:
            continue
        if bool(base.get("passed")) == bool(harn.get("passed")):
            continue
        if not base.get("eligible", True) or not harn.get("eligible", True):
            continue
        proxy = harn.get("proxy") or {}
        active = [
            name
            for name, count in (
                ("C2.compress", proxy.get("compression_events")),
                ("C4.escalate", proxy.get("c4_escalations")),
                ("C3.invalid", proxy.get("invalid_tool_calls_after_c3")),
                ("C3.repaired", proxy.get("tool_calls_repaired")),
            )
            if count
        ]
        winner = "harness" if harn.get("passed") else "baseline"
        print(
            f"  {task_id:<26} seed{seed}  winner={winner:<9} "
            f"harness_fail={harn.get('failure_class'):<3} mechanisms={active}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
