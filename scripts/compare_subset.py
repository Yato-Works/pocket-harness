"""Compare two raw benchmark files over a subset of tasks."""

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
    only = sys.argv[3] if len(sys.argv) > 3 else ""
    keys = sorted(set(left) & set(right))
    if only:
        keys = [key for key in keys if only in key[0]]
    same = 0
    changed = []
    for key in keys:
        a = left[key]
        b = right[key]
        outcome_a = (a.get("passed"), a.get("failure_class"), a.get("stop_reason"))
        outcome_b = (b.get("passed"), b.get("failure_class"), b.get("stop_reason"))
        if outcome_a == outcome_b:
            same += 1
        else:
            changed.append((key, outcome_a, outcome_b))
    print(f"compared {len(keys)} cells: {same} identical, {len(changed)} different")
    for key, before, after in changed:
        print(f"  {key[0]:<26} {key[1]:<9} seed{key[2]}  {before} -> {after}")
    passed_a = sum(1 for key in keys if left[key].get("passed"))
    passed_b = sum(1 for key in keys if right[key].get("passed"))
    print(f"passed: {passed_a} -> {passed_b}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
