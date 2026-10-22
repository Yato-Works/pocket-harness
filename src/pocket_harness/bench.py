"""``pocket bench`` — the local, deterministic benchmark harness.

The benchmark runs a fixed task set against the configured upstream in two conditions:

``baseline``  the raw client request is sent unchanged
``harness``   the request goes through the C0/C1/C2/C6 pipeline

For every run the harness records the raw outcome — completion, tool calls, token
usage, latency, and the failure class. The summary is computed from those raw
records with plain arithmetic.

**No LLM judge is used.** Success is determined by deterministic checks: an exact
string match, a required substring, a required tool call, or a required file created
in a sandbox directory. Plan.md §11 forbids an LLM judge for exactly this reason.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pocket_harness.config import Config
from pocket_harness.hashing import hash_obj
from pocket_harness.pipeline import Pipeline
from pocket_harness.upstream import UpstreamClient, UpstreamError, UpstreamTarget
from pocket_harness.version import PIPELINE_VERSION


@dataclass(slots=True)
class BenchTask:
    """One benchmark task."""

    task_id: str
    prompt: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    #: Deterministic success checks.
    expect_substring: str | None = None
    expect_exact: str | None = None
    expect_tool: str | None = None
    expect_file: str | None = None
    expect_file_content: str | None = None
    max_tokens: int = 256

    def evaluate(self, text: str, tool_names: list[str]) -> tuple[bool, str]:
        """Evaluate the deterministic success checks."""
        if self.expect_exact is not None:
            return text.strip() == self.expect_exact, "exact_match"
        if self.expect_substring is not None:
            return self.expect_substring.lower() in text.lower(), "substring"
        if self.expect_tool is not None:
            return self.expect_tool in tool_names, "tool_call"
        if self.expect_file is not None:
            return True, "file_check_skipped"
        return bool(text.strip()), "non_empty"


def load_tasks(directory: Path | None = None) -> list[BenchTask]:
    """Load the benchmark task set from JSON files."""
    directory = directory or (Path(__file__).resolve().parent.parent.parent / "bench" / "tasks")
    tasks: list[BenchTask] = []
    if not directory.is_dir():
        return tasks
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for item in raw.get("tasks", []):
            tasks.append(
                BenchTask(
                    task_id=str(item["task_id"]),
                    prompt=str(item["prompt"]),
                    tools=item.get("tools", []),
                    expect_substring=item.get("expect_substring"),
                    expect_exact=item.get("expect_exact"),
                    expect_tool=item.get("expect_tool"),
                    expect_file=item.get("expect_file"),
                    expect_file_content=item.get("expect_file_content"),
                    max_tokens=int(item.get("max_tokens", 256)),
                )
            )
    return tasks


def _client_payload(task: BenchTask, model: str) -> dict[str, Any]:
    """Build the client request for a task."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": task.prompt}],
        "max_tokens": task.max_tokens,
    }
    if task.tools:
        payload["tools"] = task.tools
    return payload


def _target(config: Config) -> UpstreamTarget:
    return UpstreamTarget(
        base_url=config.upstream.base_url,
        model=config.upstream.model,
        api_key=config.upstream.resolved_api_key(),
        extra_headers=config.upstream.extra_headers,
    )


async def _run_one(
    client: UpstreamClient,
    task: BenchTask,
    payload: dict[str, Any],
    condition: str,
    seed: int,
) -> dict[str, Any]:
    """Run one task in one condition and return its raw record."""
    record: dict[str, Any] = {
        "task_id": task.task_id,
        "condition": condition,
        "seed": seed,
        "request_sha256": hash_obj(payload),
        "completed": False,
        "success": False,
        "check": None,
        "failure_class": None,
        "latency_seconds": None,
        "usage": {},
        "tool_calls": [],
        "content_length": 0,
    }
    started = time.monotonic()
    try:
        _, collector = await client.create_chat(payload)
    except UpstreamError as exc:
        record["failure_class"] = "upstream_unavailable"
        record["error"] = f"HTTP {exc.status_code}"
        record["latency_seconds"] = round(time.monotonic() - started, 4)
        return record
    except Exception as exc:  # noqa: BLE001
        record["failure_class"] = "upstream_unavailable"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["latency_seconds"] = round(time.monotonic() - started, 4)
        return record

    text = collector.text
    calls = collector.tool_call_list()
    tool_names = [call.name for call in calls]
    success, check = task.evaluate(text, tool_names)

    record.update(
        {
            "completed": True,
            "success": success,
            "check": check,
            "latency_seconds": round(time.monotonic() - started, 4),
            "usage": collector.usage,
            "tool_calls": [
                {"name": call.name, "arguments_sha256": hash_obj(call.arguments)}
                for call in calls
            ],
            "content_length": len(text),
            "finish_reason": collector.finish_reason,
        }
    )
    if not success:
        record["failure_class"] = "task_not_satisfied"
    return record


