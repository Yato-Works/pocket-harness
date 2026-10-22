"""Re-classify existing raw records with the current failure rules.

This makes the before/after comparison of a *labelling* change possible on the
same runs, without spending new model calls.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pocket_harness.evaluation import evaluate_g0, evaluate_g1  # noqa: E402
from pocket_harness.taxonomy import classify_run  # noqa: E402
from pocket_harness.toolcall_text import find_text_tool_call  # noqa: E402


def reclassify(record: dict) -> dict:
    text = record.get("final_text") or ""
    delivered_as_text = bool(find_text_tool_call(text))
    empty = not text.strip() and record.get("stop_reason") == "model_stopped"
    proxy = record.get("proxy") or {}
    classification = classify_run(
        task_passed=bool(record.get("passed")),
        run_completed=record.get("stop_reason") not in {"upstream_error", "transport_error"},
        upstream_error=record.get("upstream_error"),
        upstream_status=record.get("upstream_status"),
        context_overflow=bool(record.get("context_overflow")),
        invalid_tool_calls=proxy.get("invalid_tool_calls_after_c3") or 0,
        loop_events=proxy.get("loop_events") or 0,
        budget_exhausted=record.get("stop_reason") in {"step_limit", "wall_limit"},
        tool_call_delivered_as_text=delivered_as_text,
        empty_completion=empty,
    )
    updated = dict(record)
    updated["failure_class"] = classification.primary_class
    updated["failure_signals"] = classification.signals
    updated["failure_reasons"] = classification.reasons
    updated["eligible"] = classification.eligible
    updated["reclassified"] = True
    return updated


def main() -> int:
    path = Path(sys.argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))
    records = [reclassify(record) for record in data["records"]]

    print("=== re-classified failure classes ===")
    for condition in sorted({r["condition"] for r in records}):
        counts: dict[str, int] = {}
        for record in records:
            if record["condition"] != condition:
                continue
            key = record["failure_class"] or "passed"
            counts[key] = counts.get(key, 0) + 1
        print(f"  {condition}: {dict(sorted(counts.items()))}")

    g0 = evaluate_g0(records)
    g1 = evaluate_g1(records)
    print("\nG0:", g0["verdict"], "-", g0["reason"])
    print("G1:", g1["verdict"], "-", g1["reason"])
    out = path.with_name(path.stem + "-reclassified.json")
    out.write_text(json.dumps({**data, "records": records}, indent=2, ensure_ascii=False), "utf-8")
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
