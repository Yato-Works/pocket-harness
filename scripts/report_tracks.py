"""The two evaluation tracks, side by side, from a raw benchmark file.

Track A: what the mechanisms did.
Track B: what happened to the tasks.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    records = data["records"]
    harness = [r for r in records if r["condition"] == "harness"]
    baseline = [r for r in records if r["condition"] == "baseline"]

    print("=== TRACK A: mechanism diagnostics ===")
    invalid_reaching_client = sum(
        int(r.get("invalid_tool_calls_before_c3") or 0) for r in harness
    )
    invalid_detected = sum(
        int((r.get("proxy") or {}).get("invalid_tool_calls_after_c3") or 0) for r in harness
    )
    repaired = sum(int((r.get("proxy") or {}).get("tool_calls_repaired") or 0) for r in harness)
    checked = sum(int((r.get("proxy") or {}).get("tool_calls_checked") or 0) for r in harness)
    compressions = sum(
        int((r.get("proxy") or {}).get("compression_events") or 0) for r in harness
    )
    tokens_saved = sum(
        int((r.get("proxy") or {}).get("tokens_saved_by_compression") or 0) for r in harness
    )
    escalations = sum(int((r.get("proxy") or {}).get("c4_escalations") or 0) for r in harness)
    loops = sum(int((r.get("proxy") or {}).get("loop_events") or 0) for r in harness)
    print(f"  tool calls validated by C3        : {checked}")
    print(f"  invalid calls detected by C3      : {invalid_detected}")
    print(f"  invalid calls repaired by C3      : {repaired}")
    print(f"  invalid calls that reached the client (harness): {invalid_reaching_client}")
    print(
        "  invalid calls that reached the client (baseline): "
        f"{sum(int(r.get('invalid_tool_calls_before_c3') or 0) for r in baseline)}"
    )
    print(f"  C2 compression events             : {compressions} ({tokens_saved} tokens saved)")
    print(f"  C4 loop detections / escalations  : {loops} / {escalations}")

    print("\n=== TRACK B: task completion ===")
    for condition, items in (("baseline", baseline), ("harness", harness)):
        eligible = [r for r in items if r.get("eligible", True)]
        passed = [r for r in eligible if r.get("passed")]
        classes = Counter(r.get("failure_class") or "passed" for r in eligible)
        excluded = len(items) - len(eligible)
        print(
            f"  {condition:<9} passed {len(passed)}/{len(eligible)} "
            f"({len(passed) / max(1, len(eligible)):.1%}), excluded {excluded}"
        )
        print(f"            classes: {dict(sorted(classes.items()))}")

    index = {(r["task_id"], r["condition"], r["seed"]): r for r in records}
    keys = sorted({(r["task_id"], r["seed"]) for r in records})
    transitions = Counter()
    for task_id, seed in keys:
        a = index.get((task_id, "baseline", seed))
        b = index.get((task_id, "harness", seed))
        if not a or not b or not a.get("eligible", True) or not b.get("eligible", True):
            transitions["ineligible pair"] += 1
            continue
        if bool(a.get("passed")) == bool(b.get("passed")):
            transitions["both pass" if a.get("passed") else "both fail"] += 1
        else:
            transitions["harness only" if b.get("passed") else "baseline only"] += 1
    print(f"\n  paired outcomes: {dict(sorted(transitions.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
