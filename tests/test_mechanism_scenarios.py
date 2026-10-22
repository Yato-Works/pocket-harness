"""Tests for the diagnostic track: the mechanism scenarios.

The scenarios create each mechanism's condition on purpose. A scenario that
passes says that the mechanism did its job; it never says that a coding task
got completed.
"""

from __future__ import annotations

import json

import pytest

from pocket_harness.config import Config
from pocket_harness.diagnose import run_scenarios
from pocket_harness.mechanism_eval import compare_tracks, evaluate_mechanisms


@pytest.fixture()
def config(tmp_path) -> Config:
    config = Config()
    config.state.root = str(tmp_path / "state")
    config.upstream.base_url = "http://upstream.test/v1"
    config.upstream.model = "m"
    return config


async def test_every_scenario_satisfies_its_obligation(config, tmp_path) -> None:
    report = await run_scenarios(config, root=str(tmp_path / "scenarios"))
    failing = [
        result["scenario"]
        for result in report["scenarios"]
        if result["satisfied"] is not True
    ]
    assert not failing, failing
    assert report["summary"]["satisfied"] == report["summary"]["scenarios"]


async def test_the_scenarios_cover_every_mechanism(config, tmp_path) -> None:
    report = await run_scenarios(config, root=str(tmp_path / "scenarios"))
    mechanisms = {result["mechanism"] for result in report["scenarios"]}
    assert mechanisms == {"C1", "C2", "C3", "C4"}


async def test_the_c2_scenario_keeps_the_system_prompt(config, tmp_path) -> None:
    report = await run_scenarios(config, root=str(tmp_path / "scenarios"))
    scenario = next(r for r in report["scenarios"] if r["scenario"] == "c2-context-budget")
    assert scenario["measured"]["system_prompt_unchanged"] is True
    assert scenario["measured"]["tool_result_pairs_valid"] is True


async def test_the_c3_scenario_does_not_invent_arguments(config, tmp_path) -> None:
    report = await run_scenarios(config, root=str(tmp_path / "scenarios"))
    scenario = next(r for r in report["scenarios"] if r["scenario"] == "c3-unrepairable")
    assert scenario["measured"]["failed"] == 1
    assert scenario["measured"]["repaired"] == 0
    assert scenario["measured"]["client_arguments"] == {}


def test_the_diagnostic_track_never_makes_a_completion_claim() -> None:
    records = [
        {
            "task_id": "t",
            "condition": "harness",
            "seed": 1,
            "passed": False,
            "eligible": True,
            "failure_class": "M",
            "failure_signals": ["M"],
            "proxy": {
                "tool_calls_checked": 3,
                "tool_calls_repaired": 3,
                "invalid_tool_calls_after_c3": 0,
                "compression_events": 0,
                "tool_selection_events": 0,
                "c4_escalations": 0,
                "loop_events": 0,
            },
            "invalid_tool_calls_before_c3": 3,
        }
    ]
    report = evaluate_mechanisms(records)
    assert report["track"] == "A"
    assert report["summary"]["tool_calls_repaired"] == 3
    assert "completion" in report["note"].lower()
    # The diagnostic track has no completion number at all.
    assert "completion_rate" not in json.dumps(report)


def test_the_two_tracks_are_reported_separately() -> None:
    records = [
        {
            "task_id": "t",
            "condition": "harness",
            "split": "held-out",
            "seed": 1,
            "passed": True,
            "eligible": True,
            "failure_class": "",
            "failure_signals": [],
            "proxy": {},
            "invalid_tool_calls_before_c3": 0,
        },
        {
            "task_id": "t",
            "condition": "baseline",
            "split": "held-out",
            "seed": 1,
            "passed": False,
            "eligible": True,
            "failure_class": "W",
            "failure_signals": ["W"],
            "proxy": {},
            "invalid_tool_calls_before_c3": 0,
        },
    ]
    both = compare_tracks(records)
    assert both["track_a_mechanism_diagnostics"]["track"] == "A"
    assert both["track_b_task_completion"]["gates"]
    assert "never substitutes" in both["how_to_read_this"]
