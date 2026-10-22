"""Declare the conditions of the held-out evaluation before running it."""

import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pocket_harness.agent import (  # noqa: E402
    SYSTEM_PROMPT_SHA256,
    TOOL_ERROR_CONTRACT_VERSION,
)

held_out = []
for path in sorted(pathlib.Path("bench/repos").glob("*/task.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("split") == "held-out":
        held_out.append(data["task_id"])

conditions = {
    "declared_at": "2026-10-22, before the run",
    "held_out_tasks": held_out,
    "held_out_task_count": len(held_out),
    "temperature": 0.0,
    "seeds": [1, 2, 3],
    "conditions": ["baseline", "harness"],
    "corrective_retries_per_request": 0,
    "corrective_retries_per_task": 0,
    "abort_on_repeat": False,
    "warn_on_repeat": True,
    "empty_retry_budget_per_task": 1,
    "max_identical_failures": 3,
    "system_prompt_sha256": SYSTEM_PROMPT_SHA256,
    "tool_error_contract": TOOL_ERROR_CONTRACT_VERSION,
    "notes": (
        "The rules are locked: the prompt, the tool error contract, the retry "
        "budgets and the task list are declared here and are not changed by "
        "this evaluation. The held-out tasks were never used for tuning; the "
        "tasks whose results were inspected earlier were moved to the "
        "development split."
    ),
}

conditions["predeclared_conditions_sha256"] = hashlib.sha256(
    json.dumps(conditions, sort_keys=True).encode("utf-8")
).hexdigest()

target = pathlib.Path("docs/benchmarks/heldout-2026-10-22-predeclared.json")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(conditions, indent=2) + "\n", encoding="utf-8")
print(json.dumps(conditions, indent=2))
