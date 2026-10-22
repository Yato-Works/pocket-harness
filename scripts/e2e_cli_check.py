"""End-to-end CLI check: proxy -> request -> report -> replay.

Runs the real ``pocket-harness`` CLI as a subprocess against a mock upstream so the
whole chain (serve, capture, evidence, report, replay) is exercised the way a user
would run it.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def free_port() -> int:
    """Return a free TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockUpstream(BaseHTTPRequestHandler):
    """A minimal OpenAI-compatible upstream that answers tool calls."""

    def log_message(self, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"object": "list", "data": [{"id": "e2e-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        message: dict[str, Any] = {"role": "assistant", "content": "done"}
        if payload.get("tools"):
            first = payload["tools"][0]["function"]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": first["name"],
                            "arguments": json.dumps({"query": "hello"}),
                        },
                    }
                ],
            }
        body = json.dumps(
            {
                "id": "chatcmpl-e2e",
                "object": "chat.completion",
                "model": "e2e-model",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)



def _child_env() -> dict[str, str]:
    """Return the environment for a child process, with PYTHONPATH set."""
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def run(args: list[str]) -> tuple[int, str, str]:
    """Run the CLI and return (code, stdout, stderr)."""
    proc = subprocess.run(
        [str(PYTHON), "-m", "pocket_harness.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=_child_env(),
    )
    return proc.returncode, proc.stdout, proc.stderr


def make_config(work: Path, upstream_port: int, proxy_port: int) -> Path:
    """Write a configuration wired to the mock upstream and a temporary state root."""
    config_path = work / "pocket.toml"
    code, out, err = run(["init", str(config_path)])
    print(f"[init] exit={code} out={out.strip()} err={err.strip()}")
    assert code == 0

    text = config_path.read_text(encoding="utf-8")
    text = text.replace(
        'base_url = "http://127.0.0.1:11434/v1"',
        f'base_url = "http://127.0.0.1:{upstream_port}/v1"',
    )
    text = text.replace('model = "qwen2.5-coder:7b"', 'model = "e2e-model"')
    text = text.replace("port = 8787", f"port = {proxy_port}")
    state_root = (work / "state").as_posix()
    text = text.replace('root = "~/.pocket"', "root = '" + state_root + "'")
    text = text.replace('capture = "metadata"', 'capture = "local-replay"')
    config_path.write_text(text, encoding="utf-8")
    return config_path



def drive_proxy(
    proxy_port: int, base_url: str | None = None, model: str = "e2e-model"
) -> str:
    """Send one request through the running proxy and return the run ID."""
    import httpx

    deadline = time.monotonic() + 20
    ready = False
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{proxy_port}/healthz", timeout=2)
            if resp.status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.2)
    assert ready, "the proxy did not come up"

    resp = httpx.post(
        f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "you are a helpful assistant"},
                {"role": "user", "content": "search for hello"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "search",
                        "description": "search the web",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                }
            ],
        },
        timeout=300,
    )
    print(f"[request] status={resp.status_code}")
    print(json.dumps(resp.json(), indent=2)[:700])
    assert resp.status_code == 200, resp.text

    status = httpx.get(f"http://127.0.0.1:{proxy_port}/pocket/status", timeout=10)
    run_id = status.json()["run_id"]
    print(f"[run_id] {run_id}")

    flush = httpx.post(f"http://127.0.0.1:{proxy_port}/pocket/flush", timeout=10)
    events = flush.json()["events"]
    print(f"[events] {len(events)} recorded")
    for event in events:
        print(f"  - {event['event']}")
    return run_id



def main() -> int:
    """Run the whole check and print a summary."""
    upstream_port = free_port()
    proxy_port = free_port()
    server = HTTPServer(("127.0.0.1", upstream_port), MockUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    work = ROOT / ".e2e"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    config_path = make_config(work, upstream_port, proxy_port)

    proc = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "pocket_harness.cli",
            "serve",
            "--config",
            str(config_path),
        ],
        cwd=str(ROOT),
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    run_id = ""
    try:
        run_id = drive_proxy(proxy_port)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.shutdown()
        server.server_close()

    for args in (
        ["status", "--config", str(config_path)],
        ["report", "--config", str(config_path), "--run", run_id],
        ["replay", "--config", str(config_path), "--run", run_id],
    ):
        code, out, err = run(args)
        print(f"\n[$ {' '.join(args)}] exit={code}")
        print(out[:1600])
        if err.strip():
            print(f"stderr: {err.strip()[:500]}")

    return 0


def run_against_real_upstream(base_url: str, model: str) -> int:
    """Run the same chain against a real local upstream, e.g. Ollama."""
    work = ROOT / ".e2e-real"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()

    config_path = work / "pocket.toml"
    code, out, err = run(["init", str(config_path)])
    assert code == 0, err

    text = config_path.read_text(encoding="utf-8")
    text = text.replace(
        'base_url = "http://127.0.0.1:11434/v1"',
        f'base_url = "{base_url}"',
    )
    text = text.replace('model = "qwen2.5-coder:7b"', f'model = "{model}"')
    state_root = (work / "state").as_posix()
    text = text.replace('root = "~/.pocket"', "root = '" + state_root + "'")
    text = text.replace('capture = "metadata"', 'capture = "local-replay"')
    config_path.write_text(text, encoding="utf-8")

    proxy_port = free_port()
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("port = 8787", f"port = {proxy_port}"), encoding="utf-8")

    proc = subprocess.Popen(
        [str(PYTHON), "-m", "pocket_harness.cli", "serve", "--config", str(config_path)],
        cwd=str(ROOT),
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    run_id = ""
    try:
        run_id = drive_proxy(proxy_port, base_url=base_url, model=model)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    for args in (
        ["status", "--config", str(config_path)],
        ["report", "--config", str(config_path), "--run", run_id],
        ["replay", "--config", str(config_path), "--run", run_id],
    ):
        code, out, err = run(args)
        print(f"\n[$ {' '.join(args)}] exit={code}")
        print(out[:2000])
        if err.strip():
            print(f"stderr: {err.strip()[:500]}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--real":
        base = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:11434/v1"
        model = sys.argv[3] if len(sys.argv) > 3 else "qwen2.5:7b-instruct"
        sys.exit(run_against_real_upstream(base, model))
    sys.exit(main())
