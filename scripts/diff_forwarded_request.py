"""Diff the request the proxy forwards against the request the client sends.

For a pair where the two conditions disagreed but no mechanism fired, the
difference has to be somewhere else. This script runs the same task twice --
once directly against the upstream, once through the proxy -- and diffs the two
request bodies field by field.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from pocket_harness.agent import ScriptedAgent
from pocket_harness.config import load_config
from pocket_harness.proxy import create_app
from pocket_harness.repo_bench import load_repo_tasks
from pocket_harness.state import StateStore

BASE = "http://127.0.0.1:11434/v1"
MODEL = "qwen2.5:7b-instruct"
TASK_ID = sys.argv[1] if len(sys.argv) > 1 else "heldout-10-mutation"
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 1


def diff(left: dict, right: dict) -> list[str]:
    """Return the differences between two request bodies."""
    out: list[str] = []
    for key in sorted(set(left) | set(right)):
        if key == "messages":
            continue
        if left.get(key) != right.get(key):
            out.append(f"{key}: client={left.get(key)!r} proxy={right.get(key)!r}")
    left_messages = left.get("messages") or []
    right_messages = right.get("messages") or []
    if len(left_messages) != len(right_messages):
        out.append(
            f"messages: client={len(left_messages)} proxy={len(right_messages)}"
        )
    for index, (a, b) in enumerate(zip(left_messages, right_messages, strict=False)):
        if a != b:
            out.append(
                f"message[{index}]:"
                f"\n    client={json.dumps(a)[:200]}"
                f"\n    proxy ={json.dumps(b)[:200]}"
            )
    return out


async def main() -> int:
    tasks = {task.task_id: task for task in load_repo_tasks()}
    task = tasks[TASK_ID]
    workdir = Path(".e2e") / "diff-probe" / TASK_ID
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(task.snapshot, workdir)

    config = load_config("bench/bench-local.toml")
    upstream = httpx.AsyncClient(timeout=300.0)

    client_requests: list[dict] = []
    proxy_requests: list[dict] = []

    async def direct(request: httpx.Request) -> httpx.Response:
        client_requests.append(json.loads(request.content.decode("utf-8")))
        return await upstream.post(
            f"{BASE}/chat/completions",
            json={**client_requests[-1], "model": MODEL},
            timeout=300.0,
        )

    async def through_proxy(request: httpx.Request) -> httpx.Response:
        proxy_requests.append(json.loads(request.content.decode("utf-8")))
        return await upstream.post(
            f"{BASE}/chat/completions",
            json={**proxy_requests[-1], "model": MODEL},
            timeout=300.0,
        )

    store = StateStore(".e2e/diff-probe/state")
    app = create_app(config, store=store)
    app.state.service.transport = httpx.MockTransport(through_proxy)

    for label, transport in (("client", direct), ("proxy", through_proxy)):
        client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        agent = ScriptedAgent(
            base_url="http://upstream.test/v1",
            model=MODEL,
            workdir=workdir,
            test_command=task.test_command,
            client=client,
            max_steps=task.max_steps,
            max_wall_seconds=task.max_wall_seconds,
            temperature=0,
            seed=SEED,
        )
        result = await agent.run(task.prompt)
        print(
            f"{label}: steps={result.steps} stop={result.stop_reason} "
            f"tools={len(result.tool_calls)}"
        )
        await client.aclose()

    await upstream.aclose()
    if client_requests and proxy_requests:
        print("\n--- differences between the client request and the forwarded request ---")
        for line in diff(client_requests[0], proxy_requests[0]):
            print(f"  {line}")
        if not diff(client_requests[0], proxy_requests[0]):
            print("  (the first requests are identical)")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
