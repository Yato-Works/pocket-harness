"""Walk one task with the real model and report every tool call it makes.

The benchmark records schema-invalid tool calls (M.schema) but only their
hash. This probe replays the agent loop against the real model and prints the
arguments of every call together with the C3 outcome, so that the class of
failure can be characterised.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from pocket_harness.agent import (
    TOOL_SCHEMAS,
    ScriptedAgent,
)
from pocket_harness.components.tool_call_repair import ToolCallRepairer
from pocket_harness.repo_bench import load_repo_tasks

BASE = "http://127.0.0.1:11434/v1"
MODEL = "qwen2.5:7b-instruct"
TASK_ID = sys.argv[1] if len(sys.argv) > 1 else "heldout-02-fix-report-script"

SCHEMAS = {
    tool["function"]["name"]: tool["function"]["parameters"] for tool in TOOL_SCHEMAS
}


async def main() -> int:
    tasks = {task.task_id: task for task in load_repo_tasks()}
    task = tasks[TASK_ID]
    workdir = Path(".e2e") / "invalid-probe" / TASK_ID
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(task.snapshot, workdir)

    upstream = httpx.AsyncClient(timeout=300.0)
    seen: list[tuple[str, str]] = []

    async def forward(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        response = await upstream.post(
            f"{BASE}/chat/completions", json={**body, "model": MODEL}
        )
        data = response.json()
        message = ((data.get("choices") or [{}])[0]).get("message") or {}
        for call in message.get("tool_calls") or []:
            seen.append((call["function"]["name"], call["function"]["arguments"]))
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(forward))
    agent = ScriptedAgent(
        base_url="http://upstream.test/v1",
        model=MODEL,
        workdir=workdir,
        test_command=task.test_command,
        client=client,
        max_steps=task.max_steps,
        max_wall_seconds=task.max_wall_seconds,
        temperature=0,
        seed=1,
    )
    result = await agent.run(task.prompt)
    await client.aclose()
    await upstream.aclose()

    repairer = ToolCallRepairer(SCHEMAS)
    print(f"task={TASK_ID} stop={result.stop_reason} steps={result.steps}")
    print(f"tool calls: {len(seen)}")
    invalid = 0
    for name, arguments in seen:
        outcome = repairer.repair(name, arguments)
        flag = "" if outcome.ok else "  <-- INVALID"
        if not outcome.ok:
            invalid += 1
        print(f"  {name}{flag}")
        if not outcome.ok:
            print(f"      arguments: {arguments[:300]}")
            for reason in outcome.reasons:
                print(f"      reason: {reason}")
    print(f"invalid: {invalid} of {len(seen)}")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
