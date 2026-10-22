"""Do the mechanisms actually fire where they are needed?

The unit tests of C1–C4 check the components in isolation. These tests check
the thing that matters for the performance claim: **in the real pipeline, a
condition that the mechanism targets makes the mechanism fire, with the right
result and the right invariants.**

Each test builds the condition deliberately:

* C1 — more tools than the cap;
* C2 — a request above the compression threshold;
* C3 — a malformed tool call;
* C4 — a repeated call and an A-B-A-B oscillation.

The upstream is a mock, so the forwarded request can be inspected and the
result does not depend on a model's behaviour.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pocket_harness.config import Config
from pocket_harness.proxy import create_app
from pocket_harness.state import StateStore


def make_config() -> Config:
    config = Config()
    config.upstream.base_url = "http://upstream.test/v1"
    config.upstream.model = "test-model"
    config.upstream.context_length = 4096
    config.context.reserved_output_tokens = 128
    config.context.safety_margin_tokens = 0
    config.tools.max_non_pinned = 3
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


def chat_body(
    content: str = "ok", tool_calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


class Recorder:
    """A mock upstream that records what the proxy forwarded."""

    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self.body = body or chat_body()
        self.forwarded: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.forwarded.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=self.body)


async def drive(
    config: Config,
    store: StateStore,
    payload: dict[str, Any],
    recorder: Recorder,
) -> tuple[httpx.Response, list[dict[str, Any]]]:
    app = create_app(config, store=store)
    app.state.service.transport = httpx.MockTransport(recorder.handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    ) as client:
        response = await client.post("/v1/chat/completions", json=payload)
    events = list(store.iter_events(store.list_runs()[0]["run_id"]))
    return response, events


def events_of(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [event for event in events if event["event"] == name]


@pytest.fixture()
def config() -> Config:
    return make_config()


@pytest.fixture()
def store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state")


# ------------------------------------------------------------------- C1 ---


async def test_c1_fires_when_there_are_more_tools_than_the_cap(config, store) -> None:
    """Ten tools, a cap of three: the forwarded request must expose three."""
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "do the thing"}],
        "tools": [tool(f"tool_{index}") for index in range(10)],
    }
    response, events = await drive(config, store, payload, recorder)

    assert response.status_code == 200
    forwarded = recorder.forwarded[0]
    exposed = [entry["function"]["name"] for entry in forwarded["tools"]]
    assert len(exposed) == config.tools.max_non_pinned

    transformed = events_of(events, "request.transformed")
    assert transformed
    rules = {rule["id"] for rule in transformed[0]["payload"]["rules"]}
    assert "C1.select" in rules
    select = next(r for r in transformed[0]["payload"]["rules"] if r["id"] == "C1.select")
    assert len(select["hidden"]) == 7
    assert set(select["exposed"]) == set(exposed)


async def test_c1_does_not_fire_below_the_cap(config, store) -> None:
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "do the thing"}],
        "tools": [tool("read_file"), tool("write_file")],
    }
    await drive(config, store, payload, recorder)
    assert len(recorder.forwarded[0]["tools"]) == 2


async def test_c1_keeps_pinned_tools_even_when_the_cap_is_exceeded(config, store) -> None:
    config.client.pinned_tools = ["write_file", "run_tests"]
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "do the thing"}],
        "tools": [tool(f"tool_{index}") for index in range(10)]
        + [tool("write_file"), tool("run_tests")],
    }
    await drive(config, store, payload, recorder)
    exposed = {entry["function"]["name"] for entry in recorder.forwarded[0]["tools"]}
    assert {"write_file", "run_tests"} <= exposed


async def test_c1_is_not_an_authorisation_boundary(config, store) -> None:
    """A call to a tool that C1 hid is still validated, not rejected."""
    recorder = Recorder(
        chat_body(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "hidden_tool", "arguments": '{"path": "a.py"}'},
                }
            ]
        )
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "do the thing"}],
        "tools": [tool(f"tool_{index}") for index in range(10)]
        + [tool("hidden_tool")],
    }
    response, events = await drive(config, store, payload, recorder)
    assert response.status_code == 200
    validated = events_of(events, "toolcall.validated")
    assert validated
    payload_out = validated[-1]["payload"]
    # The name resolved against the client's original list, so the call is not
    # an error just because C1 hid the tool.
    assert payload_out["failed"] == 0


async def test_c1_selection_is_sticky_across_requests(config, store) -> None:
    """The second request keeps the tools the first one exposed."""
    recorder = Recorder()
    app = create_app(config, store=store)
    app.state.service.transport = httpx.MockTransport(recorder.handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    ) as client:
        payload = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "read the file"}],
            "tools": [tool(f"tool_{index}") for index in range(10)],
        }
        await client.post("/v1/chat/completions", json=payload)
        await client.post("/v1/chat/completions", json=payload)
    first = {entry["function"]["name"] for entry in recorder.forwarded[0]["tools"]}
    second = {entry["function"]["name"] for entry in recorder.forwarded[1]["tools"]}
    assert first == second


# ------------------------------------------------------------------- C2 ---


def _big_conversation(old_turns: int = 2, recent_turns: int = 4, size: int = 9000) -> list[dict[str, Any]]:
    """A conversation that is over the compression threshold but still fits.

    The *old* turns are huge (they are what C2 compresses) and the recent
    turns are small (C2 must keep them verbatim).
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are a coding assistant"}
    ]
    for index in range(old_turns):
        messages.append({"role": "user", "content": f"old question {index} " + "x" * size})
        messages.append(
            {
                "role": "assistant",
                "content": "old answer " + "y" * size,
                "tool_calls": [
                    {
                        "id": f"old_{index}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": f"old_{index}.py"}),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"old_{index}",
                "name": "read_file",
                "content": "z" * size,
            }
        )
    for index in range(recent_turns):
        messages.append({"role": "user", "content": f"recent question {index}"})
        messages.append(
            {
                "role": "assistant",
                "content": "recent answer",
                "tool_calls": [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": f"file_{index}.py"}),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "name": "read_file",
                "content": "small result",
            }
        )
    return messages


async def test_c2_fires_above_the_threshold(config, store) -> None:
    """A request above 85% of the budget must be compressed."""
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": _big_conversation(),
        "tools": [tool("read_file")],
    }
    await drive(config, store, payload, recorder)

    forwarded = recorder.forwarded[0]
    before = sum(len(json.dumps(message)) for message in payload["messages"])
    after = sum(len(json.dumps(message)) for message in forwarded["messages"])
    assert after < before

    events = list(store.iter_events(store.list_runs()[0]["run_id"]))
    transformed = events_of(events, "request.transformed")
    rules = {rule["id"] for rule in transformed[0]["payload"]["rules"]}
    assert any(rule.startswith("C2") for rule in rules), rules
    token_count = transformed[0]["payload"]["token_count"]
    assert token_count["after"] < token_count["before"]


