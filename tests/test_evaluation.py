"""Tests for the benchmark agent, the task suite and the evaluation gates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from pocket_harness.agent import ScriptedAgent, ToolBox, validate_tool_call
from pocket_harness.evaluation import evaluate_g0, evaluate_g1, evaluate_gates
from pocket_harness.repo_bench import (
    _changed_files,
    _forbidden_hit,
    load_repo_tasks,
    suite_hash,
)
from pocket_harness.stats import exact_mcnemar, paired_difference, wilson_interval
from pocket_harness.taxonomy import classify_run, signal_tally, tally

TASKS_DIR = Path(__file__).resolve().parent.parent / "bench" / "repos"


# ------------------------------------------------------------------ taxonomy


def test_a_passing_run_has_no_failure_class() -> None:
    result = classify_run(task_passed=True, run_completed=True)
    assert result.primary_class == ""
    assert result.eligible is True


def test_a_plain_failure_is_class_w() -> None:
    result = classify_run(task_passed=False, run_completed=True)
    assert result.primary_class == "W"
    assert result.signals == ["W"]


def test_an_upstream_rejection_is_class_r_and_ineligible() -> None:
    result = classify_run(
        task_passed=False,
        run_completed=False,
        upstream_error="HTTP 503",
        upstream_status=503,
    )
    assert result.primary_class == "R"
    assert result.eligible is False


def test_a_rate_limit_is_class_r() -> None:
    result = classify_run(
        task_passed=False, run_completed=False, upstream_error="rate limited", upstream_status=429
    )
    assert result.primary_class == "R"


def test_a_context_rejection_is_class_o_not_r() -> None:
    result = classify_run(
        task_passed=False,
        run_completed=False,
        upstream_error="the prompt is too long: maximum context length is 8192 tokens",
        upstream_status=400,
    )
    assert result.primary_class == "O"
    assert result.eligible is True


def test_a_context_overflow_signal_is_class_o() -> None:
    result = classify_run(task_passed=False, run_completed=False, context_overflow=True)
    assert result.primary_class == "O"


def test_an_invalid_tool_call_is_class_m() -> None:
    result = classify_run(task_passed=False, run_completed=True, invalid_tool_calls=2)
    assert result.primary_class == "M"
    assert "M" in result.signals


def test_a_loop_is_class_l_and_a_budget_stop_is_class_s() -> None:
    looped = classify_run(task_passed=False, run_completed=True, loop_events=1)
    assert looped.primary_class == "L"
    stopped = classify_run(
        task_passed=False, run_completed=True, budget_exhausted=True, step_limit=12
    )
    assert stopped.primary_class == "S"
    assert "step limit" in stopped.reasons[0]


def test_r_wins_over_every_other_signal() -> None:
    result = classify_run(
        task_passed=False,
        run_completed=False,
        upstream_error="boom",
        upstream_status=500,
        invalid_tool_calls=1,
        loop_events=2,
        budget_exhausted=True,
    )
    assert result.primary_class == "R"
    assert set(result.signals) == {"R", "M", "L", "S"}


def test_tally_counts_primary_classes_and_signals() -> None:
    items = [
        classify_run(task_passed=False, run_completed=True),
        classify_run(task_passed=False, run_completed=True, invalid_tool_calls=1),
    ]
    assert tally(items)["W"] == 1
    assert tally(items)["M"] == 1
    assert signal_tally(items)["W"] == 1


# -------------------------------------------------------------------- stats


def test_wilson_interval_brackets_the_point_estimate() -> None:
    interval = wilson_interval(5, 10)
    assert interval.point == 0.5
    assert interval.low < 0.5 < interval.high


def test_wilson_interval_of_an_empty_sample() -> None:
    interval = wilson_interval(0, 0)
    assert interval.trials == 0
    assert interval.low == 0.0


def test_exact_mcnemar_on_discordant_pairs() -> None:
    pairs = [(True, False)] * 4 + [(False, True)] * 1 + [(True, True)] * 3
    result = exact_mcnemar(pairs)
    assert result.b == 4
    assert result.c == 1
    assert result.discordant == 5
    assert 0.0 < result.p_value < 1.0


def test_exact_mcnemar_without_discordant_pairs() -> None:
    result = exact_mcnemar([(True, True), (False, False)])
    assert result.discordant == 0
    assert result.p_value == 1.0


def test_paired_difference_counts_the_outcomes() -> None:
    pairs = [(True, False), (False, True), (True, True), (False, False)]
    result = paired_difference(pairs)
    assert result["pairs"] == 4
    assert result["both"] == 1
    assert result["neither"] == 1
    assert result["difference"] == 0.0


# ------------------------------------------------------------------- gates


def _record(condition: str, passed: bool, failure_class: str = "", signals: list[str] | None = None):
    return {
        "task_id": "t",
        "split": "held-out",
        "condition": condition,
        "seed": 1,
        "passed": passed,
        "eligible": True,
        "failure_class": failure_class,
        "failure_signals": signals or ([failure_class] if failure_class else []),
    }


def test_g0_passes_when_more_than_half_the_failures_are_addressable() -> None:
    records = [
        _record("baseline", False, "M"),
        _record("baseline", False, "L"),
        _record("baseline", False, "S"),
        _record("baseline", False, "W"),
        _record("baseline", True),
    ]
    verdict = evaluate_g0(records)
    assert verdict["verdict"] == "pass"
    assert verdict["attributable_to_omlsl"] == 3
    assert verdict["residual_w"] == 1


def test_g0_fails_when_w_dominates() -> None:
    records = [
        _record("baseline", False, "W"),
        _record("baseline", False, "W"),
        _record("baseline", False, "W"),
        _record("baseline", False, "M"),
    ]
    verdict = evaluate_g0(records)
    assert verdict["verdict"] == "fail"
    assert verdict["attributable_ratio"] == 0.25


def test_g0_excludes_upstream_rejections_from_the_denominator() -> None:
    records = [
        _record("baseline", False, "W"),
        _record("baseline", False, "M"),
        _record("baseline", False, "R"),
    ]
    verdict = evaluate_g0(records)
    assert verdict["eligible_baseline_failures"] == 2
    assert verdict["upstream_rejections_reported_separately"] == 1


def test_g1_pass_stop_and_diagnose_thresholds() -> None:
    def records(baseline: int, harness: int, total: int = 10):
        out = []
        for index in range(total):
            out.append(_record("baseline", index < baseline))
            out.append(_record("harness", index < harness))
        return out

    assert evaluate_g1(records(2, 6))["verdict"] == "pass"
    assert evaluate_g1(records(5, 6))["verdict"] == "stop"
    assert evaluate_g1(records(4, 6))["verdict"] == "diagnose"


def test_the_gate_report_lists_every_gate() -> None:
    report = evaluate_gates([_record("baseline", True)], task_count=6)
    names = [gate["gate"] for gate in report["gates"]]
    assert names == ["G0", "G1", "public_release"]


# ------------------------------------------------------------------- tasks


def test_the_task_suite_is_loaded_and_split() -> None:
    tasks = load_repo_tasks(TASKS_DIR)
    assert tasks, "the repository task suite must not be empty"
    splits = {task.split for task in tasks}
    assert "dev" in splits
    assert "held-out" in splits


def test_every_task_has_a_snapshot_a_prompt_and_a_test_command() -> None:
    for task in load_repo_tasks(TASKS_DIR):
        assert task.prompt.strip()
        assert task.test_command
        assert task.snapshot.is_dir()
        assert "{python}" not in " ".join(task.test_command)


def test_the_suite_hash_is_stable() -> None:
    tasks = load_repo_tasks(TASKS_DIR)
    assert suite_hash(tasks) == suite_hash(load_repo_tasks(TASKS_DIR))


def test_the_forbidden_path_rule_catches_a_test_edit(tmp_path: Path) -> None:
    changed = ["tests/test_calc.py"]
    assert _forbidden_hit(changed, ["tests/"]) == "tests/test_calc.py"
    assert _forbidden_hit(changed, ["src/"]) is None


def test_changed_files_ignores_the_caches(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    worktree = tmp_path / "worktree"
    snapshot.mkdir()
    worktree.mkdir()
    (snapshot / "a.py").write_text("x = 1\n", encoding="utf-8")
    (worktree / "a.py").write_text("x = 2\n", encoding="utf-8")
    cache = worktree / "__pycache__"
    cache.mkdir()
    (cache / "a.pyc").write_bytes(b"junk")
    assert _changed_files(snapshot, worktree) == ["a.py"]


# ------------------------------------------------------------------- agent


def test_the_workspace_listing_is_deterministic_and_relative(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("", encoding="utf-8")
    (tmp_path / "top.py").write_text("", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_bytes(b"junk")
    agent = ScriptedAgent(
        base_url="http://x.test",
        model="m",
        workdir=tmp_path,
        test_command=["true"],
        client=httpx.AsyncClient(),
    )
    listing = agent.workspace_listing()
    assert listing == "pkg/mod.py\ntop.py"
    assert agent.workspace_listing() == listing


def test_validate_tool_call_reports_the_failure_class() -> None:
    schemas = {"t": {"type": "object", "properties": {"p": {"type": "string"}}, "required": ["p"]}}
    assert validate_tool_call(schemas, "t", '{"p": "x"}') == (True, "")
    assert validate_tool_call(schemas, "t", "{not json") == (False, "M.parse")
    assert validate_tool_call(schemas, "t", "{}") == (False, "M.schema")
    assert validate_tool_call(schemas, "other", "{}") == (False, "M.name")


def test_the_toolbox_refuses_to_escape_the_worktree(tmp_path: Path) -> None:
    toolbox = ToolBox(tmp_path, ["true"])
    outcome = toolbox.execute("write_file", {"path": "../escape.py", "content": "x"})
    assert outcome.ok is False
    assert outcome.error_class == "path.invalid"
    assert not (tmp_path.parent / "escape.py").exists()


def test_the_toolbox_reads_and_writes_inside_the_worktree(tmp_path: Path) -> None:
    toolbox = ToolBox(tmp_path, ["true"])
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    assert toolbox.execute("read_file", {"path": "a.py"}).content == "hello\n"
    write = toolbox.execute("write_file", {"path": "b.py", "content": "x = 1\n"})
    assert write.ok is True
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "x = 1\n"


def test_the_agent_shows_the_workspace_to_the_model(tmp_path: Path) -> None:
    """The first user message must contain the workspace listing."""
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "1",
                "object": "chat.completion",
                "model": "m",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "done"}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            agent = ScriptedAgent(
                base_url="http://upstream.test",
                model="m",
                workdir=tmp_path,
                test_command=["true"],
                client=client,
            )
            await agent.run("fix the bug")

    import asyncio

    asyncio.run(run())
    assert seen
    first_user = seen[0]["messages"][1]["content"]
    assert "a.py" in first_user
    assert "fix the bug" in first_user


def test_the_agent_stops_at_the_step_limit(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "1",
                "object": "chat.completion",
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "run_tests", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
                "usage": {},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            agent = ScriptedAgent(
                base_url="http://upstream.test",
                model="m",
                workdir=tmp_path,
                test_command=["true"],
                client=client,
                max_steps=3,
            )
            return await agent.run("go")

    import asyncio

    result = asyncio.run(run())
    assert result.stop_reason == "step_limit"
    assert result.steps == 3
