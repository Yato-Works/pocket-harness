"""Compare the mechanisms between the two conditions of a benchmark run."""

import json
import sys
from collections import Counter
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
records = data["records"]

for condition in sorted({r["condition"] for r in records}):
    items = [r for r in records if r["condition"] == condition]
    classes = Counter(r.get("failure_class") or "passed" for r in items)
    stops = Counter(r.get("stop_reason") for r in items)
    invalid_before = sum(int(r.get("invalid_tool_calls_before_c3") or 0) for r in items)
    invalid_after = sum(
        int((r.get("proxy") or {}).get("invalid_tool_calls_after_c3") or 0) for r in items
    )
    checked = sum(int((r.get("proxy") or {}).get("tool_calls_checked") or 0) for r in items)
    repaired = sum(int((r.get("proxy") or {}).get("tool_calls_repaired") or 0) for r in items)
    escalations = sum(int((r.get("proxy") or {}).get("c4_escalations") or 0) for r in items)
    loops = sum(int((r.get("proxy") or {}).get("loop_events") or 0) for r in items)
    error_classes = Counter()
    for record in items:
        for call in record.get("tool_calls") or []:
            if call.get("error_class"):
                error_classes[call["error_class"]] += 1
    print(f"=== {condition} ===")
    print(f"  outcomes: {dict(sorted(classes.items()))}")
    print(f"  stop reasons: {dict(sorted(stops.items()))}")
    print(f"  tool calls checked: {checked}  repaired: {repaired}")
    print(f"  invalid tool calls (model side): {invalid_before}")
    print(f"  invalid tool calls (after C3): {invalid_after}")
    print(f"  invalid call error classes: {dict(sorted(error_classes.items()))}")
    print(f"  loop detections: {loops}  C4 escalations: {escalations}")
    print(f"  mean steps: {sum(int(r.get('steps') or 0) for r in items) / max(1, len(items)):.1f}")
