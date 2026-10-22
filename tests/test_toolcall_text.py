"""Tests for tool calls that an upstream delivered as text.

The upstream sometimes answers a tool request with the call written into the
message text. The client then executes nothing, the task makes no progress,
and the failure must not be recorded as a model reasoning failure.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from pocket_harness.agent import EMPTY_RETRY_NUDGE
from pocket_harness.config import Config
from pocket_harness.proxy import create_app
from pocket_harness.state import StateStore
from pocket_harness.taxonomy import classify_run
from pocket_harness.toolcall_text import describe_text_tool_call, find_text_tool_call


def test_a_json_tool_call_in_the_text_is_recognised() -> None:
    content = '{"name": "write_file", "arguments": {"path": "a.py", "content": "x"}}'
    call = find_text_tool_call(content)
    assert call is not None
    assert call["name"] == "write_file"


def test_a_tagged_tool_call_is_recognised() -> None:
    content = '<tool_call>\n{"name": "run_tests", "arguments": {}}\n</tool_call>'
    call = find_text_tool_call(content)
    assert call is not None
    assert call["name"] == "run_tests"


def test_plain_text_is_not_a_tool_call() -> None:
    assert find_text_tool_call("I will now read the file for you.") is None
    assert find_text_tool_call("") is None
    assert find_text_tool_call(None) is None


def test_text_that_only_mentions_a_tool_is_not_a_tool_call() -> None:
    assert find_text_tool_call("The write_file tool writes a file.") is None


def test_the_description_redacts_the_arguments() -> None:
    content = '{"name": "write_file", "arguments": {"path": "a.py", "content": "secret"}}'
    described = describe_text_tool_call(content)
    assert described is not None
    assert described["name"] == "write_file"
    assert described["argument_keys"] == ["content", "path"]
    assert "secret" not in json.dumps(described)


def test_a_text_delivered_call_is_class_m() -> None:
    result = classify_run(
        task_passed=False, run_completed=True, tool_call_delivered_as_text=True
    )
    assert result.primary_class == "M"
    assert result.eligible is True


def test_an_empty_completion_is_class_r_and_ineligible() -> None:
    result = classify_run(task_passed=False, run_completed=False, empty_completion=True)
    assert result.primary_class == "R"
    assert result.eligible is False


def test_a_run_that_passed_has_no_failure_class() -> None:
    """A successful run is not a failure, even when a signal was observed."""
    result = classify_run(
        task_passed=True, run_completed=True, tool_call_delivered_as_text=True
    )
    assert result.primary_class == ""
    assert "M" in result.signals
    assert result.eligible is True


def test_a_passing_run_stays_eligible_even_with_an_upstream_signal() -> None:
    result = classify_run(task_passed=True, run_completed=True, empty_completion=True)
    assert result.primary_class == ""
    assert result.eligible is True


def _response(message: dict) -> dict:
    return {
        "id": "1",
        "object": "chat.completion",
        "model": "m",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {},
    }


def test_the_agent_records_a_text_delivered_tool_call(tmp_path) -> None:
    content = '<tool_call>\n{"name": "write_file", "arguments": {"path": "a.py"}}\n</tool_call>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response({"role": "assistant", "content": content}))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            from pocket_harness.agent import ScriptedAgent

            agent = ScriptedAgent(
                base_url="http://upstream.test",
                model="m",
                workdir=tmp_path,
                test_command=["true"],
                client=client,
            )
            return await agent.run("do it")

    result = asyncio.run(run())
    assert result.tool_call_delivered_as_text is True
    assert result.stop_reason == "tool_call_delivered_as_text"
    assert result.tool_calls == []


def test_the_agent_records_an_empty_completion(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response({"role": "assistant", "content": ""}))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            from pocket_harness.agent import ScriptedAgent

            agent = ScriptedAgent(
                base_url="http://upstream.test",
                model="m",
                workdir=tmp_path,
                test_command=["true"],
                client=client,
                max_empty_retries=0,
            )
            return await agent.run("do it")

    result = asyncio.run(run())
    assert result.empty_completion is True
    assert result.stop_reason == "empty_completion"


def test_the_agent_asks_once_more_after_an_empty_answer(tmp_path) -> None:
    """An empty answer is recovered by asking the model to continue."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(200, json=_response({"role": "assistant", "content": ""}))
        if len(seen) == 2:
            return httpx.Response(
                200,
                json=_response(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                            }
                        ],
                    }
                ),
            )
        return httpx.Response(200, json=_response({"role": "assistant", "content": "done"}))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            from pocket_harness.agent import ScriptedAgent

            agent = ScriptedAgent(
                base_url="http://upstream.test",
                model="m",
                workdir=tmp_path,
                test_command=["true"],
                client=client,
            )
            return await agent.run("do it")

    result = asyncio.run(run())
    assert len(seen) == 3
    assert seen[1]["messages"][-1]["content"] == EMPTY_RETRY_NUDGE
    assert result.empty_retries == 1
    assert result.empty_completion is False
    assert result.stop_reason == "model_stopped"


async def test_the_proxy_records_a_text_delivered_call(tmp_path) -> None:
    """The evidence log must show that the upstream delivered the call as text."""
    content = '<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response({"role": "assistant", "content": content}))

    config = Config()
    config.state.root = str(tmp_path / "state")
    config.upstream.base_url = "http://upstream.test/v1"
    config.upstream.model = "m"
    store = StateStore(tmp_path / "state")
    app = create_app(config, store=store)
    app.state.service.transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200

    events = list(store.iter_events(store.list_runs()[0]["run_id"]))
    validated = [event for event in events if event["event"] == "toolcall.validated"]
    assert validated
    payload = validated[-1]["payload"]
    assert payload["checked"] == 0
    assert payload["delivered_as_text"]["name"] == "read_file"
