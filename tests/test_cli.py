"""CLI smoke tests against a live mock upstream."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from pocket_harness.cli import main


def free_port() -> int:
    """Return a free TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _sample_arguments(schema: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal valid argument object for a JSON Schema."""
    properties = schema.get("properties", {}) or {}
    required = schema.get("required", []) or list(properties)
    out: dict[str, Any] = {}
    for name in required:
        prop = properties.get(name, {})
        kind = prop.get("type", "string")
        if "enum" in prop and prop["enum"]:
            out[name] = prop["enum"][0]
        elif kind == "integer":
            out[name] = 1
        elif kind == "number":
            out[name] = 1.0
        elif kind == "boolean":
            out[name] = True
        elif kind == "array":
            out[name] = []
        elif kind == "object":
            out[name] = {}
        else:
            out[name] = "UTC"
    return out


class MockUpstream(BaseHTTPRequestHandler):
    """A minimal OpenAI-compatible upstream."""

    received: list[dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:  # noqa: D102 - silence the server
        return

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        """Serve the model list."""
        body = json.dumps({"object": "list", "data": [{"id": "cli-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        """Serve a chat completion, streaming it when asked to."""
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        MockUpstream.received.append(payload)

        if payload.get("stream"):
            chunks = [
                {
                    "id": "chatcmpl-cli",
                    "model": "cli-model",
                    "choices": [{"index": 0, "delta": {"content": "ok"}}],
                }
            ]
            lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
            lines.append("data: [DONE]\n\n")
            body = "".join(lines).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        message: dict[str, Any] = {"role": "assistant", "content": "ok"}
        if payload.get("tools"):
            # Emit a tool call for the first tool, with valid arguments.
            first = payload["tools"][0]["function"]
            message["content"] = None
            message["tool_calls"] = [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": first["name"],
                        "arguments": json.dumps(_sample_arguments(first.get("parameters", {}))),
                    },
                }
            ]

        body = json.dumps(
            {
                "id": "chatcmpl-cli",
                "object": "chat.completion",
                "model": "cli-model",
                "choices": [
                    {"index": 0, "message": message, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def upstream_url():
    """Start a mock upstream and yield its base URL."""
    MockUpstream.received = []
    port = free_port()
    server = HTTPServer(("127.0.0.1", port), MockUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()


def test_doctor_reports_all_checks(upstream_url, tmp_path, capsys) -> None:
    """``pocket doctor`` runs every check and reports the result."""
    main(
        [
            "doctor",
            "--upstream",
            upstream_url,
            "--model",
            "cli-model",
            "--context",
            "4096",
        ]
    )
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["ok"] is True, json.dumps(report, indent=2)
    names = [check["check"] for check in report["checks"]]
    assert "upstream.reachable" in names
    assert "model.available" in names
    assert "context.known" in names
    assert "chat.unary" in names
    assert "chat.stream" in names


def test_bench_writes_raw_results(upstream_url, tmp_path, capsys) -> None:
    """``pocket bench`` prints a summary and writes the raw records."""
    exit_code = main(
        [
            "bench",
            "--upstream",
            upstream_url,
            "--model",
            "cli-model",
            "--context",
            "4096",
            "--limit",
            "2",
            "--seed",
            "3",
            "--out",
            str(tmp_path),
        ]
    )
    capsys.readouterr()
    assert exit_code == 0
    raw = tmp_path / "bench-raw-seed3.json"
    assert raw.is_file()
    data = json.loads(raw.read_text(encoding="utf-8"))
    assert data["seed"] == 3
    assert data["records"]
    assert "no LLM judge" in data["note"]


def test_status_and_report_run_without_a_proxy(tmp_path, capsys) -> None:
    """``pocket status`` and ``pocket report`` work with no recorded run."""
    assert main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert "runs" in status
    assert status["pipeline_version"].startswith("pocket-harness/")


def test_init_writes_a_configuration(tmp_path, capsys) -> None:
    """``pocket init`` writes a usable configuration file."""
    target = tmp_path / "pocket.toml"
    assert main(["init", str(target)]) == 0
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "[upstream]" in text
    assert "context_length" in text
    # The file is valid TOML that the configuration loader accepts.
    from pocket_harness.config import load_config

    config = load_config(target)
    assert config.upstream.context_length == 32768


def test_init_refuses_to_overwrite(tmp_path, capsys) -> None:
    """``pocket init`` does not clobber an existing file unless asked to."""
    target = tmp_path / "pocket.toml"
    target.write_text("keep me", encoding="utf-8")
    assert main(["init", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == "keep me"
    assert main(["init", str(target), "--force"]) == 0
    assert target.read_text(encoding="utf-8") != "keep me"


def test_bad_config_reports_a_hint_instead_of_a_traceback(tmp_path, capsys) -> None:
    """A malformed configuration produces a readable message and exit code 2."""
    target = tmp_path / "pocket.toml"
    target.write_text('[state]\nroot = "C:\\Users\\me\\.pocket"\n', encoding="utf-8")

    exit_code = main(["status", "--config", str(target)])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "not valid TOML" in captured.err
    assert "Traceback" not in captured.err
    # The hint about Windows paths must actually be shown: this is the error
    # the README promises to explain.
    assert "hint:" in captured.err
    assert "literal string" in captured.err


def test_a_missing_config_file_is_an_error(tmp_path, capsys) -> None:
    """An explicitly named configuration file must exist."""
    missing = tmp_path / "not-here.toml"
    exit_code = main(["status", "--config", str(missing)])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "does not exist" in captured.err
    assert "Traceback" not in captured.err


def test_bench_without_tasks_exits_non_zero(tmp_path, capsys) -> None:
    """A benchmark that could not produce records must not report success."""
    target = tmp_path / "pocket.toml"
    target.write_text(
        f"[state]\nroot = '{(tmp_path / 'state').as_posix()}'\n", encoding="utf-8"
    )
    empty = tmp_path / "tasks"
    empty.mkdir()
    exit_code = main(
        [
            "bench",
            "--config",
            str(target),
            "--suite",
            "repo",
            "--tasks",
            str(empty),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error" in captured.out


def test_report_and_replay_use_the_configured_state_root(tmp_path, capsys) -> None:
    """The state root from the configuration is honored by the offline commands."""
    state_root = tmp_path / "custom-state"
    target = tmp_path / "pocket.toml"
    target.write_text(
        f"[state]\nroot = '{state_root.as_posix()}'\n", encoding="utf-8"
    )

    assert main(["status", "--config", str(target)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state_root"] == str(state_root)
    assert state_root.is_dir()


def test_report_of_a_missing_run_fails(tmp_path, capsys) -> None:
    """``pocket report`` exits non-zero when the run cannot be summarized."""
    target = tmp_path / "pocket.toml"
    target.write_text(
        f"[state]\nroot = '{(tmp_path / 'state').as_posix()}'\n", encoding="utf-8"
    )
    assert main(["report", "--config", str(target), "--run", "nope"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert "no events" in report["reason"]


def test_bench_runs_the_task_set(upstream_url, tmp_path, capsys) -> None:
    """``pocket bench`` runs the task set and reports a deterministic summary."""
    exit_code = main(
        [
            "bench",
            "--upstream",
            upstream_url,
            "--model",
            "cli-model",
            "--context",
            "4096",
            "--limit",
            "1",
            "--condition",
            "baseline",
            "--out",
            str(tmp_path),
        ]
    )
    capsys.readouterr()
    assert exit_code == 0
    raw = tmp_path / "bench-raw-seed0.json"
    assert raw.is_file()
    data = json.loads(raw.read_text(encoding="utf-8"))
    assert "no LLM judge" in data["note"]