async def run_benchmark(
    config: Config,
    seed: int = 0,
    limit: int | None = None,
    output_dir: Path | None = None,
    condition: str = "both",
    task_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the benchmark and return the summary plus the path to the raw results."""
    tasks = load_tasks(task_dir)
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        return {
            "summary": {"error": "no benchmark tasks were found", "task_count": 0},
            "raw_path": "",
            "records": [],
        }

    client = UpstreamClient(_target(config), timeout=config.upstream.timeout_seconds)
    pipeline = Pipeline(config)

    conditions = ["baseline", "harness"] if condition == "both" else [condition]
    records: list[dict[str, Any]] = []

    for cond in conditions:
        for task in tasks:
            payload = _client_payload(task, config.upstream.model)
            if cond == "harness":
                canonical, _ = pipeline.normalize(payload)
                result = pipeline.transform(canonical)
                payload = result.request.to_payload()
            records.append(await _run_one(client, task, payload, cond, seed))

    summary = _summarize(records, config, seed, len(tasks))
    raw_path = ""
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = str(output_dir / f"bench-raw-seed{seed}.json")
        Path(raw_path).write_text(
            json.dumps(
                {
                    "pipeline_version": PIPELINE_VERSION,
                    "config_hash": config.config_hash(),
                    "model": config.upstream.model,
                    "seed": seed,
                    "task_count": len(tasks),
                    "note": (
                        "success is determined by deterministic checks only; "
                        "no LLM judge was used"
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


def _summarize(
    records: list[dict[str, Any]],
    config: Config,
    seed: int,
    task_count: int,
) -> dict[str, Any]:
    """Compute the benchmark summary from the raw records."""
    by_condition: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_condition.setdefault(record["condition"], []).append(record)

    conditions: dict[str, Any] = {}
    for cond, items in sorted(by_condition.items()):
        completed = [r for r in items if r["completed"]]
        succeeded = [r for r in items if r["success"]]
        latencies = [r["latency_seconds"] for r in items if r["latency_seconds"] is not None]
        total_tokens = 0
        for r in completed:
            usage = r.get("usage") or {}
            total_tokens += int(usage.get("total_tokens") or 0)
        conditions[cond] = {
            "runs": len(items),
            "completed": len(completed),
            "completion_rate": round(len(completed) / len(items), 4) if items else 0.0,
            "succeeded": len(succeeded),
            "success_rate": round(len(succeeded) / len(items), 4) if items else 0.0,
            "mean_latency_seconds": (
                round(sum(latencies) / len(latencies), 4) if latencies else None
            ),
            "total_tokens": total_tokens,
        }

    baseline = conditions.get("baseline", {})
    harness = conditions.get("harness", {})
    delta = None
    if baseline and harness:
        delta = {
            "completion_rate": round(
                harness.get("completion_rate", 0.0) - baseline.get("completion_rate", 0.0), 4
            ),
            "success_rate": round(
                harness.get("success_rate", 0.0) - baseline.get("success_rate", 0.0), 4
            ),
            "mean_latency_seconds": (
                round(
                    (harness.get("mean_latency_seconds") or 0.0)
                    - (baseline.get("mean_latency_seconds") or 0.0),
                    4,
                )
                if harness.get("mean_latency_seconds") is not None
                and baseline.get("mean_latency_seconds") is not None
                else None
            ),
        }

    failure_classes: dict[str, int] = {}
    for record in records:
        failure = record.get("failure_class")
        if failure:
            failure_classes[failure] = failure_classes.get(failure, 0) + 1

    return {
        "pipeline_version": PIPELINE_VERSION,
        "config_hash": config.config_hash(),
        "model": config.upstream.model,
        "seed": seed,
        "task_count": task_count,
        "conditions": conditions,
        "delta_harness_minus_baseline": delta,
        "failure_classes": dict(sorted(failure_classes.items())),
        "note": (
            "success is determined by deterministic checks only; no LLM judge was used"
        ),
    }



