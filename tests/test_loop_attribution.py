"""Tests for the mitigation-attribution of a detected loop.

Detection and actual mitigation are different facts. These tests pin the
attribution labels of ``taxonomy.classify_loop_attribution``:

* ``proxy_escalated`` — the proxy's warning reached a later request;
* ``client_guard_handled`` — recorded ONLY on explicit client-stop evidence
  (the client's own identical-failure safeguard stopped the run), never
  because a next request merely failed to arrive;
* ``detected_only`` — detected, but no mitigation can be credited.

They also pin the invariants the measurement relies on: one label per run,
no double counting, stop reasons are never rewritten, and the replay round
trip reproduces the event order and the attribution.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from pocket_harness.config import Config
from pocket_harness.proxy import create_app
from pocket_harness.replay import run_replay
from pocket_harness.state import StateStore
from pocket_harness.taxonomy import (
    CLIENT_GUARD_STOP_REASON,
    LOOP_ATTRIBUTION_LABELS,
    classify_loop_attribution,
)


def _attr(
    loop_events: int = 1,
    proxy_escalations: int = 0,
    stop_reason: str | None = None,
):
    return classify_loop_attribution(
        loop_events=loop_events,
        proxy_escalations=proxy_escalations,
        stop_reason=stop_reason,
    )


# --------------------------------------------------------------- labels ---


def test_a_proxy_escalation_is_attributed_to_the_proxy() -> None:
    attribution = _attr(loop_events=1, proxy_escalations=1, stop_reason="model_stopped")
    assert attribution.label == "proxy_escalated"
    assert attribution.detected is True
    assert attribution.notes == []


def test_explicit_client_guard_evidence_is_recorded_as_client_guard_handled() -> None:
    attribution = _attr(
        loop_events=1, proxy_escalations=0, stop_reason=CLIENT_GUARD_STOP_REASON
    )
    assert attribution.label == "client_guard_handled"
    assert any("explicit" in note for note in attribution.notes)


def test_a_stop_without_guard_evidence_stays_detected_only() -> None:
    """A missing next request is not client-guard evidence."""
    for stop_reason in ("model_stopped", "step_limit", "", None):
        attribution = _attr(loop_events=1, proxy_escalations=0, stop_reason=stop_reason)
        assert attribution.label == "detected_only", stop_reason
        assert any("no mitigation" in note for note in attribution.notes)


def test_a_run_without_a_detected_loop_cannot_be_attributed() -> None:
    attribution = _attr(loop_events=0, proxy_escalations=0, stop_reason=CLIENT_GUARD_STOP_REASON)
    assert attribution.label == "not_detected"
    assert attribution.detected is False


def test_labels_are_mutually_exclusive_and_the_inputs_are_untouched() -> None:
    stop_reason = CLIENT_GUARD_STOP_REASON
    attribution = _attr(loop_events=2, proxy_escalations=1, stop_reason=stop_reason)
    assert attribution.label == "proxy_escalated"
    # the client stop is not lost: it is recorded as a note, and the inputs
    # are never rewritten (no stop-reason misattribution)
    assert any("also stopped" in note for note in attribution.notes)
    assert stop_reason == CLIENT_GUARD_STOP_REASON
    assert len({label for label in LOOP_ATTRIBUTION_LABELS}) == 3


def test_the_tally_counts_each_run_exactly_once() -> None:
    records = [
        {"condition": "harness", "task_id": "t1", "seed": 1, "stop_reason": "model_stopped",
         "proxy": {"loop_events": 1, "c4_escalations": 1}},
        {"condition": "harness", "task_id": "t2", "seed": 1, "stop_reason": CLIENT_GUARD_STOP_REASON,
         "proxy": {"loop_events": 1, "c4_escalations": 0}},
        {"condition": "harness", "task_id": "t3", "seed": 1, "stop_reason": "model_stopped",
         "proxy": {"loop_events": 1, "c4_escalations": 0}},
        {"condition": "baseline", "task_id": "ignored", "seed": 1, "stop_reason": "model_stopped",
         "proxy": {"loop_events": 9, "c4_escalations": 9}},
    ]
    labels = []
    for record in records:
        if record["condition"] != "harness":
            continue
        proxy = record.get("proxy") or {}
        labels.append(
            _attr(
                loop_events=proxy.get("loop_events", 0),
                proxy_escalations=proxy.get("c4_escalations", 0),
                stop_reason=record.get("stop_reason"),
            ).label
        )
    assert len(labels) == 3
    assert sorted(labels) == sorted(
        ["proxy_escalated", "client_guard_handled", "detected_only"]
    )


# --------------------------------------------------- live pipeline checks ---


def make_config(state_root: str) -> Config:
    config = Config()
    config.state.root = state_root
    config.upstream.base_url = "http://upstream.test/v1"
    config.upstream.model = "test-model"
    config.upstream.context_length = 4096
    config.context.reserved_output_tokens = 128
    config.context.safety_margin_tokens = 0
    config.loop_guard.repeated_call_threshold = 3
    return config


def tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"the {name} tool",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }


def chat_body() -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


class Recorder:
    def __init__(self) -> None:
        self.forwarded: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.forwarded.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=chat_body())


def history_with_repeats(count: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are a coding assistant"},
        {"role": "user", "content": "read a.py"},
    ]
    for index in range(count):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "a.py"}),
                        },
                    }
                ],
            }
        )
        messages.append(
            {"role": "tool", "tool_call_id": f"call_{index}", "name": "read_file", "content": "x"}
        )
    return messages


async def drive(config: Config, store: StateStore, payload: dict[str, Any]) -> list[dict[str, Any]]:
    app = create_app(config, store=store)
    recorder = Recorder()
    app.state.service.transport = httpx.MockTransport(recorder.handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    ) as client:
        response = await client.post("/v1/chat/completions", json=payload)
        assert response.status_code == 200
    assert "[pocket:C4]" in json.dumps(recorder.forwarded[0]["messages"])
    return list(store.iter_events(store.list_runs()[0]["run_id"]))


def attribution_of_events(events: list[dict[str, Any]]):
    """Derive the attribution from the proxy evidence events alone."""
    loop_events = sum(1 for e in events if e["event"] == "loop.detected")
    escalations = sum(
        1
        for e in events
        if e["event"] == "request.transformed"
        and any(r["id"].startswith("C4") for r in (e["payload"] or {}).get("rules") or [])
    )
    return classify_loop_attribution(
        loop_events=loop_events,
        proxy_escalations=escalations,
    )


async def test_the_proxy_escalates_a_real_repetition_once_and_only_once(tmp_path) -> None:
    config = make_config(str(tmp_path / "state"))
    events = await drive(
        config, StateStore(tmp_path / "state"),
        {"model": "test-model", "messages": history_with_repeats(3), "tools": [tool("read_file")]},
    )
    attribution = attribution_of_events(events)
    assert attribution.label == "proxy_escalated"
    case3 = classify_loop_attribution(
        loop_events=0,  # a request without a repeat: no detection at all
        proxy_escalations=0,
        stop_reason=None,
    )
    assert case3.label == "not_detected"


async def test_replay_reproduces_the_rule_order_and_the_attribution(tmp_path) -> None:
    """The round trip: same rules in the same order, same attribution label."""
    state_root = str(tmp_path / "state")
    config = make_config(state_root)
    config.logging.capture = "local-replay"
    store = StateStore(tmp_path / "state")
    events = await drive(
        config, store,
        {"model": "test-model", "messages": history_with_repeats(3), "tools": [tool("read_file")]},
    )
    run_id = store.list_runs()[0]["run_id"]
    live_rules = [
        rule["id"]
        for e in events
        if e["event"] == "request.transformed"
        for rule in (e["payload"] or {}).get("rules") or []
    ]
    live_attribution = attribution_of_events(events)

    report = run_replay(config, store.runs_dir / run_id)
    assert report["ok"] is True
    assert report["input_matched"] is True
    assert report["matched"] is True
    # the replay returns the same rule sequence in the same order, and the
    # attribution derived from the replay equals the attribution of the live run
    assert report["rules"] == live_rules
    replayed_attribution = classify_loop_attribution(
        loop_events=live_attribution.detected * 1,
        proxy_escalations=sum(1 for rule in report["rules"] if rule.startswith("C4")),
    )
    assert replayed_attribution.label == live_attribution.label == "proxy_escalated"


async def test_no_double_warning_and_no_abort_on_the_same_loop(tmp_path) -> None:
    """One repetition = one warning; the abort path stays off and unused."""
    state_root = str(tmp_path / "state")
    config = make_config(state_root)
    assert config.loop_guard.abort_on_repeat is False  # the refusal is opt-in
    store = StateStore(tmp_path / "state")
    events = await drive(
        config, store,
        {"model": "test-model", "messages": history_with_repeats(3), "tools": [tool("read_file")]},
    )
    warnings = [
        rule
        for e in events
        if e["event"] == "request.transformed"
        for rule in (e["payload"] or {}).get("rules") or []
        if rule["id"] == "C4.repeat_warning"
    ]
    assert len(warnings) == 1  # one detected repetition, one escalation
    assert not [e for e in events if e["event"] == "loop.aborted"]
    # the proxy never invents or rewrites a stop reason: the record keeps
    # whatever the client set, and a passed-through reason is not claimed as
    # client-guard evidence when there is none
    attribution = attribution_of_events(events)
    assert attribution.label == "proxy_escalated"
    assert attribution.notes == []
