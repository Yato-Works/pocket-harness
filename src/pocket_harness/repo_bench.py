"""The repository-task benchmark (Plan.md §11).

The smoke task set in ``bench/tasks`` measures the *proxy* on single-turn
prompts. It cannot answer the questions in §11: those are about coding tasks,
worktrees, test-command exit status, a development/held-out split, several
seeds, and a failure taxonomy.

This module runs the comparison the plan describes:

* a task is a repository snapshot plus a prompt, a test command and optional
  forbidden-file constraints;
* every run gets a **fresh copy** of the snapshot (a clean worktree);
* pass/fail is the **exit status of the test command** plus the forbidden-file
  check — never an LLM judge;
* the same agent code drives both conditions; only the endpoint differs;
* every run records the model id, the steps, the timings, the token counts and
  the failure class;
* runs that could not be evaluated (infrastructure failures) are recorded as
  excluded with a reason and are not silently dropped.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from pocket_harness.agent import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_SHA256,
    TOOL_ERROR_CONTRACT_VERSION,
    AgentRun,
    ScriptedAgent,
    prepare_worktree,
)
from pocket_harness.config import Config
from pocket_harness.evaluation import evaluate_gates
from pocket_harness.mechanism_eval import compare_tracks
from pocket_harness.proxy import create_app
from pocket_harness.state import StateStore
from pocket_harness.stats import (
    exact_mcnemar,
    latency_summary,
    paired_difference,
    wilson_interval,
)
from pocket_harness.taxonomy import Classification, classify_run
from pocket_harness.version import PIPELINE_VERSION

DEFAULT_TASKS_DIR = Path(__file__).resolve().parent.parent.parent / "bench" / "repos"


@dataclass
class RepoTask:
    """One repository task."""

    task_id: str
    split: str
    prompt: str
    test_command: list[str]
    snapshot: Path
    forbidden_paths: list[str] = field(default_factory=list)
    max_steps: int = 12
    max_wall_seconds: int = 300
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "split": self.split,
            "test_command": self.test_command,
            "forbidden_paths": self.forbidden_paths,
            "max_steps": self.max_steps,
            "max_wall_seconds": self.max_wall_seconds,
            "description": self.description,
        }


def _test_command(raw: list[str]) -> list[str]:
    """Resolve the ``{python}`` placeholder to the running interpreter."""
    import sys

    return [str(sys.executable) if part == "{python}" else str(part) for part in raw]


def load_repo_tasks(directory: Path | None = None) -> list[RepoTask]:
    """Load the repository task suite."""
    root = Path(directory) if directory else DEFAULT_TASKS_DIR
    if not root.is_dir():
        return []
    tasks: list[RepoTask] = []
    for task_file in sorted(root.glob("*/task.json")):
        try:
            raw = json.loads(task_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        snapshot = task_file.parent / "repo"
        if not snapshot.is_dir():
            continue
        tasks.append(
            RepoTask(
                task_id=str(raw["task_id"]),
                split=str(raw.get("split", "dev")),
                prompt=str(raw["prompt"]),
                test_command=_test_command(
                    raw.get("test_command", ["{python}", "-m", "pytest", "-q"])
                ),
                snapshot=snapshot,
                forbidden_paths=[str(p) for p in raw.get("forbidden_paths", [])],
                max_steps=int(raw.get("max_steps", 12)),
                max_wall_seconds=int(raw.get("max_wall_seconds", 300)),
                description=str(raw.get("description", "")),
            )
        )
    return tasks


def suite_hash(tasks: list[RepoTask]) -> str:
    """Return a stable identity of the task suite."""
    digest = hashlib.sha256()
    for task in sorted(tasks, key=lambda t: t.task_id):
        digest.update(task.task_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(json.dumps(task.to_dict(), sort_keys=True).encode("utf-8"))
        digest.update(b"\0")
        for path in sorted(task.snapshot.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(task.snapshot)).encode("utf-8"))
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "sha256:" + digest.hexdigest()


def _changed_files(snapshot: Path, worktree: Path) -> list[str]:
    """Return the workspace-relative paths that differ between snapshot and worktree."""
    changed: list[str] = []

    def index(root: Path) -> dict[str, Path]:
        return {
            str(p.relative_to(root)).replace("\\", "/"): p
            for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
        }

    before = index(snapshot)
    after = index(worktree)
    for name in set(before) | set(after):
        old = before.get(name)
        new = after.get(name)
        if old is None or new is None:
            changed.append(name)
        elif old.read_bytes() != new.read_bytes():
            changed.append(name)
    return sorted(changed)


def _forbidden_hit(changed: list[str], forbidden: list[str]) -> str | None:
    """Return the first changed path that violates a forbidden-path rule."""
    for name in changed:
        for rule in forbidden:
            rule = rule.strip().replace("\\", "/")
            if not rule:
                continue
            if rule.endswith("/"):
                if name == rule.rstrip("/") or name.startswith(rule):
                    return name
            elif name == rule:
                return name
    return None


def check_task(task: RepoTask, worktree: Path) -> dict[str, Any]:
    """Run the task's test command and apply the forbidden-file constraints."""
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - the command comes from the task file
            task.test_command,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=task.max_wall_seconds,
        )
        exit_code = completed.returncode
        output = (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired:
        exit_code = 124
        output = "the test command timed out"
    except OSError as exc:
        exit_code = 125
        output = f"the test command could not be run: {exc}"

    changed = _changed_files(task.snapshot, worktree)
    forbidden = _forbidden_hit(changed, task.forbidden_paths)
    passed = exit_code == 0 and forbidden is None
    return {
        "passed": passed,
        "test_exit_code": exit_code,
        "test_output_tail": output[-2000:],
        "changed_files": changed,
        "forbidden_violation": forbidden,
        "check_seconds": round(time.monotonic() - started, 4),
    }


async def _run_agent(
    config: Config,
    task: RepoTask,
    condition: str,
    worktree: Path,
    *,
    seed: int,
    capture: str,
    state_root: Path | None,
    temperature: float | None = 0.0,
) -> tuple[AgentRun, dict[str, Any]]:
    """Run one agent against one endpoint and return the run plus the proxy trace."""
    proxy_events: list[dict[str, Any]] = []
    store: StateStore | None = None
    client: httpx.AsyncClient
    base_url = config.upstream.base_url

    if condition == "harness":
        run_config = config.model_copy(deep=True)
        run_config.logging.capture = capture
        store = StateStore(state_root)
        app = create_app(run_config, store=store)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://pocket.test",
            timeout=config.upstream.timeout_seconds,
        )
        base_url = "http://pocket.test/v1"
    else:
        client = httpx.AsyncClient(timeout=config.upstream.timeout_seconds)

    agent = ScriptedAgent(
        base_url=base_url,
        model=config.upstream.model,
        workdir=worktree,
        test_command=task.test_command,
        client=client,
        max_steps=task.max_steps,
        max_wall_seconds=task.max_wall_seconds,
        request_timeout=config.upstream.timeout_seconds,
        temperature=temperature,
        seed=seed,
    )
    try:
        run = await agent.run(task.prompt)
    finally:
        await client.aclose()
        if store is not None:
            proxy_events = list(store.iter_events(store.list_runs()[0]["run_id"]))

    return run, {"events": proxy_events}


