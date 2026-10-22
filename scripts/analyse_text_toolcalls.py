"""Count the turns in which the model emitted a tool call as *text*."""

import json
import re
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
records = data["records"]

PATTERN = re.compile(r'"name"\s*:\s*"[A-Za-z_][A-Za-z0-9_]*"\s*,\s*"arguments"')

with_text = 0
without = 0
runs_with = 0
for record in records:
    text = record.get("final_text") or ""
    hit = bool(PATTERN.search(text))
    if hit:
        with_text += 1
        runs_with += 1
    else:
        without += 1

print(f"runs: {len(records)}")
print(f"runs whose final message contains a tool call as text: {runs_with}")
print(f"runs whose final message does not: {without}")

# A stricter variant: the tool call text plus a closing tag.
STRICT = re.compile(r"</?\s*tool[_-]?call", re.IGNORECASE)
tagged = sum(1 for r in records if STRICT.search(r.get("final_text") or ""))
print(f"runs with a <tool_call>-style tag in the final message: {tagged}")

empty_final = sum(1 for r in records if not (r.get("final_text") or "").strip())
print(f"runs with an empty final message: {empty_final}")
