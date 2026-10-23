"""The ``pocket-harness`` command line interface.

Subcommands:

``serve``    run the local proxy
``doctor``   check the upstream, the model, the context window, and the tool round trip
``bench``    run the fixed local task set
``replay``   re-apply the transformations to captured payloads and compare hashes
``report``   summarize a run
``status``   print a deterministic status report
``init``     write a starter ``pocket.toml``

The CLI never calls an LLM to decide anything. ``doctor`` and ``bench`` talk to the
configured upstream because that is the thing being measured — not to judge the result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from pocket_harness.config import Config, ConfigError, load_config
from pocket_harness.version import PIPELINE_VERSION, __version__


def _print_json(value: Any) -> None:
    """Print a value as pretty JSON."""
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def _load(args: argparse.Namespace) -> Config:
    """Load the configuration and apply the command-line overrides."""
    config_path = getattr(args, "config", None)
    if config_path:
        # An explicitly named configuration file must exist. The loader falls
        # back to the built-in defaults for a missing file, which would run
        # the command against a different configuration than the user asked
        # for without saying so.
        resolved = Path(config_path).expanduser()
        if not resolved.is_file():
            raise ConfigError(f"the configuration file {resolved} does not exist")
    config = load_config(config_path)
    if getattr(args, "upstream", None):
        config.upstream.base_url = args.upstream
    if getattr(args, "model", None):
        config.upstream.model = args.model
    if getattr(args, "context", None):
        config.upstream.context_length = args.context
    if getattr(args, "capture", None):
        config.logging.capture = args.capture
    if getattr(args, "profile", None):
        from pocket_harness.profiles import apply_profile

        apply_profile(config, args.profile)
    if getattr(args, "host", None):
        config.server.host = args.host
    if getattr(args, "port", None):
        config.server.port = args.port
    return config


# --------------------------------------------------------------------- serve


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the proxy server."""
    import uvicorn

    from pocket_harness.proxy import create_app

    config = _load(args)
    app = create_app(config)
    print(
        f"pocket-harness {__version__} listening on "
        f"http://{config.server.host}:{config.server.port}/v1 "
        f"-> {config.upstream.base_url} ({config.upstream.model or 'no model set'})"
    )
    if config.effective_context_length() is None:
        print(
            "warning: no context length configured; context budgeting is disabled. "
            "Pass --context or set [upstream].context_length."
        )
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")
    return 0


# -------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the upstream, the model, the context window, and the tool round trip."""
    from pocket_harness.doctor import run_doctor

    config = _load(args)
    report = asyncio.run(run_doctor(config))
    _print_json(report)
    return 0 if report.get("ok") else 1


# --------------------------------------------------------------------- bench


def cmd_bench(args: argparse.Namespace) -> int:
    """Run the local benchmark task set."""
    if args.suite == "repo":
        return _cmd_bench_repo(args)
    from pocket_harness.bench import run_benchmark

    config = _load(args)
    result = asyncio.run(
        run_benchmark(
            config,
            seed=args.seed,
            limit=args.limit,
            output_dir=Path(args.out) if args.out else None,
            condition=args.condition,
        )
    )
    _print_json(result["summary"])
    print(f"\nraw results written to {result['raw_path']}", file=sys.stderr)
    if result["summary"].get("error"):
        return 1
    return 0


def _cmd_bench_repo(args: argparse.Namespace) -> int:
    """Run the repository benchmark and evaluate the Plan.md gates."""
    from pocket_harness.evaluation import evaluate_gates
    from pocket_harness.repo_bench import load_repo_tasks, run_repo_benchmark

    config = _load(args)
    if getattr(args, "baseline_upstream", None):
        config.upstream.baseline_base_url = str(args.baseline_upstream)
    if getattr(args, "abort_on_repeat", False):
        config.loop_guard.abort_on_repeat = True
    if getattr(args, "no_c4_warning", False):
        config.loop_guard.warn_on_repeat = False
    if getattr(args, "no_corrective_retry", False):
        config.repair.max_corrective_retries_per_request = 0
        config.repair.max_corrective_retries_per_task = 0
    seeds = [int(part) for part in str(args.seeds).split(",") if part.strip()]
    tasks = load_repo_tasks(Path(args.tasks) if args.tasks else None)
    result = asyncio.run(
        run_repo_benchmark(
            config,
            task_dir=Path(args.tasks) if args.tasks else None,
            seeds=seeds,
            conditions=[args.condition] if args.condition != "both" else ["baseline", "harness"],
            split=args.split,
            limit=args.limit,
            output_dir=Path(args.out) if args.out else None,
            capture=args.capture or "metadata",
            keep_worktrees=args.keep_worktrees,
            temperature=args.temperature,
            reverse_order=args.reverse_order,
        )
    )
    summary = dict(result["summary"])
    if not summary.get("error"):
        summary["gates"] = evaluate_gates(result["records"], task_count=len(tasks))
    _print_json(summary)
    print(f"\nraw results written to {result['raw_path']}", file=sys.stderr)
    if summary.get("error"):
        return 1
    if not result["records"]:
        print("the benchmark produced no records", file=sys.stderr)
        return 1
    if args.require_gates:
        verdicts = [gate["verdict"] for gate in summary["gates"]["gates"]]
        if "fail" in verdicts or "stop" in verdicts:
            print("the performance gates were not met", file=sys.stderr)
            return 1
    return 0


