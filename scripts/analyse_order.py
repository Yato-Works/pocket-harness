"""Did the result follow the condition or the run order?

The benchmark runs the baseline first and the harness second for the same
(task, seed). With ``--reverse-order`` both are run again in the opposite
order. If the *first* run of a pair wins systematically, the measurement is
biased by the order and not by the proxy.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    records = data["records"]
    print(f"records: {len(records)}")

    # The records of the reversed pass come after the normal ones.
    by_key: dict[tuple[str, str, int], list[dict]] = {}
    for record in records:
        key = (record["task_id"], record["condition"], record["seed"])
        by_key.setdefault(key, []).append(record)

    first_pass = Counter()
    second_pass = Counter()
    flips = 0
    for (task_id, condition, seed), items in sorted(by_key.items()):
        if len(items) < 2:
            continue
        a, b = items[0], items[1]
        first_pass[(condition, bool(a.get("passed")))] += 1
        second_pass[(condition, bool(b.get("passed")))] += 1
        if bool(a.get("passed")) != bool(b.get("passed")):
            flips += 1
            print(
                f"  flip {task_id:<26} {condition:<9} seed{seed}: "
                f"{a.get('passed')} -> {b.get('passed')} "
                f"(classes {a.get('failure_class')} -> {b.get('failure_class')})"
            )
    print()
    for condition in ("baseline", "harness"):
        print(
            f"  {condition:<9} first pass: pass={first_pass[(condition, True)]} "
            f"fail={first_pass[(condition, False)]} | "
            f"second pass: pass={second_pass[(condition, True)]} "
            f"fail={second_pass[(condition, False)]}"
        )
    print(f"\n  runs whose outcome changed between the two orders: {flips}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
