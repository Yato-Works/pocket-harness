"""Summarize a raw benchmark file: per-condition rates, gates and coverage."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pocket_harness.config import load_config  # noqa: E402
from pocket_harness.evaluation import evaluate_gates  # noqa: E402
from pocket_harness.repo_bench import summarize  # noqa: E402


def main() -> int:
    path = Path(sys.argv[1])
    config_path = sys.argv[2] if len(sys.argv) > 2 else "bench/bench-local.toml"
    data = json.loads(path.read_text(encoding="utf-8"))
    config = load_config(config_path)
    records = data["records"]
    seeds = sorted({r["seed"] for r in records})
    conditions = sorted({r["condition"] for r in records})
    summary = summarize(records, config, seeds=seeds, conditions=conditions, started=0)
    gates = evaluate_gates(records, task_count=len({r["task_id"] for r in records}))

    print(f"file: {path}")
    print(f"runs: {len(records)}  eligible: {summary['eligible_runs']}  "
          f"excluded: {len(summary['excluded_runs'])}")
    for condition, values in summary["per_condition"].items():
        rate = values["completion_rate"]
        print(
            f"  {condition:<9} passed {values['passed']}/{values['eligible_runs']} "
            f"= {rate['point']:.3f}  95% CI [{rate['low']:.3f}, {rate['high']:.3f}]  "
            f"classes={values['failure_classes']}"
        )
    print(f"  paired difference: {summary['paired']['difference']}  "
          f"test: {summary['paired_test']['method']} p={summary['paired_test']['p_value']}")
    print(f"  proxy overhead (harness): "
          f"{summary['per_condition'].get('harness', {}).get('proxy_only_overhead_ms')}")
    print(f"  intervention coverage: {summary['intervention_coverage']}")
    for gate in gates["gates"]:
        print(f"  {gate['gate']:<14} {gate['verdict']:<14} {gate['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