# -------------------------------------------------------------------- replay


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-apply the transformations to captured payloads and compare hashes."""
    from pocket_harness.replay import run_replay

    config = _load(args)
    report = run_replay(config, Path(args.run), capture_ref=args.capture_file)
    _print_json(report)
    return 0 if report.get("ok") else 1


# -------------------------------------------------------------------- report


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Run the deterministic mechanism scenarios (diagnostic track)."""
    from pocket_harness.diagnose import run_scenarios

    config = _load(args)
    report = asyncio.run(run_scenarios(config, root=args.root or ".e2e/diagnose"))
    _print_json(report)
    return 0 if report["summary"]["failed"] == 0 else 1


def cmd_report(args: argparse.Namespace) -> int:
    """Summarize a run."""
    from pocket_harness.report import build_report

    config = _load(args)
    report = build_report(config, args.run)
    _print_json(report)
    return 0 if report.get("ok") else 1


# -------------------------------------------------------------------- status


def cmd_status(args: argparse.Namespace) -> int:
    """Print a deterministic status report."""
    from pocket_harness.state import StateStore

    config = _load(args)
    store = StateStore(config.state.resolved_root())
    runs = store.list_runs()
    if args.run:
        runs = [run for run in runs if run["run_id"] == args.run]
    _print_json(
        {
            "pipeline_version": PIPELINE_VERSION,
            "state_root": str(store.root),
            "capture": config.logging.capture,
            "runs": [
                {
                    "run_id": run["run_id"],
                    "label": run["label"],
                    "events": sum(
                        1 for _ in store.iter_events(run["run_id"])
                    ),
                }
                for run in runs
            ],
        }
    )
    return 0


# ---------------------------------------------------------------------- init


DEFAULT_TOML = """\
# Pocket Harness configuration.
# Every value here participates in the configuration hash recorded with each run.

[server]
host = "127.0.0.1"
port = 8787

[state]
# Where runs, evidence events, and the local blob store are written.
# On Windows use forward slashes or a literal string, e.g. 'C:/Users/me/.pocket'.
root = "~/.pocket"

[upstream]
kind = "openai-compatible"
base_url = "http://127.0.0.1:11434/v1"
model = "qwen2.5-coder:7b"
# The context window is required for C2 to enforce a budget.
context_length = 32768
# Set to false when the upstream does not accept these fields.
supports_count_endpoint = false
unsupported_fields = []
optional_fields = []

[client]
profile = "cline"
pinned_tools = []
destructive_tools = []

[context]
start_at = 0.85
target_at = 0.70
keep_recent_turns = 4
system_prompt_mutation = false
reserved_output_tokens = 1024
safety_margin_tokens = 256
max_tool_output_bytes = 8192

[tools]
max_non_pinned = 6
sticky_unused_turns = 5
fuzzy_tool_name_repair = false
max_description_chars = 200

[repair]
max_corrective_retries_per_request = 1
max_corrective_retries_per_task = 3
allow_fuzzy_tool_name = false
allow_schema_defaults = false

[loop_guard]
repeated_call_threshold = 3
max_steps_per_task = 40
max_wall_seconds = 1800
detect_oscillation = true

[logging]
capture = "metadata"
telemetry = false

[routing]
enabled = false
allow_paid_fallback = false
"""


