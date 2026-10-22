"""The scripted coding agent used by the repository benchmark.

The agent is deliberately small and completely deterministic apart from the
model: it sends the task prompt, executes the tool calls the model returns
inside a throw-away worktree, and stops when the model stops or the published
budget is exhausted.

The *same* agent code drives both conditions of the comparison. The only
difference between them is the base URL the agent talks to:

* ``baseline``  the upstream inference endpoint, unmodified;
* ``harness``   the Pocket Harness proxy, which applies C0/C1/C2/C6.

That is what Plan.md §11.2 requires: the endpoint/proxy path is the intended
difference and everything else stays identical.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from pocket_harness.toolcall_text import find_text_tool_call

SYSTEM_PROMPT = (
    "You are a careful coding assistant working inside a small Python project. "
    "Use the provided tools to inspect and change the project, then run the tests."
)

#: The system prompt is a *declared condition* of the benchmark: it is part of
#: the client configuration, it is identical in both conditions, and it is
#: recorded in the run manifest. It also has to be treated as brittle: with
#: qwen2.5:7b-instruct at temperature 0, some wordings make the model emit a
#: generation that decodes into nothing once it has read two or more files
#: (see scripts/probe_empty_system.py). The benchmark therefore records the
#: prompt and its hash with every run.
SYSTEM_PROMPT_SHA256 = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the workspace.",
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
            "description": "Write UTF-8 text to a file in the workspace, creating it if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the project's test command and return its exit status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

#: How much of a file read is returned to the model.
MAX_READ_CHARS = 8000
#: How much of the test output is returned to the model.
MAX_TEST_OUTPUT_CHARS = 4000

#: The nudge the client sends when the upstream answered with nothing. The
#: upstream sometimes emits a generation that decodes into an empty message
#: (class R); asking the model to continue recovers it (see
#: scripts/probe_empty_recovery.py). It is a client behaviour, it is bounded and
#: it is identical in both conditions.
EMPTY_RETRY_NUDGE = "Your last response was empty. Please continue with the tool call."

#: Volatile values that must not enter the model's context: a duration in the
#: test output changes from run to run and makes the whole conversation (and
#: therefore the result) irreproducible. The paths are replaced for the same
#: reason -- the worktree lives in a temporary directory whose name changes.
_DURATION_RE = re.compile(r"\bin \d+(?:\.\d+)?s\b")
_ISO_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?\b")


def sanitize_tool_output(text: str, workdir: Path | None = None) -> str:
    """Replace the volatile parts of a tool result with stable placeholders."""
    if not text:
        return text
    cleaned = _DURATION_RE.sub("in <duration>", text)
    cleaned = _ISO_TIMESTAMP_RE.sub("<timestamp>", cleaned)
    if workdir is not None:
        for candidate in (str(workdir), str(workdir).replace("\\", "/")):
            if candidate:
                cleaned = cleaned.replace(candidate, "<workspace>")
    return cleaned


@dataclass
class ToolOutcome:
    """The result of one executed tool call."""

    name: str
    arguments: dict[str, Any]
    ok: bool
    content: str
    error_class: str = ""


@dataclass
class AgentRun:
    """Everything one agent run produced."""

    steps: int = 0
    wall_seconds: float = 0.0
    ttft_seconds: float | None = None
    time_to_first_tool_call: float | None = None
    stop_reason: str = "model_stopped"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    invalid_tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    upstream_error: str | None = None
    upstream_status: int | None = None
    context_overflow: bool = False
    #: The upstream delivered a tool call as text instead of a structured call.
    tool_call_delivered_as_text: bool = False
    #: How often an empty answer was recovered by asking the model to continue.
    empty_retries: int = 0
    #: The upstream returned an empty completion.
    empty_completion: bool = False
    final_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "wall_seconds": round(self.wall_seconds, 4),
            "ttft_seconds": self.ttft_seconds,
            "time_to_first_tool_call": self.time_to_first_tool_call,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls,
            "invalid_tool_calls": self.invalid_tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "upstream_error": self.upstream_error,
            "upstream_status": self.upstream_status,
            "context_overflow": self.context_overflow,
            "tool_call_delivered_as_text": self.tool_call_delivered_as_text,
            "empty_completion": self.empty_completion,
            "empty_retries": self.empty_retries,
        }


class ToolBox:
    """Executes the agent's tool calls inside one worktree."""

    def __init__(self, workdir: Path, test_command: list[str], *, test_timeout: int = 120) -> None:
        self.workdir = Path(workdir).resolve()
        self.test_command = list(test_command)
        self.test_timeout = test_timeout
        self.test_exit_code: int | None = None

    def _resolve(self, relative: str) -> Path | None:
        """Resolve a workspace-relative path, refusing to escape the worktree."""
        if not relative or os.path.isabs(relative):
            return None
        candidate = (self.workdir / relative).resolve()
        try:
            candidate.relative_to(self.workdir)
        except ValueError:
            return None
        return candidate

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Execute one tool call and return what the model gets to see."""
        if name == "read_file":
            path = self._resolve(str(arguments.get("path", "")))
            if path is None:
                return ToolOutcome(name, arguments, False, "error: invalid path", "path.invalid")
            if not path.is_file():
                return ToolOutcome(
                    name, arguments, False, f"error: no such file: {arguments.get('path')}", "file.missing"
                )
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return ToolOutcome(name, arguments, False, f"error: {exc}", "file.unreadable")
            if len(text) > MAX_READ_CHARS:
                text = text[:MAX_READ_CHARS] + "\n... (truncated)"
            return ToolOutcome(name, arguments, True, text)

        if name == "write_file":
            path = self._resolve(str(arguments.get("path", "")))
            if path is None:
                return ToolOutcome(name, arguments, False, "error: invalid path", "path.invalid")
            content = arguments.get("content")
            if not isinstance(content, str):
                return ToolOutcome(name, arguments, False, "error: content must be a string", "args.invalid")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            except OSError as exc:
                return ToolOutcome(name, arguments, False, f"error: {exc}", "file.unwritable")
            return ToolOutcome(
                name,
                arguments,
                True,
                f"wrote {len(content)} characters to {arguments.get('path')}",
            )

        if name == "run_tests":
            return self._run_tests()

        return ToolOutcome(name, arguments, False, f"error: unknown tool {name!r}", "tool.unknown")

    def _run_tests(self) -> ToolOutcome:
        """Run the project's test command and return its exit status."""
        try:
            completed = subprocess.run(  # noqa: S603 - the command comes from the task file
                self.test_command,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=self.test_timeout,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return ToolOutcome("run_tests", {}, False, "error: the test command timed out", "test.timeout")
        except OSError as exc:
            return ToolOutcome("run_tests", {}, False, f"error: {exc}", "test.unavailable")
        self.test_exit_code = completed.returncode
        output = (completed.stdout or "") + (completed.stderr or "")
        if len(output) > MAX_TEST_OUTPUT_CHARS:
            output = output[-MAX_TEST_OUTPUT_CHARS:]
        return ToolOutcome(
            "run_tests",
            {},
            completed.returncode == 0,
            f"exit code {completed.returncode}\n{output}",
        )


def validate_tool_call(
    schemas: dict[str, dict[str, Any]], name: str, arguments_text: str
) -> tuple[bool, str]:
    """Validate a tool call *without* repairing it.

    This measures the tool calls as the model produced them ("before C3"), so
    that the effect of C3 can be reported (Plan.md §11.4).
    """
    if name not in schemas:
        return False, "M.name"
    try:
        arguments = json.loads(arguments_text or "{}")
    except json.JSONDecodeError:
        return False, "M.parse"
    if not isinstance(arguments, dict):
        return False, "M.shape"
    from jsonschema import Draft202012Validator

    errors = sorted(
        Draft202012Validator(schemas[name] or {}).iter_errors(arguments),
        key=lambda e: list(e.path),
    )
    if errors:
        return False, "M.schema"
    return True, ""


class ScriptedAgent:
    """Drives one task against one endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        workdir: Path,
        test_command: list[str],
        client: httpx.AsyncClient,
        max_steps: int = 12,
        max_wall_seconds: int = 300,
        request_timeout: int = 300,
        list_workspace: bool = True,
        temperature: float | None = 0.0,
        seed: int | None = None,
        max_empty_retries: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.workdir = Path(workdir)
        self.toolbox = ToolBox(self.workdir, test_command)
        self.client = client
        self.max_steps = max_steps
        self.max_wall_seconds = max_wall_seconds
        self.request_timeout = request_timeout
        self.list_workspace = list_workspace
        #: The sampling settings are part of the comparison conditions
        #: (Plan.md §11.2): they are identical in both conditions and pinned so
        #: that the upstream answers the same request the same way.
        self.temperature = temperature
        self.seed = seed
        #: How often an empty answer may be recovered by asking again.
        self.max_empty_retries = max_empty_retries
        self.schemas = {
            tool["function"]["name"]: tool["function"]["parameters"] for tool in TOOL_SCHEMAS
        }

    def workspace_listing(self) -> str:
        """Return the workspace file list, the way a real coding agent shows it.

        Without it the model has to guess where the files live and frequently
        invents a path (``src/calc.py``) or stops after reading. The listing is
        deterministic: sorted, relative, without caches.
        """
        if not self.list_workspace:
            return ""
        entries: list[str] = []
        for path in sorted(self.workdir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.workdir)
            parts = relative.parts
            if any(part in {"__pycache__", ".pytest_cache", ".git"} for part in parts):
                continue
            entries.append(relative.as_posix())
        return "\n".join(entries)

    async def run(self, prompt: str) -> AgentRun:
        """Run the task and return what happened."""
        run = AgentRun()
        listing = self.workspace_listing()
        user_content = prompt
        if listing:
            user_content = (
                f"{prompt}\n\n"
                "These are the files in the workspace (relative paths):\n"
                f"{listing}\n\n"
                "Change the files that need changing, then run the tests."
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        started = time.monotonic()

        while True:
            if run.steps >= self.max_steps:
                run.stop_reason = "step_limit"
                break
            if time.monotonic() - started > self.max_wall_seconds:
                run.stop_reason = "wall_limit"
                break

            request_started = time.monotonic()
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "tools": TOOL_SCHEMAS,
                "max_tokens": 1024,
            }
            if self.temperature is not None:
                payload["temperature"] = self.temperature
            if self.seed is not None:
                payload["seed"] = self.seed
            try:
                response = await self.client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    timeout=self.request_timeout,
                )
            except httpx.HTTPError as exc:
                run.upstream_error = f"{type(exc).__name__}: {exc}"
                run.stop_reason = "transport_error"
                break

            elapsed = time.monotonic() - request_started
            if run.ttft_seconds is None:
                run.ttft_seconds = round(elapsed, 4)
            if response.status_code != 200:
                run.upstream_status = response.status_code
                try:
                    body = response.json()
                    detail = body.get("error", {})
                    message = detail.get("message") if isinstance(detail, dict) else str(detail)
                except Exception:  # noqa: BLE001 - the upstream may not answer JSON
                    message = response.text
                run.upstream_error = str(message or f"HTTP {response.status_code}")
                if response.status_code == 413 or "context" in str(message).lower():
                    run.context_overflow = True
                run.stop_reason = "upstream_error"
                break

            data = response.json()
            usage = data.get("usage") or {}
            run.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            run.completion_tokens += int(usage.get("completion_tokens") or 0)
            run.total_tokens += int(usage.get("total_tokens") or 0)

            choices = data.get("choices") or []
            if not choices:
                run.upstream_error = "the upstream returned no choices"
                run.stop_reason = "upstream_error"
                break
            message = choices[0].get("message") or {}
            run.steps += 1
            run.final_text = message.get("content") or ""
            calls = message.get("tool_calls") or []
            if not calls:
                # The upstream sometimes answers with a generation that decodes
                # into nothing (class R). A client can recover from that by
                # asking the model to continue; the behaviour is identical in
                # both conditions and every retry is counted.
                if not run.final_text.strip() and run.empty_retries < self.max_empty_retries:
                    run.empty_retries += 1
                    messages.append(message)
                    messages.append({"role": "user", "content": EMPTY_RETRY_NUDGE})
                    continue
                # The upstream sometimes delivers a tool call as text. That is
                # an upstream-side delivery fault, not a model reasoning
                # failure, and the run must say so instead of silently ending.
                if find_text_tool_call(run.final_text) is not None:
                    run.tool_call_delivered_as_text = True
                    run.stop_reason = "tool_call_delivered_as_text"
                elif not run.final_text.strip():
                    run.empty_completion = True
                    run.stop_reason = "empty_completion"
                else:
                    run.stop_reason = "model_stopped"
                break
            if run.time_to_first_tool_call is None:
                run.time_to_first_tool_call = round(time.monotonic() - started, 4)

            messages.append(message)
            for call in calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                arguments_text = str(function.get("arguments") or "")
                valid, error_class = validate_tool_call(self.schemas, name, arguments_text)
                if not valid:
                    run.invalid_tool_calls += 1
                try:
                    arguments = json.loads(arguments_text or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                outcome = self.toolbox.execute(name, arguments)
                run.tool_calls.append(
                    {
                        "name": name,
                        "arguments_sha256": _hash(arguments_text),
                        "valid_before_c3": valid,
                        "error_class": error_class,
                        "ok": outcome.ok,
                        "tool_error_class": outcome.error_class,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": name,
                        "content": sanitize_tool_output(outcome.content, self.workdir),
                    }
                )

        run.wall_seconds = time.monotonic() - started
        return run


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def prepare_worktree(snapshot: Path, destination: Path) -> Path:
    """Copy a task snapshot into a fresh worktree."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(snapshot, destination)
    return destination


__all__ = [
    "AgentRun",
    "SYSTEM_PROMPT",
    "ScriptedAgent",
    "TOOL_SCHEMAS",
    "ToolBox",
    "ToolOutcome",
    "prepare_worktree",
    "validate_tool_call",
]
