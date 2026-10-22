"""End-to-end tests for the proxy.

A mock upstream is used so that the tests never need a real inference server and
never depend on a model's behaviour.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pocket_harness.config import Config
from pocket_harness.proxy import create_app


def make_config() -> Config:
    """Return a configuration for the test proxy."""
    config = Config()
    config.upstream.base_url = "http://upstream.test/v1"
    config.upstream.model = "test-model"
    config.upstream.context_length = 4096
    config.context.reserved_output_tokens = 128
    config.context.safety_margin_tokens = 0
    config.tools.max_non_pinned = 3
    return config


def tool(name: str) -> dict[str, Any]:
    """Return a simple tool definition."""
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


def chat_body(content: str = "ok", tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return an OpenAI-compatible chat completion body."""
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


def make_client(handler) -> httpx.AsyncClient:
    """Return an async client whose transport is routed to ``handler``."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://proxy.test")


def upstream_handler(body: dict[str, Any] | None = None, status: int = 200):
    """Return a mock upstream handler."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "test-model"}]})
        if request.url.path.endswith("/chat/completions"):
            payload = body if body is not None else chat_body()
            if payload.get("stream"):
                return sse_response(payload)
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"error": "not found"})

    return handler


def sse_response(payload: dict[str, Any]) -> httpx.Response:
    """Return a Server-Sent-Events response for a streamed completion."""
    lines = []
    for chunk in payload.get("chunks", []):
        lines.append(f"data: {json.dumps(chunk)}\n\n")
    lines.append("data: [DONE]\n\n")
    return httpx.Response(200, text="".join(lines), headers={"Content-Type": "text/event-stream"})


@pytest.fixture
def config() -> Config:
    """Return a test configuration."""
    return make_config()


@pytest.fixture
def patch_upstream():
    """Return a factory that routes the upstream client's calls to a mock handler."""

    def install(app, handler):
        app.state.service.transport = httpx.MockTransport(handler)
        return handler

    return install