def cmd_init(args: argparse.Namespace) -> int:
    """Write a starter configuration file."""
    path = Path(args.path)
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    path.write_text(DEFAULT_TOML, encoding="utf-8")
    print(f"wrote {path}")
    return 0


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="pocket-harness",
        description="Local-first deterministic proxy and execution-trace system.",
    )
    parser.add_argument("--version", action="version", version=f"pocket-harness {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="path to pocket.toml")
        p.add_argument("--upstream", help="upstream base URL, e.g. http://127.0.0.1:11434/v1")
        p.add_argument("--model", help="upstream model ID")
        p.add_argument("--context", type=int, help="context window in tokens")
        p.add_argument(
            "--capture",
            choices=["metadata", "local-replay"],
            help="how much to record locally",
        )
        p.add_argument("--profile", help="client profile, e.g. cline, opencode")

    p_serve = sub.add_parser("serve", help="run the local proxy")
    add_common(p_serve)
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.set_defaults(func=cmd_serve)

    p_doctor = sub.add_parser("doctor", help="check the upstream and the tool round trip")
    add_common(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    p_bench = sub.add_parser("bench", help="run the local benchmark task set")
    add_common(p_bench)
    p_bench.add_argument("--seed", type=int, default=0)
    p_bench.add_argument("--limit", type=int, help="run only the first N tasks")
    p_bench.add_argument("--out", help="directory for the raw results")
    p_bench.add_argument(
        "--condition",
        choices=["both", "harness", "baseline"],
        default="both",
        help="which condition(s) to run",
    )
    p_bench.add_argument(
        "--suite",
        choices=["smoke", "repo"],
        default="smoke",
        help=(
            "smoke: the single-turn task set (a proxy smoke test); "
            "repo: the repository task suite with the Plan.md §11 gates"
        ),
    )
    p_bench.add_argument("--tasks", help="directory with the repository task suite")
    p_bench.add_argument("--seeds", default="1,2,3", help="comma separated seeds")
    p_bench.add_argument(
        "--split",
        choices=["dev", "held-out"],
        help="only run the tasks of this split",
    )
    p_bench.add_argument(
        "--require-gates",
        action="store_true",
        help="exit non-zero when a performance gate is not met",
    )
    p_bench.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="sampling temperature sent to the upstream (0 means greedy and reproducible)",
    )
    p_bench.add_argument(
        "--abort-on-repeat",
        action="store_true",
        help="let the proxy refuse a request that repeats a tool call (ADR 0003)",
    )
    p_bench.add_argument(
        "--no-c4-warning",
        action="store_true",
        help="do not append the C4 repetition warning (diagnosis only)",
    )
    p_bench.add_argument(
        "--baseline-upstream",
        dest="baseline_upstream",
        help=(
            "base URL of an independent upstream instance for the baseline "
            "condition, so the two conditions never share upstream state "
            "(DECISION.md §7.1)"
        ),
    )
    p_bench.add_argument(
        "--reverse-order",
        action="store_true",
        help=(
            "run each condition a second time in the opposite order, so that neither "
            "one inherits the upstream state from the other (diagnosis only)"
        ),
    )
    p_bench.add_argument(
        "--no-corrective-retry",
        action="store_true",
        help="do not issue a corrective inference retry (diagnosis only)",
    )
    p_bench.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="keep the temporary worktrees for inspection (diagnostics only)",
    )
    p_bench.set_defaults(func=cmd_bench)

    p_replay = sub.add_parser("replay", help="re-apply transformations and compare hashes")
    add_common(p_replay)
    p_replay.add_argument("--run", required=True, help="run ID to replay")
    p_replay.add_argument(
        "--capture-file",
        dest="capture_file",
        help=(
            "path to a captured payload JSON file (or a sha256 blob reference); "
            "defaults to the body the proxy captured locally"
        ),
    )
    p_replay.set_defaults(func=cmd_replay)

    p_report = sub.add_parser("report", help="summarize a run")
    add_common(p_report)
    p_report.add_argument("--run", help="run ID (defaults to the newest run)")
    p_report.set_defaults(func=cmd_report)

    p_diagnose = sub.add_parser(
        "diagnose",
        help="run the deterministic mechanism scenarios (diagnostic track)",
    )
    add_common(p_diagnose)
    p_diagnose.add_argument("--root", help="directory for the scenario state")
    p_diagnose.set_defaults(func=cmd_diagnose)

    p_status = sub.add_parser("status", help="print a deterministic status report")
    add_common(p_status)
    p_status.add_argument("--run", help="run ID")
    p_status.set_defaults(func=cmd_status)

    p_init = sub.add_parser("init", help="write a starter pocket.toml")
    p_init.add_argument("path", nargs="?", default="pocket.toml")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    """The CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"pocket-harness: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

