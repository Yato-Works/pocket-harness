"""Capture the exact request that produced an empty upstream answer.

The probe drives the benchmark agent against the real model, records every
request body and every answer, and then replays the request that came back
empty — directly against the upstream and with parts removed — to find out
what triggers it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from pocket_harness.agent import ScriptedAgent
from pocket_harness.repo_bench import load_repo_tasks

BASE = "http://127.0.0.1:11434/v1"
MODEL = "qwen2.5:7b-instruct"
TASK_ID = sys.argv[1] if len(sys.argv) > 1 else "heldout-03-slice-off-by-one"
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 1

RECORDED: list[dict] = []


def build_conversation(task) -> list[dict]:
    from pocket_harness.agent import SYSTEM_PROMPT, TOOL_SCHEMAS  # noqa: F401

    listing = []
    for path in sorted(task.snapshot.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(task.snapshot)
        if any(part in {"__pycache__", ".pytest_cache"} for part in relative.parts):
            continue
        listing.append(relative.as_posix())
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{task.prompt}\n\n"
                "These are the files in the workspace (relative paths):\n"
                f"{chr(10).join(listing)}\n\n"
                "Change the files that need changing, then run the tests."
            ),
        },
    ]


async def main() -> int:
    tasks = {task.task_id: task for task in load_repo_tasks()}
    task = tasks[TASK_ID]
    workdir = Path(".e2e") / "empty-probe" / TASK_ID
    if workdir.exists():
        import shutil

        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copytree(task.snapshot, workdir)

    upstream = httpx.AsyncClient(timeout=300.0)

    async def forward(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        response = await upstream.post(f"{BASE}/chat/completions", json=body)
        data = response.json()
        message = ((data.get("choices") or [{}])[0]).get("message") or {}
        RECORDED.append(
            {
                "request": body,
                "message": message,
                "empty": not (message.get("tool_calls") or (message.get("content") or "").strip()),
            }
        )
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
        seed=SEED,
    )
    result = await agent.run(task.prompt)
    await client.aclose()
    await upstream.aclose()

    print(f"task={TASK_ID} seed={SEED} stop={result.stop_reason} steps={result.steps}")
    for index, record in enumerate(RECORDED):
        message = record["message"]
        print(
            f"  request {index}: empty={record['empty']} "
            f"messages={len(record['request']['messages'])} "
            f"calls={len(message.get('tool_calls') or [])} "
            f"content={(message.get('content') or '')[:60]!r}"
        )

    empty_index = next((i for i, r in enumerate(RECORDED) if r["empty"]), None)
    if empty_index is None:
        print("\nno empty answer in this run")
        return 0

    body = RECORDED[empty_index]["request"]
    dump = Path(".e2e") / "empty-probe" / "failing-request.json"
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_text(json.dumps(body, indent=2), encoding="utf-8")
    print(f"\n--- replaying request {empty_index} (saved to {dump}) ---")
    async with httpx.AsyncClient(timeout=300.0) as direct:
        for attempt in range(3):
            response = await direct.post(f"{BASE}/chat/completions", json=body)
            data = response.json()
            message = ((data.get("choices") or [{}])[0]).get("message") or {}
            kind = (
                "tool_call"
                if message.get("tool_calls")
                else "empty"
                if not (message.get("content") or "").strip()
                else "text"
            )
            if attempt == 0:
                print(f"  direct replay {attempt}: {kind}")
                print(
                    "    raw: "
                    + json.dumps(
                        {
                            "finish_reason": (data.get("choices") or [{}])[0].get("finish_reason"),
                            "usage": data.get("usage"),
                            "message_keys": sorted(message),
                        }
                    )
                )

        variants = {
            "without_the_last_tool_result": body["messages"][:-1],
            "without_any_tool_result": [
                m for m in body["messages"] if m.get("role") != "tool"
            ],
            "only_the_first_two_messages": body["messages"][:2],
            "only_the_first_tool_result": _keep_first_tool_result(body["messages"]),
            "tool_results_shortened": _shorten_tool_results(body["messages"]),
            "without_repeated_assistant_calls": _drop_repeats(body["messages"]),
            "with_two_tool_results": _keep_n_tool_results(body["messages"], 2),
            "with_the_same_result_three_times": _same_result(body["messages"]),
            "with_the_test_file_result_only": _only_result_of(body["messages"], "test_"),
        }
        for name, messages in variants.items():
            variant = dict(body)
            variant["messages"] = messages
            response = await direct.post(f"{BASE}/chat/completions", json=variant)
            message = ((response.json().get("choices") or [{}])[0]).get("message") or {}
            kind = (
                "tool_call"
                if message.get("tool_calls")
                else "empty"
                if not (message.get("content") or "").strip()
                else "text"
            )
            print(f"  variant {name:<34}: {kind}")

    print("\n--- the failing conversation ---")
    for index, message in enumerate(body["messages"]):
        role = message.get("role")
        content = str(message.get("content") or "")
        calls = message.get("tool_calls") or []
        detail = content.replace("\n", " ")[:70]
        print(
            f"  [{index}] {role:<9} calls={len(calls)} "
            f"{detail!r}"
        )
    return 0


def _keep_n_tool_results(messages: list[dict], count: int) -> list[dict]:
    seen = 0
    out = []
    for message in messages:
        if message.get("role") == "tool":
            if seen >= count:
                continue
            seen += 1
        out.append(message)
    return out


def _same_result(messages: list[dict]) -> list[dict]:
    """Replace every tool result with the same short text."""
    first = next(
        (m.get("content") for m in messages if m.get("role") == "tool"),
        "ok",
    )
    out = []
    for message in messages:
        if message.get("role") == "tool":
            message = dict(message)
            message["content"] = first
        out.append(message)
    return out


def _only_result_of(messages: list[dict], marker: str) -> list[dict]:
    """Keep only the tool result whose content contains ``marker``."""
    out = []
    for message in messages:
        if message.get("role") == "tool" and marker not in str(message.get("content") or ""):
            continue
        out.append(message)
    return out


def _keep_first_tool_result(messages: list[dict]) -> list[dict]:
    seen = False
    out = []
    for message in messages:
        if message.get("role") == "tool":
            if seen:
                continue
            seen = True
        out.append(message)
    return out


def _shorten_tool_results(messages: list[dict]) -> list[dict]:
    out = []
    for message in messages:
        if message.get("role") == "tool":
            message = dict(message)
            message["content"] = "ok"
        out.append(message)
    return out


def _drop_repeats(messages: list[dict]) -> list[dict]:
    """Keep only the first assistant tool call of each identical fingerprint."""
    out = []
    seen: set[str] = set()
    for message in messages:
        calls = message.get("tool_calls") or []
        if calls:
            key = json.dumps(
                [(c.get("function", {}).get("name"), c.get("function", {}).get("arguments"))
                 for c in calls],
                sort_keys=True,
            )
            if key in seen:
                continue
            seen.add(key)
        out.append(message)
    return out


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