def _proxy_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract the proxy-side measurements from the evidence events."""
    transform_ms: list[float] = []
    invalid_after = 0
    checked = 0
    repaired = 0
    compression_events = 0
    bytes_saved = 0
    selection_events = 0
    c4_warnings = 0
    blob_refs = 0
    loop_events = 0
    loop_aborted = 0
    corrective_retries = 0
    for event in events:
        name = event.get("event")
        payload = event.get("payload") or {}
        if name == "request.transformed":
            if isinstance(payload.get("transform_ms"), (int, float)):
                transform_ms.append(float(payload["transform_ms"]))
            token_count = payload.get("token_count") or {}
            before = int(token_count.get("before") or 0)
            after = int(token_count.get("after") or 0)
            if after < before:
                compression_events += 1
                bytes_saved += before - after
            for rule in payload.get("rules") or []:
                rule_id = str(rule.get("id", ""))
                if rule_id.startswith("C1"):
                    selection_events += 1
                elif rule_id.startswith("C4"):
                    c4_warnings += 1
        elif name == "toolcall.validated":
            checked += int(payload.get("checked") or 0)
            repaired += int(payload.get("repaired") or 0)
            invalid_after += int(payload.get("failed") or 0)
        elif name == "request.captured":
            blob_refs += 1
        elif name == "loop.detected":
            loop_events += 1
        elif name == "loop.aborted":
            loop_aborted += 1
        elif name == "repair.retry":
            corrective_retries += 1
    return {
        "transform_ms": transform_ms,
        "proxy_only_overhead_ms": latency_summary(transform_ms),
        "tool_calls_checked": checked,
        "tool_calls_repaired": repaired,
        "invalid_tool_calls_after_c3": invalid_after,
        "compression_events": compression_events,
        "tokens_saved_by_compression": bytes_saved,
        "tool_selection_events": selection_events,
        "c4_escalations": c4_warnings,
        "blob_references": blob_refs,
        "loop_events": loop_events,
        "loop_aborted": loop_aborted,
        "corrective_retries": corrective_retries,
    }


async def run_repo_benchmark(
    config: Config,
    *,
    task_dir: Path | None = None,
    seeds: list[int] | None = None,
    conditions: list[str] | None = None,
    split: str | None = None,
    limit: int | None = None,
    output_dir: Path | None = None,
    capture: str = "metadata",
    keep_worktrees: bool = False,
    temperature: float | None = 0.0,
    reverse_order: bool = False,
) -> dict[str, Any]:
    """Run the repository benchmark and return the raw records plus the summary."""
    tasks = load_repo_tasks(task_dir)
    if split:
        tasks = [t for t in tasks if t.split == split]
    if limit is not None:
        tasks = tasks[:limit]
    seeds = list(seeds or [1, 2, 3])
    conditions = list(conditions or ["baseline", "harness"])
    started = time.time()

    if not tasks:
        return {
            "summary": {"error": "no repository tasks were found", "task_count": 0},
            "raw_path": "",
            "records": [],
        }

    records: list[dict[str, Any]] = []
    worktree_root = Path(tempfile.mkdtemp(prefix="pocket-bench-"))
    try:
        for task in tasks:
            for seed in seeds:
                for condition in conditions:
                    records.append(
                        await _run_one(
                            config,
                            task,
                            condition,
                            seed,
                            worktree_root,
                            capture=capture,
                            keep_worktrees=keep_worktrees,
                            temperature=temperature,
                        )
                    )
                if reverse_order:
                    # The two conditions are run a second time in the opposite
                    # order, so that neither of them systematically inherits the
                    # upstream state from the other. This is a diagnostic: it
                    # shows whether the result follows the condition or the order.
                    for condition in reversed(conditions):
                        records.append(
                            await _run_one(
                                config,
                                task,
                                condition,
                                seed,
                                worktree_root,
                                capture=capture,
                                keep_worktrees=keep_worktrees,
                                temperature=temperature,
                            )
                        )
    finally:
        if not keep_worktrees:
            shutil.rmtree(worktree_root, ignore_errors=True)

    summary = summarize(
        records,
        config,
        seeds=seeds,
        conditions=conditions,
        started=started,
        temperature=temperature,
    )
    summary["task_suite_hash"] = suite_hash(tasks)
    summary["tasks"] = [t.to_dict() for t in tasks]
    # The public release gate counts the whole suite, not only the tasks that
    # took part in this run (Plan.md §11.5: "expand to at least 40 tasks, keep
    # a separate held-out slice").
    summary["task_suite_size"] = len(load_repo_tasks(task_dir))
    summary["gates"] = evaluate_gates(
        records,
        task_count=summary["task_suite_size"],
        splits=_splits_of(records),
    )
    raw_path = ""
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = str(output_dir / "bench-repo-raw.json")
        Path(raw_path).write_text(
            json.dumps(
                {
                    "pipeline_version": PIPELINE_VERSION,
                    "config_hash": config.config_hash(),
                    "manifest": build_manifest(
                        config, seeds=seeds, conditions=conditions, temperature=temperature
                    ),
                    "records": records,
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    else:
        raw_path = "(not written; pass --out to persist the raw records)"
    return {"summary": summary, "raw_path": raw_path, "records": records}


async def _run_one(
    config: Config,
    task: RepoTask,
    condition: str,
    seed: int,
    worktree_root: Path,
    *,
    capture: str,
    keep_worktrees: bool,
    temperature: float | None = 0.0,
) -> dict[str, Any]:
    """Run one (task, seed, condition) combination and return its record."""
    worktree = worktree_root / f"{task.task_id}-{condition}-{seed}"
    prepare_worktree(task.snapshot, worktree)
    state_root = worktree_root / f"{task.task_id}-{condition}-{seed}-state"

    record: dict[str, Any] = {
        "task_id": task.task_id,
        "split": task.split,
        "condition": condition,
        "seed": seed,
        "model": config.upstream.model,
        "upstream": config.upstream.base_url,
        "passed": False,
        "eligible": True,
        "excluded_reason": None,
        "failure_class": "",
        "failure_signals": [],
        "failure_reasons": [],
    }

    run_started = time.monotonic()
    try:
        run, proxy = await _run_agent(
            config,
            task,
            condition,
            worktree,
            seed=seed,
            capture=capture,
            state_root=state_root,
            temperature=temperature,
        )
    except Exception as exc:  # noqa: BLE001 - an infrastructure failure is recorded, not raised
        record["eligible"] = False
        record["excluded_reason"] = f"infrastructure failure: {type(exc).__name__}: {exc}"
        record["failure_class"] = "R"
        record["failure_signals"] = ["R"]
        record["wall_seconds"] = round(time.monotonic() - run_started, 4)
        return record
    finally:
        if not keep_worktrees:
            shutil.rmtree(state_root, ignore_errors=True)

    check = check_task(task, worktree)
    proxy_metrics = _proxy_metrics(proxy["events"])

    classification = classify_run(
        task_passed=check["passed"],
        run_completed=run.stop_reason not in {"upstream_error", "transport_error"},
        upstream_error=run.upstream_error,
        upstream_status=run.upstream_status,
        context_overflow=run.context_overflow,
        invalid_tool_calls=proxy_metrics["invalid_tool_calls_after_c3"]
        if condition == "harness"
        else run.invalid_tool_calls,
        loop_events=proxy_metrics["loop_events"],
        budget_exhausted=run.stop_reason in {"step_limit", "wall_limit"},
        step_limit=task.max_steps if run.stop_reason == "step_limit" else None,
        wall_limit=run.stop_reason == "wall_limit",
        tool_call_delivered_as_text=run.tool_call_delivered_as_text,
        empty_completion=run.empty_completion,
    )

    record.update(
        {
            "passed": check["passed"],
            "test_exit_code": check["test_exit_code"],
            "test_output_tail": check["test_output_tail"],
            "check_seconds": check["check_seconds"],
            "forbidden_violation": check["forbidden_violation"],
            "changed_files": check["changed_files"],
            "steps": run.steps,
            "wall_seconds": run.wall_seconds,
            "ttft_seconds": run.ttft_seconds,
            "time_to_first_tool_call": run.time_to_first_tool_call,
            "stop_reason": run.stop_reason,
            "prompt_tokens": run.prompt_tokens,
            "completion_tokens": run.completion_tokens,
            "total_tokens": run.total_tokens,
            "invalid_tool_calls_before_c3": run.invalid_tool_calls,
            "tool_calls": run.tool_calls,
            "final_text": run.final_text[:2000],
            "failure_class": classification.primary_class,
            "failure_signals": classification.signals,
            "failure_reasons": classification.reasons,
            "eligible": classification.eligible,
            "proxy": proxy_metrics,
        }
    )
    if not keep_worktrees:
        shutil.rmtree(worktree, ignore_errors=True)
    return record


def build_manifest(
    config: Config,
    *,
    seeds: list[int],
    conditions: list[str],
    temperature: float | None = 0.0,
) -> dict[str, Any]:
    """Build the run manifest required by Plan.md §11.2."""
    manifest: dict[str, Any] = {
        "pipeline_version": PIPELINE_VERSION,
        "config_hash": config.config_hash(),
        "model": config.upstream.model,
        "upstream_base_url": config.upstream.base_url,
        "upstream_model": config.upstream.model,
        "context_length": config.effective_context_length(),
        "seeds": seeds,
        "conditions": conditions,
        "temperature": temperature,
        "system_prompt": SYSTEM_PROMPT,
        "system_prompt_sha256": SYSTEM_PROMPT_SHA256,
        "tool_error_contract": TOOL_ERROR_CONTRACT_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": _cpu_count(),
        "ram_bytes": _ram_bytes(),
        "packages": _package_versions(),
        "server": _server_version(config.upstream.base_url),
    }
    return manifest


def _cpu_count() -> int | None:
    import os

    return os.cpu_count()


def _ram_bytes() -> int | None:
    try:
        import psutil  # type: ignore

        return int(psutil.virtual_memory().total)
    except Exception:
        pass
    try:  # pragma: no cover - Windows specific
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return int(status.ullTotalPhys)
    except Exception:
        return None


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in ("httpx", "starlette", "uvicorn", "pydantic", "jsonschema"):
        try:
            module = __import__(name)
            versions[name] = str(getattr(module, "__version__", "unknown"))
        except Exception:
            versions[name] = "not installed"
    return versions


def _server_version(base_url: str) -> dict[str, Any]:
    """Ask the upstream server for its version, if it exposes one."""
    root = base_url.rstrip("/")
    for suffix in ("/../api/version", "/api/version"):
        url = root + suffix if suffix.startswith("/api") else root.replace("/v1", "") + "/api/version"
        try:
            response = httpx.get(url, timeout=5.0)
            if response.status_code == 200:
                return {"url": url, "version": response.json()}
        except Exception:
            continue
    return {"url": None, "version": None}


def summarize(
    records: list[dict[str, Any]],
    config: Config,
    *,
    seeds: list[int],
    conditions: list[str],
    started: float,
    temperature: float | None = 0.0,
) -> dict[str, Any]:
    """Summarize the raw records the way Plan.md §11.4 requires."""
    eligible = [r for r in records if r.get("eligible", True)]
    excluded = [r for r in records if not r.get("eligible", True)]

    per_condition: dict[str, Any] = {}
    for condition in sorted({r["condition"] for r in records}):
        items = [r for r in records if r["condition"] == condition]
        eligible_items = [r for r in items if r.get("eligible", True)]
        passed = [r for r in eligible_items if r["passed"]]
        interval = wilson_interval(len(passed), len(eligible_items))
        failures = [r for r in eligible_items if not r["passed"]]
        class_counts: dict[str, int] = {}
        for record in failures:
            code = record.get("failure_class") or "W"
            class_counts[code] = class_counts.get(code, 0) + 1
        per_condition[condition] = {
            "runs": len(items),
            "eligible_runs": len(eligible_items),
            "excluded_runs": len(items) - len(eligible_items),
            "passed": len(passed),
            "completion_rate": interval.to_dict(),
            "mean_steps": _mean([r.get("steps") for r in eligible_items]),
            "mean_wall_seconds": _mean([r.get("wall_seconds") for r in eligible_items]),
            "wall_seconds": latency_summary(
                [r["wall_seconds"] for r in eligible_items if r.get("wall_seconds") is not None]
            ),
            "ttft_seconds": latency_summary(
                [r["ttft_seconds"] for r in eligible_items if r.get("ttft_seconds") is not None]
            ),
            "total_tokens": sum(int(r.get("total_tokens") or 0) for r in eligible_items),
            "failure_classes": dict(sorted(class_counts.items())),
            "invalid_tool_calls_before_c3": sum(
                int(r.get("invalid_tool_calls_before_c3") or 0) for r in eligible_items
            ),
            "invalid_tool_calls_after_c3": sum(
                int((r.get("proxy") or {}).get("invalid_tool_calls_after_c3") or 0)
                for r in eligible_items
            ),
            "tokens_saved_by_compression": sum(
                int((r.get("proxy") or {}).get("tokens_saved_by_compression") or 0)
                for r in eligible_items
            ),
            "proxy_only_overhead_ms": latency_summary(
                [
                    ms
                    for r in eligible_items
                    for ms in ((r.get("proxy") or {}).get("transform_ms") or [])
                ]
            ),
        }

    pairs = _paired(records)
    paired = paired_difference(pairs)
    test = exact_mcnemar(pairs)

    return {
        "pipeline_version": PIPELINE_VERSION,
        "config_hash": config.config_hash(),
        "model": config.upstream.model,
        "seeds": seeds,
        "conditions": conditions,
        "temperature": temperature,
        "runs": len(records),
        "eligible_runs": len(eligible),
        "excluded_runs": [
            {
                "task_id": r["task_id"],
                "condition": r["condition"],
                "seed": r["seed"],
                "reason": r.get("excluded_reason"),
            }
            for r in excluded
        ],
        "per_condition": per_condition,
        "paired": paired,
        "paired_test": test.to_dict(),
        "intervention_coverage": _coverage(eligible),
        "tracks": compare_tracks(records),
        "started_at": started,
        "finished_at": time.time(),
    }


def _splits_of(records: list[dict[str, Any]]) -> dict[str, int]:
    """Return the number of distinct tasks per split in these records."""
    counts: dict[str, int] = {}
    for record in records:
        split = str(record.get("split") or "unknown")
        counts.setdefault(split, 0)
    for split in list(counts):
        counts[split] = len(
            {
                record["task_id"]
                for record in records
                if str(record.get("split") or "unknown") == split
            }
        )
    return counts


def _coverage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """How often the proxy components actually changed something.

    A comparison in which no intervention ever fired cannot show an effect,
    whatever the completion numbers look like.
    """
    harness_records = [r for r in records if r.get("condition") == "harness"]
    if not harness_records:
        return {"harness_runs": 0}
    proxy = [r.get("proxy") or {} for r in harness_records]
    return {
        "harness_runs": len(harness_records),
        "runs_with_compression": sum(1 for p in proxy if p.get("compression_events")),
        "runs_with_tool_selection": sum(1 for p in proxy if p.get("tool_selection_events")),
        "runs_with_tool_repair": sum(1 for p in proxy if p.get("tool_calls_repaired")),
        "runs_with_invalid_tool_calls": sum(
            1 for p in proxy if p.get("invalid_tool_calls_after_c3")
        ),
        "runs_with_loop_detection": sum(1 for p in proxy if p.get("loop_events")),
        "compression_events": sum(int(p.get("compression_events") or 0) for p in proxy),
        "tool_selection_events": sum(int(p.get("tool_selection_events") or 0) for p in proxy),
        "c4_escalations": sum(int(p.get("c4_escalations") or 0) for p in proxy),
        "tool_calls_repaired": sum(int(p.get("tool_calls_repaired") or 0) for p in proxy),
        "invalid_tool_calls_after_c3": sum(
            int(p.get("invalid_tool_calls_after_c3") or 0) for p in proxy
        ),
        "loop_events": sum(int(p.get("loop_events") or 0) for p in proxy),
        "tokens_saved_by_compression": sum(
            int(p.get("tokens_saved_by_compression") or 0) for p in proxy
        ),
    }


def _paired(records: list[dict[str, Any]]) -> list[tuple[bool, bool]]:
    """Return the paired (baseline, harness) outcomes per (task, seed)."""
    index: dict[tuple[str, int], dict[str, bool]] = {}
    for record in records:
        if not record.get("eligible", True):
            continue
        key = (record["task_id"], record["seed"])
        index.setdefault(key, {})[record["condition"]] = bool(record["passed"])
    return [
        (values["baseline"], values["harness"])
        for values in index.values()
        if "baseline" in values and "harness" in values
    ]


def _mean(values: list[Any]) -> float | None:
    numbers = [float(v) for v in values if isinstance(v, (int, float))]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 4)


__all__ = [
    "Classification",
    "RepoTask",
    "build_manifest",
    "check_task",
    "load_repo_tasks",
    "run_repo_benchmark",
    "suite_hash",
    "summarize",
]