async def test_c2_does_not_fire_below_the_threshold(config, store) -> None:
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "short question"}],
        "tools": [tool("read_file")],
    }
    await drive(config, store, payload, recorder)
    events = list(store.iter_events(store.list_runs()[0]["run_id"]))
    transformed = events_of(events, "request.transformed")
    rules = {rule["id"] for rule in transformed[0]["payload"]["rules"]}
    assert not any(rule.startswith("C2") for rule in rules), rules


async def test_c2_keeps_the_system_prompt_and_the_pairing(config, store) -> None:
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": _big_conversation(),
        "tools": [tool("read_file")],
    }
    await drive(config, store, payload, recorder)
    forwarded = recorder.forwarded[0]
    assert forwarded["messages"][0] == payload["messages"][0]

    # Every assistant tool call still has its result.
    call_ids = {
        call["id"]
        for message in forwarded["messages"]
        for call in (message.get("tool_calls") or [])
    }
    result_ids = {
        message.get("tool_call_id")
        for message in forwarded["messages"]
        if message.get("role") == "tool"
    }
    assert call_ids - result_ids == set()


async def test_c2_is_idempotent(config, store) -> None:
    """Applying the same policy to its own output changes nothing."""
    from pocket_harness.pipeline import Pipeline

    pipeline = Pipeline(config)
    canonical, _ = pipeline.normalize(
        {"model": "m", "messages": _big_conversation(), "tools": [tool("read_file")]}
    )
    once = pipeline.transform(canonical)
    twice = pipeline.transform(once.request)
    assert once.request.to_payload() == twice.request.to_payload()


# ------------------------------------------------------------------- C3 ---


async def test_c3_repairs_a_malformed_call_and_the_client_receives_it(config, store) -> None:
    """ADR 0001: the repaired arguments are what the client gets."""
    recorder = Recorder(
        chat_body(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '```json\n{"path": "a.py",}\n```',
                    },
                }
            ]
        )
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "read a.py"}],
        "tools": [tool("read_file")],
    }
    response, events = await drive(config, store, payload, recorder)
    assert response.status_code == 200

    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"path": "a.py"}

    validated = events_of(events, "toolcall.validated")
    outcome = validated[-1]["payload"]["outcomes"][0]
    assert outcome["ok"] is True
    # The *text* changed (the fence and the trailing comma are gone); the
    # parsed arguments are the same, so the semantic hash is unchanged.
    assert outcome["text_changed"] is True
    assert outcome["changed"] is False
    assert outcome["original_sha256"] == outcome["repaired_sha256"]
    assert outcome["original_text_sha256"] != outcome["repaired_sha256"]


