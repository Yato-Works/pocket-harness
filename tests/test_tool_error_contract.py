"""Tests for the client's deterministic tool error and the repetition policy."""

from __future__ import annotations

import asyncio
import json

import httpx

from pocket_harness.agent import ScriptedAgent, describe_call_problem

SCHEMAS = {
    "write_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["overwrite", "append"]},
        },
        "required": ["path", "content"],
    },
    "read_file": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
}


def test_the_error_names_the_missing_argument() -> None:
    message = describe_call_problem(SCHEMAS, "write_file", "{}")
    assert "path" in message
    assert "content" in message
    assert "missing" in message
    assert "was rejected before it was executed" in message


def test_the_error_names_a_wrong_type() -> None:
    message = describe_call_problem(SCHEMAS, "read_file", '{"path": 5}')
    assert "string" in message
    assert "integer" in message


def test_the_error_names_an_invalid_enum_value() -> None:
    message = describe_call_problem(
        SCHEMAS, "write_file", '{"path": "a", "content": "b", "mode": "sideways"}'
    )
    assert "'overwrite'" in message
    assert "'append'" in message


def test_the_error_names_an_unknown_tool() -> None:
    message = describe_call_problem(SCHEMAS, "write_shel", '{"path": "a"}')
    assert "no tool named" in message
    assert "write_file" in message


def test_the_error_names_invalid_json() -> None:
    message = describe_call_problem(SCHEMAS, "read_file", "{not json")
    assert "not valid JSON" in message


def test_the_error_never_guesses_a_value() -> None:
    message = describe_call_problem(SCHEMAS, "write_file", "{}")
    assert "path of the file" not in message
    # No plausible value may appear in the error.
    assert ".py" not in message
    assert "src/" not in message


def _response(message: dict) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "m",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {},
    }


def _tool_call(name: str, arguments: str) -> dict:
    return {
        "id": "call_1",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_the_client_returns_the_precise_error_instead_of_executing(tmp_path) -> None:
    """The invalid call is not executed; the model is told what is missing."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        sent.append(body)
        if len(sent) == 1:
            return httpx.Response(
                200,
                json=_response(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [_tool_call("write_file", "{}")],
                    }
                ),
            )
        return httpx.Response(200, json=_response({"role": "assistant", "content": "done"}))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            agent = ScriptedAgent(
                base_url="http://upstream.test",
                model="m",
                workdir=tmp_path,
                test_command=["true"],
                client=client,
            )
            agent.schemas = SCHEMAS
            return await agent.run("write the file")

    result = asyncio.run(run())
    tool_message = sent[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "the required argument 'path'" in tool_message["content"]
    assert result.invalid_tool_calls == 1


def test_the_client_stops_after_repeated_identical_failures(tmp_path) -> None:
    """A call that fails the same way three times ends the task."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        sent.append(body)
        return httpx.Response(
            200,
            json=_response(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [_tool_call("write_file", "{}")],
                }
            ),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            agent = ScriptedAgent(
                base_url="http://upstream.test",
                model="m",
                workdir=tmp_path,
                test_command=["true"],
                client=client,
                max_identical_failures=3,
            )
            agent.schemas = SCHEMAS
            return await agent.run("write the file")

    result = asyncio.run(run())
    assert result.stop_reason == "repeated_identical_failure"
    assert result.repeated_identical_failures == 3
    assert "stopping instead of repeating it" in result.status
    # The client asked three times and then stopped.
    assert len(sent) == 3
