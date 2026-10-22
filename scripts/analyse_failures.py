"""Aggregate the failure causes of a raw benchmark file."""

import json
import sys
from collections import Counter
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
records = data["records"]

print("=== failure classes by condition ===")
for condition in sorted({r["condition"] for r in records}):
    counts = Counter(
        r.get("failure_class") or "passed"
        for r in records
        if r["condition"] == condition and r.get("eligible", True)
    )
    print(f"  {condition}: {dict(sorted(counts.items()))}")

print("\n=== stop reasons by condition ===")
for condition in sorted({r["condition"] for r in records}):
    counts = Counter(
        r.get("stop_reason")
        for r in records
        if r["condition"] == condition
    )
    print(f"  {condition}: {dict(sorted(counts.items()))}")

print("\n=== failing runs (class, exit code, forbidden, last tool, tail) ===")
for r in records:
    if r.get("passed") or not r.get("eligible", True):
        continue
    calls = r.get("tool_calls") or []
    last = calls[-1]["name"] if calls else "-"
    print(f"\n--- {r['task_id']} / {r['condition']} / seed {r['seed']}")
    print(f"    class={r.get('failure_class')} exit={r.get('test_exit_code')} "
          f"forbidden={r.get('forbidden_violation')} stop={r.get('stop_reason')} "
          f"steps={r.get('steps')} last_tool={last}")
    print(f"    changed: {r.get('changed_files')}")
    tail = (r.get("test_output_tail") or "").strip().splitlines()
    for line in tail[-6:]:
        print(f"    | {line}")