async def test_c3_does_not_touch_a_call_that_is_already_usable(config, store) -> None:
    """A re-serialisation of valid JSON is not a repair.

    The model emitted ``{"path":"calc.py"}``; C3 re-serialises it as
    ``{"path": "calc.py"}``. The client must receive the model's own text and
    the evidence must not claim a repair.
    """
    recorder = Recorder(
        chat_body(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"calc.py"}'},
                }
            ]
        )
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "read a.py"}],
        "tools": [tool("read_file")],
    }
    response, events = await drive(config, store, payload, recorder)
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["arguments"] == '{"path":"calc.py"}'

    validated = events_of(events, "toolcall.validated")
    payload_out = validated[-1]["payload"]
    assert payload_out["repaired"] == 0
    assert payload_out["outcomes"][0]["text_changed"] is False


async def test_c3_issues_one_corrective_retry_when_a_call_cannot_be_repaired(config, store) -> None:
    """Plan.md §6 C3 §6: a bounded corrective inference retry."""
    config.repair.max_corrective_retries_per_request = 1
    config.repair.max_corrective_retries_per_task = 3
    calls: list[dict[str, Any]] = []

    class RetryRecorder(Recorder):
        def handler(self, request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content.decode("utf-8")))
            if len(calls) == 1:
                # First answer: a tool call with no arguments at all.
                return httpx.Response(
                    200,
                    json=chat_body(
                        tool_calls=[
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": "{}"},
                            }
                        ]
                    ),
                )
            # The corrective retry: a usable call.
            return httpx.Response(
                200,
                json=chat_body(
                    tool_calls=[
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": '{"path": "a.py", "content": "x"}',
                            },
                        }
                    ]
                ),
            )

    recorder = RetryRecorder()
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "write a.py"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                },
            }
        ],
    }
    response, events = await drive(config, store, payload, recorder)

    assert len(calls) == 2, "the proxy must issue exactly one corrective retry"
    hint = calls[1]["messages"][-1]["content"]
    assert "[pocket:C3]" in hint
    assert "write_file" in hint

    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"path": "a.py", "content": "x"}

    retries = events_of(events, "repair.retry")
    assert retries
    assert retries[0]["payload"]["failed"] == 1


async def test_c3_respects_the_corrective_retry_budget(config, store) -> None:
    """The retry is bounded per request and per task."""
    config.repair.max_corrective_retries_per_request = 0
    config.repair.max_corrective_retries_per_task = 0
    calls: list[dict[str, Any]] = []

    class CountingRecorder(Recorder):
        def handler(self, request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json=chat_body(
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": "{}"},
                        }
                    ]
                ),
            )

    recorder = CountingRecorder()
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "write a.py"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                },
            }
        ],
    }
    response, events = await drive(config, store, payload, recorder)
    assert len(calls) == 1, "no retry may be issued when the budget is zero"
    assert not events_of(events, "repair.retry")
    # The original invalid call is still reported to the client.
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["arguments"] == "{}"


async def test_c3_leaves_an_unrepairable_call_untouched(config, store) -> None:
    recorder = Recorder(
        chat_body(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{not json at all"},
                }
            ]
        )
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "read a.py"}],
        "tools": [tool("read_file")],
    }
    response, events = await drive(config, store, payload, recorder)
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["arguments"] == "{not json at all"

    validated = events_of(events, "toolcall.validated")
    assert validated[-1]["payload"]["failed"] == 1
    assert validated[-1]["payload"]["outcomes"][0]["error_class"].startswith("M.")


async def test_c3_does_not_rename_a_destructive_tool(config, store) -> None:
    """A destructive tool is never fuzzy-renamed (Plan.md §10 safety rule)."""
    config.client.destructive_tools = ["run_shell"]
    recorder = Recorder(
        chat_body(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run_shel", "arguments": '{"cmd": "ls"}'},
                }
            ]
        )
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "run ls"}],
        "tools": [tool("run_shell")],
    }
    response, events = await drive(config, store, payload, recorder)
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "run_shel"
    validated = events_of(events, "toolcall.validated")
    assert validated[-1]["payload"]["failed"] == 1


# ------------------------------------------------------------------- C4 ---


def _history_with_repeats(count: int, name: str = "read_file") -> list[dict[str, Any]]:
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
                            "name": name,
                            "arguments": json.dumps({"path": "a.py"}),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "name": name,
                "content": "contents",
            }
        )
    return messages


