"""Live end-to-end test: run the real server and talk to it over HTTP.

A tiny mock upstream is started as a local HTTP server, so the whole path
(client -> proxy -> upstream -> proxy -> client) is exercised for real.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest

from pocket_harness.config import Config
from pocket_harness.proxy import create_app


def free_port() -> int:
    """Return a free TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockUpstream(BaseHTTPRequestHandler):
    """A minimal OpenAI-compatible upstream for the live test."""

    received: list[dict[str, Any]] = []
    reply: dict[str, Any] = {}

    def log_message(self, *args: Any) -> None:  # noqa: D102 - silence the server
        return

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        """Serve the model list."""
        body = json.dumps({"object": "list", "data": [{"id": "live-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        """Serve a chat completion."""
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        MockUpstream.received.append(json.loads(raw))
        body = json.dumps(type(self).reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def live_upstream():
    """Start a mock upstream server and yield its base URL."""
    MockUpstream.received = []
    MockUpstream.reply = {
        "id": "chatcmpl-live",
        "object": "chat.completion",
        "model": "live-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "live ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    port = free_port()
    server = HTTPServer(("127.0.0.1", port), MockUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1", MockUpstream
    finally:
        server.shutdown()
        server.server_close()


def test_live_proxy_round_trip(live_upstream, store) -> None:
    """A request travels through the real proxy to a real HTTP upstream."""
    import uvicorn

    base_url, upstream = live_upstream

    config = Config()
    config.upstream.base_url = base_url
    config.upstream.model = "live-model"
    config.upstream.context_length = 4096
    config.tools.max_non_pinned = 2

    app = create_app(config, store=store)

    tools = [
        {
            "type": "function",
            "function": {
                "name": f"t{i}",
                "description": f"tool {i}",
                "parameters": {"type": "object", "properties": {"p": {"type": "string"}}},
            },
        }
        for i in range(8)
    ]

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "the proxy did not start"

        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "live-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "tools": tools,
                },
            )
            health = client.get("/healthz")
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "live ok"
    assert health.json()["status"] == "ok"

    # The upstream saw the reduced tool set.
    assert len(upstream.received) == 1
    assert len(upstream.received[0]["tools"]) == 2

    # The evidence log recorded the whole exchange.
    events = list(store.iter_events(app.state.service.run_id))
    names = [e["event"] for e in events]
    assert "request.received" in names
    assert "request.transformed" in names
    assert "response.completed" in names


def test_bench_runs_both_conditions(live_upstream, tmp_path) -> None:
    """The benchmark runs the baseline and the harness condition and summarizes both."""
    from pocket_harness.bench import run_benchmark

    base_url, upstream = live_upstream

    config = Config()
    config.upstream.base_url = base_url
    config.upstream.model = "live-model"
    config.upstream.context_length = 4096

    result = asyncio.run(
        run_benchmark(config, seed=7, output_dir=tmp_path, condition="both")
    )
    summary = result["summary"]
    assert set(summary["conditions"]) == {"baseline", "harness"}
    assert summary["conditions"]["baseline"]["runs"] == summary["conditions"]["harness"]["runs"]
    assert summary["delta_harness_minus_baseline"] is not None
    # The raw records were written for later inspection.
    assert (tmp_path / "bench-raw-seed7.json").is_file()
    assert "no LLM judge" in summary["note"]


def test_bench_uses_no_llm_judge(live_upstream, tmp_path) -> None:
    """The benchmark only ever calls the upstream once per task and condition."""
    from pocket_harness.bench import load_tasks, run_benchmark

    base_url, upstream = live_upstream

    config = Config()
    config.upstream.base_url = base_url
    config.upstream.model = "live-model"
    config.upstream.context_length = 4096

    tasks = load_tasks()
    assert tasks, "the benchmark task set must not be empty"

    asyncio.run(run_benchmark(config, seed=0, limit=2, condition="both", output_dir=tmp_path))
    # 2 tasks x 2 conditions = 4 upstream calls, and nothing more.
    assert len(upstream.received) == 4


def test_report_summarizes_a_run(live_upstream, store, monkeypatch) -> None:
    """The report is computed from the recorded events only."""
    from pocket_harness import report as report_module
    from pocket_harness.proxy import create_app

    base_url, upstream = live_upstream

    config = Config()
    config.upstream.base_url = base_url
    config.upstream.model = "live-model"
    config.upstream.context_length = 4096
    config.state.root = str(store.root)

    app = create_app(config, store=store)
    service = app.state.service

    import uvicorn

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "the proxy did not start"
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            client.post(
                "/v1/chat/completions",
                json={"model": "live-model", "messages": [{"role": "user", "content": "hi"}]},
            )
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    result = report_module.build_report(config, service.run_id)
    assert result["ok"] is True
    assert result["event_counts"]["request.received"] == 1
    assert result["event_counts"]["response.completed"] == 1


def test_replay_reports_that_metadata_capture_cannot_replay(config, store) -> None:
    """With metadata-only capture, replay says so instead of pretending to verify."""
    from pocket_harness.replay import run_replay

    config.logging.capture = "metadata"
    config.state.root = str(store.root)
    run_id = "run-test"
    store.create_run(run_id, label="test", manifest={})
    store.append_event(
        run_id,
        "req-1",
        "request.transformed",
        {"input_sha256": "a" * 64, "output_sha256": "b" * 64, "rules": []},
    )

    result = run_replay(config, store.runs_dir / run_id)
    assert result["ok"] is False
    assert "no request body was captured" in result["reason"]
    assert result["recorded_output_hashes"] == ["b" * 64]


def test_replay_reproduces_the_output_hash(config, store) -> None:
    """With a captured body, replay reproduces the recorded output hash."""
    from pocket_harness.replay import run_replay

    config.logging.capture = "local-replay"
    config.upstream.context_length = 4096
    config.state.root = str(store.root)

    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": "hello"}],
    }
    from pocket_harness.pipeline import Pipeline

    pipeline = Pipeline(config)
    canonical, _ = pipeline.normalize(payload)
    expected = pipeline.transform(canonical).output_hash

    capture_path = tmp_path_capture(store, payload)
    run_id = "run-replay"
    store.create_run(run_id, label="test", manifest={})
    store.append_event(
        run_id,
        "req-1",
        "request.transformed",
        {"input_sha256": "x" * 64, "output_sha256": expected, "rules": []},
    )

    result = run_replay(config, store.runs_dir / run_id, capture_ref=capture_path)
    assert result["ok"] is True
    assert result["matched"] is True
    assert result["replayed_output_sha256"] == expected



def tmp_path_capture(store, payload):
    """Write a captured payload to a temporary file and return its path."""
    import tempfile
    from pathlib import Path

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(payload, handle)
    handle.close()
    return Path(handle.name)

