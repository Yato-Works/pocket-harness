"""``pocket diagnose`` — run the deterministic mechanism scenarios.

The scenarios create each mechanism's condition on purpose and check its
obligation. The result is the **diagnostic track**: it says whether C1, C2, C3
and C4 did their job when their condition arose. It never says whether a
coding task got completed — that is the gate evaluation.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from pocket_harness.config import Config
from pocket_harness.mechanism_eval import MechanismObligation
from pocket_harness.proxy import create_app
from pocket_harness.scenarios import Scenario, apply_overrides, build_scenarios
from pocket_harness.state import StateStore


async def _run_scenario(
    scenario: Scenario,
    base_config: Config,
    root: str,
) -> dict[str, Any]:
    """Run one scenario against the proxy and check its obligation."""
    import copy

    config = copy.deepcopy(base_config)
    config.state.root = f"{root}/{scenario.name}"
    config.upstream.base_url = "http://upstream.test/v1"
    config.upstream.model = "m"
    config.upstream.context_length = 4096
    config.context.reserved_output_tokens = 128
    config.context.safety_margin_tokens = 0
    config.tools.max_non_pinned = 3
    apply_overrides(config, scenario.config_overrides)

    forwarded: list[dict[str, Any]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=scenario.upstream)

    store = StateStore(config.state.root)
    app = create_app(config, store=store)
    app.state.service.transport = httpx.MockTransport(upstream)
    started = time.perf_counter()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    ) as client:
        response = await client.post("/v1/chat/completions", json=scenario.payload)
    elapsed = (time.monotonic() - started) * 1000.0

    events = list(store.iter_events(store.list_runs()[0]["run_id"]))
    rules: list[str] = []
    for event in events:
        if event["event"] != "request.transformed":
            continue
        for rule in event["payload"].get("rules") or []:
            rules.append(str(rule.get("id")))
    validated = [event["payload"] for event in events if event["event"] == "toolcall.validated"]

    client_body = response.json()
    client_calls = (
        (client_body.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
    )
    client_arguments = (
        json.loads(client_calls[0]["function"]["arguments"]) if client_calls else None
    )

    forwarded_body = forwarded[0] if forwarded else {}
    forwarded_messages = forwarded_body.get("messages") or []
    system_ok = True
    pairs_ok = True
    if forwarded_messages:
        original_system = next(
            (m for m in scenario.payload["messages"] if m.get("role") == "system"), None
        )
        if original_system is not None:
            system_ok = any(
                m.get("role") == "system" and m.get("content") == original_system.get("content")
                for m in forwarded_messages
            )
        call_ids = {
            call.get("id")
            for message in forwarded_messages
            for call in (message.get("tool_calls") or [])
        }
        result_ids = {
            m.get("tool_call_id")
            for m in forwarded_messages
            if m.get("role") == "tool"
        }
        pairs_ok = not (call_ids - result_ids)

    expect = scenario.expect
    checks: dict[str, bool] = {}
    measured: dict[str, Any] = {
        "status_code": response.status_code,
        "rules": rules,
        "forwarded_tools": len(forwarded_body.get("tools") or []),
        "forwarded_messages": len(forwarded_messages),
        "system_prompt_unchanged": system_ok,
        "tool_result_pairs_valid": pairs_ok,
        "client_arguments": client_arguments,
        "wall_ms": round(elapsed, 3),
    }
    if "max_forwarded_tools" in expect:
        checks["forwarded_tools"] = (
            measured["forwarded_tools"] <= expect["max_forwarded_tools"]
        )
    if "rule_prefixes" in expect:
        checks["rule_prefixes"] = all(
            any(rule.startswith(prefix) for rule in rules) for prefix in expect["rule_prefixes"]
        )
    if "repaired" in expect:
        repaired = sum(int((payload or {}).get("repaired") or 0) for payload in validated)
        measured["repaired"] = repaired
        checks["repaired"] = repaired == expect["repaired"]
    if "failed" in expect:
        failed = sum(int((payload or {}).get("failed") or 0) for payload in validated)
        measured["failed"] = failed
        checks["failed"] = failed == expect["failed"]
    if "client_arguments" in expect:
        checks["client_arguments"] = client_arguments == expect["client_arguments"]
    if expect.get("system_prompt_unchanged"):
        checks["system_prompt_unchanged"] = system_ok
    if expect.get("pairs_valid"):
        checks["tool_result_pairs_valid"] = pairs_ok
    if expect.get("warning_in_request"):
        has_warning = any(
            "[pocket:C4]" in str(message.get("content") or "")
            for message in forwarded_messages
        )
        measured["warning_in_request"] = has_warning
        checks["warning_in_request"] = has_warning

    satisfied = all(checks.values()) if checks else None
    return {
        "scenario": scenario.name,
        "mechanism": scenario.mechanism,
        "condition": scenario.condition,
        "obligation": scenario.obligation,
        "satisfied": satisfied,
        "checks": checks,
        "measured": measured,
    }


async def run_scenarios(config: Config, root: str = ".e2e/diagnose") -> dict[str, Any]:
    """Run every scenario and return the diagnostic report."""
    import shutil
    from pathlib import Path

    shutil.rmtree(root, ignore_errors=True)
    Path(root).mkdir(parents=True, exist_ok=True)

    results = []
    for scenario in build_scenarios():
        results.append(await _run_scenario(scenario, config, root))

    obligations = [
        MechanismObligation(
            mechanism=result["mechanism"],
            condition=result["condition"],
            obligation=result["obligation"],
            measured=result["measured"],
            satisfied=result["satisfied"],
            note="" if result["satisfied"] else "the obligation was not met",
        ).to_dict()
        for result in results
    ]
    return {
        "track": "A",
        "name": "mechanism_diagnostics",
        "scenarios": results,
        "obligations": obligations,
        "summary": {
            "scenarios": len(results),
            "satisfied": sum(1 for result in results if result["satisfied"]),
            "failed": sum(1 for result in results if result["satisfied"] is False),
        },
        "note": (
            "Deterministic mechanism scenarios. They show whether a mechanism did "
            "its job when its condition arose. They say nothing about task "
            "completion, which is the gate evaluation (track B)."
        ),
    }


__all__ = ["run_scenarios"]
