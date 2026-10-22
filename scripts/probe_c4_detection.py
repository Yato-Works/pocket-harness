"""Show what the proxy detects in the requests of a real run.

The escalation fired in the benchmark but the refusal (ADR 0003) did not. This
script drives the agent through the proxy and prints the C4 detection for every
request.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from pocket_harness.components.loop_guard import detect_from_history
from pocket_harness.config import load_config
from pocket_harness.pipeline import Pipeline
from pocket_harness.repo_bench import load_repo_tasks

BASE = "http://127.0.0.1:11434/v1"
MODEL = "qwen2.5:7b-instruct"
TASK_ID = sys.argv[1] if len(sys.argv) > 1 else "heldout-03-slice-off-by-one"
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 1


async def main() -> int:
    tasks = {task.task_id: task for task in load_repo_tasks()}
    task = tasks[TASK_ID]
    workdir = Path(".e2e") / "detect-probe" / TASK_ID
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(task.snapshot, workdir)

    upstream = httpx.AsyncClient(timeout=300.0)
    seen: list[dict] = []

    async def forward(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.append(body)
        response = await upstream.post(
            f"{BASE}/chat/completions", json={**body, "model": MODEL}
        )
        return response

    config = load_config("bench/bench-local.toml")
    pipeline = Pipeline(config)
    client = httpx.AsyncClient(transport=httpx.MockTransport(forward))
    from pocket_harness.agent import ScriptedAgent

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
    await client.aclose()
    await upstream.aclose()

    print(f"task={TASK_ID} seed={SEED} stop={result.stop_reason} steps={result.steps}")
    for index, body in enumerate(seen):
        canonical, _ = pipeline.normalize(body)
        events = detect_from_history(
            canonical.messages,
            repeated_call_threshold=config.loop_guard.repeated_call_threshold,
            detect_oscillation=config.loop_guard.detect_oscillation,
        )
        calls = [
            (c.name, c.arguments_text[:40])
            for message in canonical.messages
            for c in (message.tool_calls or [])
        ]
        print(f"  request {index}: {len(canonical.messages)} messages, "
              f"{len(calls)} historical calls, detected={[e.signal.value for e in events]}")
        if index == len(seen) - 1:
            for name, arguments in calls[-6:]:
                print(f"      {name} {arguments!r}")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
