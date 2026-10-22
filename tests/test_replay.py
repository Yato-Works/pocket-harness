"""Tests for the replay round trip.

A replay is only meaningful when the recorded request hash can be reproduced from a
captured body, so these tests drive the proxy, capture the body, and then replay it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from pocket_harness.config import Config
from pocket_harness.proxy import create_app
from pocket_harness.replay import run_replay
from pocket_harness.state import StateStore


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


async def test_replay_reproduces_the_recorded_hash(config, store) -> None:
    """A captured body replays to exactly the recorded output hash."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
    }
    run_id = await drive_proxy(config, store, body)

    report = run_replay(config, store.runs_dir / run_id)
    assert report["ok"] is True
    assert report["matched"] is True
    assert report["input_matched"] is True
    assert report["replayed_output_sha256"] == report["recorded_output_sha256"]
    assert report["capture_source"].startswith("sha256:")


async def test_replay_without_a_capture_explains_why(config, store) -> None:
    """Without a captured body the replay refuses to claim it verified anything."""
    config.state.root = str(store.root)
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
    }
    run_id = await drive_proxy(config, store, body)

    report = run_replay(config, store.runs_dir / run_id)
    assert report["ok"] is False
    assert "no request body was captured" in report["reason"]
    assert report["recorded_input_hashes"]


async def test_replay_accepts_an_explicit_capture_file(config, store, tmp_path) -> None:
    """``--capture-file`` overrides the auto-discovered blob."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
    }
    run_id = await drive_proxy(config, store, body)

    capture_file = tmp_path / "capture.json"
    capture_file.write_text(json.dumps(body), encoding="utf-8")

    report = run_replay(config, store.runs_dir / run_id, capture_ref=str(capture_file))
    assert report["ok"] is True
    assert report["capture_source"] == str(capture_file)


async def test_replay_detects_a_changed_configuration(config, store) -> None:
    """A changed configuration is reported, so the reproduction claim is not made."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
    }
    run_id = await drive_proxy(config, store, body)

    # Change the configuration so the replay no longer uses the recorded settings.
    config.upstream.context_length = 512
    config.context.reserved_output_tokens = 256
    report = run_replay(config, store.runs_dir / run_id)
    assert report["ok"] is False
    assert report["config_matched"] is False
    assert report["recorded_config_hash"] != report["config_hash"]
    assert report["warnings"]


async def test_replay_reports_a_matching_configuration(config, store) -> None:
    """With unchanged settings the configuration hash matches the recorded one."""
    config.state.root = str(store.root)
    config.logging.capture = "local-replay"
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
    }
    run_id = await drive_proxy(config, store, body)

    report = run_replay(config, store.runs_dir / run_id)
    assert report["config_matched"] is True
    assert report["recorded_config_hash"] == report["config_hash"]
    assert report["warnings"] == []
