"""Classify the *sub-causes* of a failed run (diagnosis aid, not product code)."""

import json
import re
import sys
from collections import Counter
from pathlib import Path

TOOL_CALL_TEXT = re.compile(r'"name"\s*:\s*"[A-Za-z_][A-Za-z0-9_]*"\s*,\s*"arguments"')

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
only = sys.argv[2] if len(sys.argv) > 2 else None
records = data["records"]

counts: dict[str, Counter] = {}
for record in records:
    if record.get("passed") or not record.get("eligible", True):
        continue
    if only and record.get("split") != only:
        continue
    condition = record["condition"]
    changed = record.get("changed_files") or []
    exit_code = record.get("test_exit_code")
    text = record.get("final_text") or ""
    if record.get("forbidden_violation"):
        cause = "edited_a_forbidden_path"
    elif not changed:
        if TOOL_CALL_TEXT.search(text):
            cause = "tool_call_delivered_as_text"
        elif not text.strip():
            cause = "empty_completion_from_the_upstream"
        else:
            cause = "stopped_without_acting"
    elif exit_code == 2:
        cause = "broke_the_project_syntax"
    else:
        cause = "changed_the_code_but_the_test_still_fails"
    counts.setdefault(condition, Counter())[cause] += 1
    counts.setdefault(condition, Counter())["_class_" + str(record.get("failure_class"))] += 1

for condition, counter in sorted(counts.items()):
    print(f"{condition}:")
    for key, value in sorted(counter.items()):
        print(f"   {key:<42} {value}")
