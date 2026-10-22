"""Show the tool calls and the final files of selected runs."""

import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
wanted = sys.argv[2] if len(sys.argv) > 2 else None

for record in data["records"]:
    if wanted and wanted not in record["task_id"]:
        continue
    print(f"--- {record['task_id']} / {record['condition']} / seed {record['seed']} "
          f"passed={record['passed']} exit={record.get('test_exit_code')}")
    for call in record.get("tool_calls") or []:
        print(f"    {call['name']:<12} valid={call['valid_before_c3']} ok={call['ok']} "
              f"err={call['tool_error_class'] or call['error_class']}")
    print("    changed:", record.get("changed_files"))
    print("    final:", (record.get("final_text") or "")[:400].replace("\n", " "))
    print()
