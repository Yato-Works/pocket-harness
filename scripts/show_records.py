"""Print a readable table of the raw benchmark records."""

import json
import sys
from pathlib import Path

COLUMNS = (
    ("task", 26),
    ("condition", 9),
    ("seed", 5),
    ("pass", 6),
    ("exit", 5),
    ("steps", 6),
    ("stop", 16),
    ("class", 6),
    ("forbidden", 10),
)


def main() -> int:
    path = Path(sys.argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data["records"]
    print(f"runs: {len(records)}")
    header = " ".join(f"{name:<{width}}" for name, width in COLUMNS)
    print(header)
    print("-" * len(header))
    for record in records:
        values = (
            record["task_id"],
            record["condition"],
            record["seed"],
            record["passed"],
            record.get("test_exit_code"),
            record.get("steps"),
            record.get("stop_reason"),
            record.get("failure_class"),
            record.get("forbidden_violation"),
        )
        cells = []
        for (_name, width), value in zip(COLUMNS, values, strict=True):
            cells.append(f"{str(value):<{width}}")
        print(" ".join(cells).rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
