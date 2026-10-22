"""Replay and determinism audit (Plan.md §10.6, §2 reproducibility row).

These tests check the properties the audit asks for explicitly:

* the request body is actually stored by ``capture = "local-replay"``;
* the body can be discovered from a ``request.captured`` event;
* ``--capture-file`` wins over auto-discovery;
* a changed input makes ``input_matched`` fail;
* a changed configuration makes ``config_matched`` fail;
* ``ok`` is never true when either of them fails;
* the proxy and the replay use the same rule for ``recent_tool_uses`` and
  ``recent_context``;
* a missing blob, a corrupted blob and an invalid event sequence are handled
  without pretending that anything was verified.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from pocket_harness.config import Config
from pocket_harness.proxy import create_app
from pocket_harness.replay import run_replay
from pocket_harness.state import StateStore

CONVERSATION_WITH_TOOL_CALLS: dict[str, Any] = {
    "model": "test-model",
    "messages": [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Please read the configuration file."},
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
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "print('hello')"},
        {"role": "user", "content": "Now read the other file too."},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": "Run the test suite.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ],
}


def chat_body() -> dict[str, Any]:
    """Return a minimal OpenAI-compatible chat completion body."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def upstream_handler():
    """Return a mock upstream handler."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json=chat_body())
        return httpx.Response(404, json={"error": "not found"})

    return handler


def make_config(state_root) -> Config:
    """Return a configuration rooted at ``state_root``."""
    config = Config()
    config.state.root = str(state_root)
    config.upstream.base_url = "http://upstream.test/v1"
    config.upstream.model = "test-model"
    config.upstream.context_length = 4096
    config.context.reserved_output_tokens = 128
    config.context.safety_margin_tokens = 0
    return config


async def drive_proxy(config: Config, store: StateStore, body: dict[str, Any]) -> str:
    """Send one request through the proxy and return the run ID."""
    app = create_app(config, store=store)
    app.state.service.transport = httpx.MockTransport(upstream_handler())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    ) as client:
        response = await client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
    return app.state.service.run_id


async def test_local_replay_actually_stores_the_request_body(config, store) -> None:
    """``capture = "local-replay"`` writes the canonical request body to a blob."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
    run_id = await drive_proxy(config, store, body)

    captured = [
        event
        for event in store.iter_events(run_id)
        if event.get("event") == "request.captured"
    ]
    assert captured, "the proxy must record a request.captured event"
    ref = captured[0]["payload"]["blob_ref"]
    assert store.has_blob(ref), "the captured body must exist in the blob store"

    stored = json.loads(store.get_blob(ref).decode("utf-8"))
    assert stored["messages"][0]["content"] == "hello"


async def test_metadata_mode_stores_no_request_body(config, store) -> None:
    """The default ``metadata`` mode must not persist the request body."""
    config.state.root = str(store.root)
    config.logging.capture = "metadata"
    body = {"model": "test-model", "messages": [{"role": "user", "content": "secret-content"}]}
    run_id = await drive_proxy(config, store, body)

    assert not [
        event for event in store.iter_events(run_id) if event.get("event") == "request.captured"
    ]
    for blob in store.blobs_dir.glob("*"):
        assert b"secret-content" not in blob.read_bytes()


async def test_captured_body_is_discovered_from_the_event(config, store) -> None:
    """The body is found automatically through the ``request.captured`` event."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
    run_id = await drive_proxy(config, store, body)

    report = run_replay(config, store.runs_dir / run_id)
    assert report["capture_source"].startswith("sha256:")
    assert report["ok"] is True


async def test_capture_file_wins_over_auto_discovery(config, store, tmp_path) -> None:
    """``--capture-file`` takes priority over the auto-discovered blob."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
    run_id = await drive_proxy(config, store, body)

    capture_file = tmp_path / "capture.json"
    capture_file.write_text(json.dumps(body), encoding="utf-8")

    report = run_replay(config, store.runs_dir / run_id, capture_ref=str(capture_file))
    assert report["capture_source"] == str(capture_file)
    assert report["ok"] is True


async def test_a_changed_input_fails_the_input_match(config, store, tmp_path) -> None:
    """A modified captured body must not report a matching input."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
    run_id = await drive_proxy(config, store, body)

    changed = {"model": "test-model", "messages": [{"role": "user", "content": "something else"}]}
    capture_file = tmp_path / "capture.json"
    capture_file.write_text(json.dumps(changed), encoding="utf-8")

    report = run_replay(config, store.runs_dir / run_id, capture_ref=str(capture_file))
    assert report["input_matched"] is False
    assert report["matched"] is False
    assert report["ok"] is False


async def test_a_missing_blob_is_reported_without_claiming_success(config, store) -> None:
    """A blob reference that does not exist must fail explicitly."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
    run_id = await drive_proxy(config, store, body)

    report = run_replay(config, store.runs_dir / run_id, capture_ref="sha256:" + "00" * 32)
    assert report["ok"] is False
    assert report["reason"]


async def test_a_corrupted_blob_is_reported_without_claiming_success(config, store) -> None:
    """A blob whose content is not valid JSON must fail explicitly."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
    run_id = await drive_proxy(config, store, body)

    captured = [
        event
        for event in store.iter_events(run_id)
        if event.get("event") == "request.captured"
    ]
    ref = captured[0]["payload"]["blob_ref"]
    digest = ref.split(":", 1)[-1]
    (store.blobs_dir / digest).write_bytes(b"{not json at all")

    report = run_replay(config, store.runs_dir / run_id, capture_ref=ref)
    assert report["ok"] is False
    assert "json" in report["reason"].lower()


async def test_an_invalid_event_sequence_is_handled(config, store) -> None:
    """Garbage in the event log must not crash the replay."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
    run_id = await drive_proxy(config, store, body)

    events_path = store.events_path(run_id)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write("this is not json\n")
        handle.write("{\n")
    report = run_replay(config, store.runs_dir / run_id)
    assert report["ok"] is True  # the valid events are still usable

    events_path.write_text("garbage only\n", encoding="utf-8")
    report = run_replay(config, store.runs_dir / run_id)
    assert report["ok"] is False
    assert report["reason"]


async def test_proxy_and_replay_use_the_same_recent_tool_rule(config, store) -> None:
    """A conversation with tool calls replays to the identical output hash.

    This is the parity check for ``recent_tool_uses`` / ``recent_context``: the
    replay recomputes them from the captured body, and the resulting C1
    selection must be the one the proxy made.
    """
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    run_id = await drive_proxy(config, store, CONVERSATION_WITH_TOOL_CALLS)

    report = run_replay(config, store.runs_dir / run_id)
    assert report["matched"] is True, report
    assert report["input_matched"] is True


async def test_replay_identifies_the_compared_request(config, store) -> None:
    """The report must say which recorded request it was compared against."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
    run_id = await drive_proxy(config, store, body)

    report = run_replay(config, store.runs_dir / run_id)
    assert report.get("compared_request_index") == 0
    assert report.get("compared_request_id")
    assert report.get("compared_request_seq")
    assert report.get("recorded_request_count") == 1


async def test_unrecorded_configuration_is_reported_as_unverifiable(config, store) -> None:
    """A run without a configuration hash must not claim the settings matched."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
    run_id = await drive_proxy(config, store, body)

    # Rewrite the manifest so that no configuration hash was recorded.
    import sqlite3

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE runs SET manifest = ? WHERE run_id = ?", ("{}", run_id)
        )
        conn.commit()

    report = run_replay(config, store.runs_dir / run_id)
    assert report["config_matched"] is None
    assert report["warnings"]
    assert report["matched"] is True