async def test_c4_fires_on_a_repeated_call_and_changes_the_request(config, store) -> None:
    """ADR 0002: the escalation is part of the transformation."""
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": _history_with_repeats(3),
        "tools": [tool("read_file")],
    }
    await drive(config, store, payload, recorder)

    forwarded = recorder.forwarded[0]
    last = forwarded["messages"][-1]
    assert last["role"] == "user"
    assert "[pocket:C4]" in last["content"]
    assert "Change the approach or stop" in last["content"]

    events = list(store.iter_events(store.list_runs()[0]["run_id"]))
    transformed = events_of(events, "request.transformed")
    rules = {rule["id"] for rule in transformed[0]["payload"]["rules"]}
    assert "C4.repeat_warning" in rules


async def test_c4_does_not_fire_without_a_repeat(config, store) -> None:
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": _history_with_repeats(2),
        "tools": [tool("read_file")],
    }
    await drive(config, store, payload, recorder)
    forwarded = recorder.forwarded[0]
    assert all("[pocket:C4]" not in str(message.get("content", "")) for message in forwarded["messages"])


async def test_c4_detects_an_oscillation(config, store) -> None:
    recorder = Recorder()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are a coding assistant"},
        {"role": "user", "content": "go"},
    ]
    for index, name in enumerate(["read_file", "write_file", "read_file", "write_file"]):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps({"path": "a.py"}),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "name": name,
                "content": "ok",
            }
        )
    payload = {
        "model": "test-model",
        "messages": messages,
        "tools": [tool("read_file"), tool("write_file")],
    }
    await drive(config, store, payload, recorder)
    forwarded = recorder.forwarded[0]
    assert "[pocket:C4]" in forwarded["messages"][-1]["content"]
    assert "oscillation" in forwarded["messages"][-1]["content"]


async def test_c4_never_changes_the_model_the_temperature_or_the_seed(config, store) -> None:
    """Plan.md §6 C4: the guard must not change generation parameters."""
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": _history_with_repeats(3),
        "tools": [tool("read_file")],
        "temperature": 0.2,
        "seed": 1234,
    }
    await drive(config, store, payload, recorder)
    forwarded = recorder.forwarded[0]
    assert forwarded["model"] == "test-model"
    assert forwarded.get("temperature") == 0.2
    assert forwarded.get("seed") == 1234


async def test_c4_escalation_is_reproducible(config, store) -> None:
    """The same conversation must produce the same transformed request."""
    from pocket_harness.pipeline import Pipeline

    pipeline = Pipeline(config)
    payload = {
        "model": "test-model",
        "messages": _history_with_repeats(3),
        "tools": [tool("read_file")],
    }
    canonical, _ = pipeline.normalize(payload)
    first = pipeline.transform(canonical)
    second = pipeline.transform(canonical)
    assert first.output_hash == second.output_hash
    assert first.output_hash != canonical_hash(canonical)


def canonical_hash(canonical) -> str:
    from pocket_harness.hashing import hash_obj

    return hash_obj(canonical.to_payload())


async def test_c4_warning_is_accounted_for_in_the_budget(config, store) -> None:
    """The warning must not push the request over the context budget."""
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": _big_conversation(),
        "tools": [tool("read_file")],
    }
    await drive(config, store, payload, recorder)
    events = list(store.iter_events(store.list_runs()[0]["run_id"]))
    transformed = events_of(events, "request.transformed")
    token_count = transformed[0]["payload"]["token_count"]
    budget = config.effective_context_length() - config.context.reserved_output_tokens
    assert token_count["after"] <= budget


async def test_c4_abort_is_off_by_default(config, store) -> None:
    """ADR 0003: the refusal is opt-in."""
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": _history_with_repeats(3),
        "tools": [tool("read_file")],
    }
    response, events = await drive(config, store, payload, recorder)
    assert response.status_code == 200
    assert recorder.forwarded


async def test_c4_abort_refuses_a_repeated_call_when_enabled(config, store) -> None:
    """ADR 0003: the refusal is a typed 4xx and the request is not forwarded."""
    config.loop_guard.abort_on_repeat = True
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": _history_with_repeats(3),
        "tools": [tool("read_file")],
    }
    response, events = await drive(config, store, payload, recorder)
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["type"] == "loop_repeated_call"
    assert "change the call or stop" in body["error"]["message"]
    assert not recorder.forwarded, "the request must not reach the upstream"
    aborted = events_of(events, "loop.aborted")
    assert aborted
    assert aborted[0]["payload"]["action"] == "refuse the request"


async def test_c4_abort_does_not_fire_without_a_repeat(config, store) -> None:
    config.loop_guard.abort_on_repeat = True
    recorder = Recorder()
    payload = {
        "model": "test-model",
        "messages": _history_with_repeats(2),
        "tools": [tool("read_file")],
    }
    response, _events = await drive(config, store, payload, recorder)
    assert response.status_code == 200
    assert recorder.forwarded
