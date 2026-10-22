"""``pocket doctor`` — a deterministic pre-flight check.

The doctor verifies, in order:

1. the upstream is reachable and speaks the OpenAI-compatible protocol,
2. the configured model is advertised by the upstream,
3. the context window is known (C2 cannot enforce a budget without it),
4. a minimal chat completion succeeds,
5. a streamed completion succeeds and the SSE framing is well formed,
6. a tool call round trip works — the model is asked to call one specific tool and
   the proxy checks that the arguments parse and validate against the schema.

Every check reports what was observed. A failing check never triggers a fallback.
"""

from __future__ import annotations

import json
import time
from typing import Any

from pocket_harness.config import Config
from pocket_harness.upstream import UpstreamClient, UpstreamError, UpstreamTarget

DOCTOR_TOOL = {
    "type": "function",
    "function": {
        "name": "pocket_doctor_probe",
        "description": "Report the current time. Used by Pocket Harness to verify the tool call round trip.",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {"type": "string", "description": "IANA timezone name, e.g. UTC"}
            },
            "required": ["timezone"],
        },
    },
}


def _target(config: Config) -> UpstreamTarget:
    return UpstreamTarget(
        base_url=config.upstream.base_url,
        model=config.upstream.model,
        api_key=config.upstream.resolved_api_key(),
        extra_headers=config.upstream.extra_headers,
    )


def _check(name: str, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    entry = {"check": name, "ok": ok, "detail": detail}
    entry.update(extra)
    return entry


async def run_doctor(config: Config) -> dict[str, Any]:
    """Run every doctor check and return a report."""
    checks: list[dict[str, Any]] = []
    client = UpstreamClient(_target(config), timeout=config.upstream.timeout_seconds)

    # 1) reachability + model list
    models: list[str] = []
    try:
        models = await client.list_models()
        checks.append(
            _check(
                "upstream.reachable",
                True,
                f"the upstream answered on {config.upstream.base_url}",
                model_count=len(models),
            )
        )
    except UpstreamError as exc:
        checks.append(
            _check("upstream.reachable", False, f"the upstream returned HTTP {exc.status_code}")
        )
        return _finalize(checks, config)
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("upstream.reachable", False, f"{type(exc).__name__}: {exc}"))
        return _finalize(checks, config)

    # 2) model availability
    if config.upstream.model:
        if config.upstream.model in models:
            checks.append(
                _check("model.available", True, f"the upstream advertises {config.upstream.model}")
            )
        else:
            checks.append(
                _check(
                    "model.available",
                    False,
                    f"the upstream does not advertise {config.upstream.model}",
                    advertised=models[:20],
                )
            )
    else:
        checks.append(_check("model.available", False, "no upstream model is configured"))

    # 3) context window
    context_length = config.effective_context_length()
    if context_length:
        checks.append(
            _check(
                "context.known",
                True,
                f"the context window is {context_length} tokens",
                context_length=context_length,
            )
        )
    else:
        checks.append(
            _check(
                "context.known",
                False,
                "no context length is configured; C2 cannot enforce a budget",
            )
        )

    # 4) unary completion
    try:
        started = time.monotonic()
        _, collector = await client.create_chat(
            {
                "model": config.upstream.model,
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                "max_tokens": 16,
            }
        )
        checks.append(
            _check(
                "chat.unary",
                True,
                "a non-streaming completion succeeded",
                latency_seconds=round(time.monotonic() - started, 4),
                content_length=len(collector.text),
            )
        )
    except UpstreamError as exc:
        checks.append(_check("chat.unary", False, f"the upstream returned HTTP {exc.status_code}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("chat.unary", False, f"{type(exc).__name__}: {exc}"))

    # 5) streaming completion
    checks.append(await _check_stream(client, config))

    # 6) tool call round trip
    checks.append(await _check_tool_round_trip(client, config))

    return _finalize(checks, config)


async def _check_stream(client: UpstreamClient, config: Config) -> dict[str, Any]:
    """Verify that a streamed completion is well formed."""
    try:
        started = time.monotonic()
        saw_done = False
        chunks = 0
        malformed = False
        async for line in client.stream_chat(
            {
                "model": config.upstream.model,
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                "max_tokens": 16,
            }
        ):
            stripped = line.strip()
            if not stripped:
                continue
            chunks += 1
            if stripped.startswith("data:"):
                body = stripped[5:].strip()
                if body == "[DONE]":
                    saw_done = True
                else:
                    try:
                        json.loads(body)
                    except json.JSONDecodeError:
                        malformed = True
                        break
        if malformed:
            return _check("chat.stream", False, "the upstream emitted a malformed SSE payload")
        return _check(
            "chat.stream",
            saw_done and chunks > 0,
            "a streaming completion succeeded" if saw_done else "the stream ended without [DONE]",
            chunks=chunks,
            latency_seconds=round(time.monotonic() - started, 4),
        )
    except UpstreamError as exc:
        return _check("chat.stream", False, f"the upstream returned HTTP {exc.status_code}")
    except Exception as exc:  # noqa: BLE001
        return _check("chat.stream", False, f"{type(exc).__name__}: {exc}")


async def _check_tool_round_trip(client: UpstreamClient, config: Config) -> dict[str, Any]:
    """Verify that a tool call is produced and that its arguments validate."""
    from pocket_harness.components.tool_call_repair import ToolCallRepairer

    try:
        _, collector = await client.create_chat(
            {
                "model": config.upstream.model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Call the pocket_doctor_probe tool with timezone set to UTC. "
                            "Do not answer in text; only make the tool call."
                        ),
                    }
                ],
                "tools": [DOCTOR_TOOL],
                "tool_choice": {"type": "function", "function": {"name": "pocket_doctor_probe"}},
                "max_tokens": 128,
            }
        )
    except UpstreamError as exc:
        return _check("tools.round_trip", False, f"the upstream returned HTTP {exc.status_code}")
    except Exception as exc:  # noqa: BLE001
        return _check("tools.round_trip", False, f"{type(exc).__name__}: {exc}")

    calls = collector.tool_call_list()
    if not calls:
        return _check(
            "tools.round_trip",
            False,
            "the model did not produce a tool call for an explicit tool_choice",
        )

    repairer = ToolCallRepairer(
        {"pocket_doctor_probe": DOCTOR_TOOL["function"]["parameters"]},
        allow_fuzzy_tool_name=config.repair.allow_fuzzy_tool_name,
    )
    outcomes = []
    all_ok = True
    for call in calls:
        outcome = repairer.repair(call.name, call.arguments)
        outcomes.append(outcome.as_event_dict())
        if not outcome.ok:
            all_ok = False

    return _check(
        "tools.round_trip",
        all_ok,
        "the model produced a tool call whose arguments validate against the schema"
        if all_ok
        else "the model's tool call did not validate against the schema",
        outcomes=outcomes,
    )


def _finalize(checks: list[dict[str, Any]], config: Config) -> dict[str, Any]:
    ok = all(check["ok"] for check in checks)
    return {
        "ok": ok,
        "upstream": config.upstream.base_url,
        "model": config.upstream.model,
        "context_length": config.effective_context_length(),
        "checks": checks,
    }