def make_app_client(app) -> httpx.AsyncClient:
    """Return an async client bound to the Starlette app."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    )


async def test_proxy_forwards_a_chat_completion(config, store, patch_upstream) -> None:
    """A normal request is forwarded and the response is returned unchanged."""
    app = create_app(config, store=store)
    patch_upstream(app, upstream_handler(chat_body(content="hello")))
    async with make_app_client(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello"


async def test_proxy_reduces_the_exposed_tool_set(config, store, patch_upstream) -> None:
    """C1 reduces the tool set that is sent upstream."""
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=chat_body())
        return httpx.Response(404)

    app = create_app(config, store=store)
    patch_upstream(app, handler)
    async with make_app_client(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [tool(f"t{i}") for i in range(10)],
            },
        )
    assert response.status_code == 200
    assert len(seen["body"]["tools"]) == 3


async def test_proxy_rejects_an_invalid_body(config, store, patch_upstream) -> None:
    """A malformed body is rejected with a stable error type."""
    app = create_app(config, store=store)
    patch_upstream(app, upstream_handler())
    async with make_app_client(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "client_invalid_request"


async def test_upstream_failure_is_not_hidden(config, store, patch_upstream) -> None:
    """An upstream failure is reported; no fallback is attempted."""
    calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(500, text="boom")

    app = create_app(config, store=store)
    patch_upstream(app, handler)
    async with make_app_client(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_unavailable"
    assert calls["count"] == 1


async def test_tool_call_is_validated_and_recorded(config, store, patch_upstream) -> None:
    """A tool call is validated with C3 and the outcome is recorded."""
    body = chat_body(
        content=None,
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "t0", "arguments": '{"path": "a.py"}'},
            }
        ],
    )
    app = create_app(config, store=store)
    patch_upstream(app, upstream_handler(body))
    async with make_app_client(app) as client:
        await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [tool(f"t{i}") for i in range(5)],
            },
        )
    events = list(store.iter_events(app.state.service.run_id))
    names = [e["event"] for e in events]
    assert "toolcall.validated" in names
    validated = next(e for e in events if e["event"] == "toolcall.validated")
    assert validated["payload"]["checked"] == 1
    assert validated["payload"]["failed"] == 0


async def test_invalid_tool_call_is_reported_as_a_failure(config, store, patch_upstream) -> None:
    """A tool call to an unknown tool is recorded as a failure."""
    body = chat_body(
        content=None,
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "unknown_tool", "arguments": "{}"},
            }
        ],
    )
    app = create_app(config, store=store)
    patch_upstream(app, upstream_handler(body))
    async with make_app_client(app) as client:
        await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [tool("t0")],
            },
        )
    events = list(store.iter_events(app.state.service.run_id))
    validated = next(e for e in events if e["event"] == "toolcall.validated")
    assert validated["payload"]["failed"] == 1


async def test_streaming_response_is_forwarded(config, store, patch_upstream) -> None:
    """A streamed response reaches the client with its SSE framing intact."""
    chunks = [
        {
            "id": "chatcmpl-1",
            "model": "test-model",
            "choices": [{"index": 0, "delta": {"content": "he"}}],
        },
        {
            "id": "chatcmpl-1",
            "model": "test-model",
            "choices": [{"index": 0, "delta": {"content": "llo"}}],
        },
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return sse_response({"chunks": chunks})
        return httpx.Response(404)

    app = create_app(config, store=store)
    patch_upstream(app, handler)
    async with make_app_client(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert response.status_code == 200
    text = response.text
    assert "data: " in text
    assert "[DONE]" in text
    assert '"he"' in text and '"llo"' in text


async def test_models_endpoint_lists_the_model(config, store) -> None:
    """The models endpoint reports the configured upstream model."""
    app = create_app(config, store=store)
    async with make_app_client(app) as client:
        response = await client.get("/v1/models")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "test-model"


async def test_status_endpoint_reports_the_run(config, store) -> None:
    """The status endpoint reports a deterministic status."""
    app = create_app(config, store=store)
    async with make_app_client(app) as client:
        response = await client.get("/pocket/status")
    body = response.json()
    assert body["run_id"] == app.state.service.run_id
    assert body["requests"] == 0


async def test_health_endpoint(config, store) -> None:
    """The health probe reports ok."""
    app = create_app(config, store=store)
    async with make_app_client(app) as client:
        response = await client.get("/healthz")
    assert response.json()["status"] == "ok"


async def test_authorization_header_is_redacted_in_events(config, store, patch_upstream) -> None:
    """A bearer token never reaches the evidence log."""
    app = create_app(config, store=store)
    patch_upstream(app, upstream_handler())
    async with make_app_client(app) as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer sk-super-secret-value"},
        )
    raw = store.events_path(app.state.service.run_id).read_text(encoding="utf-8")
    assert "sk-super-secret-value" not in raw
    assert "[REDACTED]" in raw


async def test_metadata_capture_stores_no_request_body(config, store, patch_upstream) -> None:
    """With the default capture mode no request body is written to the blob store."""
    app = create_app(config, store=store)
    patch_upstream(app, upstream_handler())
    async with make_app_client(app) as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
    events = list(store.iter_events(app.state.service.run_id))
    assert not [e for e in events if e["event"] == "request.captured"]
    assert list(store.blobs_dir.iterdir()) == []


async def test_local_replay_capture_stores_the_request_body(
    config, store, patch_upstream
) -> None:
    """With ``local-replay`` the body is stored locally and a replay reproduces it."""
    config.logging.capture = "local-replay"
    app = create_app(config, store=store)
    patch_upstream(app, upstream_handler())
    async with make_app_client(app) as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    events = list(store.iter_events(app.state.service.run_id))
    captured = [e for e in events if e["event"] == "request.captured"]
    assert len(captured) == 1
    ref = captured[0]["payload"]["blob_ref"]
    assert ref.startswith("sha256:")
    assert store.has_blob(ref)





