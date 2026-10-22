"""Compare two raw benchmark files cell by cell.

This is the reproducibility check: with the same task, condition and seed, a
reproducible upstream must give the same outcome.
"""

import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        (r["task_id"], r["condition"], r["seed"]): r
        for r in data["records"]
    }


def main() -> int:
    left = load(sys.argv[1])
    right = load(sys.argv[2])
    keys = sorted(set(left) | set(right))
    same = 0
    different = []
    for key in keys:
        a = left.get(key)
        b = right.get(key)
        if a is None or b is None:
            different.append((key, "missing"))
            continue
        outcome_a = (a.get("passed"), a.get("failure_class"), a.get("stop_reason"))
        outcome_b = (b.get("passed"), b.get("failure_class"), b.get("stop_reason"))
        if outcome_a == outcome_b:
            same += 1
        else:
            different.append((key, f"{outcome_a} -> {outcome_b}"))
    print(f"compared {len(keys)} cells: {same} identical, {len(different)} different")
    for key, change in different:
        print(f"  {key[0]:<28} {key[1]:<9} seed{key[2]}  {change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
