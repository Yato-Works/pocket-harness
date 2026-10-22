"""C5 — Execution ledger.

A deterministic snapshot of *observable* actions and unresolved failures.

The ledger may contain only what the trace actually shows: tool name, argument/result
hashes, reported command and exit code, explicit file paths taken from structured
arguments or results, step/time counters, and unresolved error fingerprints.

It must **not** infer that a file was changed merely because an agent intended to edit
it. Nothing here is produced by an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pocket_harness.hashing import hash_obj


@dataclass(slots=True)
class LedgerEntry:
    """One ledger record."""

    step: int
    kind: str  # "tool_call" | "tool_result" | "error" | "warning"
    tool: str | None = None
    args_hash: str | None = None
    result_hash: str | None = None
    command: str | None = None
    exit_code: int | None = None
    paths: list[str] = field(default_factory=list)
    error_fingerprint: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "kind": self.kind,
            "tool": self.tool,
            "args_hash": self.args_hash,
            "result_hash": self.result_hash,
            "command": self.command,
            "exit_code": self.exit_code,
            "paths": list(self.paths),
            "error_fingerprint": self.error_fingerprint,
            "detail": self.detail,
        }


def extract_paths(arguments: dict[str, Any]) -> list[str]:
    """Extract explicit path-like values from structured tool arguments.

    Only values that are unambiguously path-shaped are returned; no inference is made
    about whether the path was actually modified.
    """
    found: list[str] = []
    path_keys = {"path", "file_path", "filepath", "filename", "file", "target_file", "abs_path"}
    for key, value in arguments.items():
        if key.lower() in path_keys and isinstance(value, str) and value.strip():
            found.append(value)
    return sorted(set(found))


def extract_command(arguments: dict[str, Any]) -> str | None:
    """Extract an explicitly reported command from structured tool arguments."""
    for key in ("command", "cmd", "script"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def extract_exit_code(text: str) -> int | None:
    """Extract an explicitly reported exit status from a tool result text."""
    import re

    match = re.search(r"(?im)^\s*(?:exit(?:ed)?(?:\s+code)?|status)\s*[:=]\s*(-?\d+)\s*$", text or "")
    if match:
        return int(match.group(1))
    return None


class ExecutionLedger:
    """Accumulates the observable execution record of a task."""

    def __init__(self, max_entries: int = 2000) -> None:
        self.entries: list[LedgerEntry] = []
        self.max_entries = max_entries
        self.unresolved_errors: dict[str, int] = {}

    def record_tool_call(self, step: int, tool: str, arguments: dict[str, Any]) -> LedgerEntry:
        """Record a tool call with only the observable facts."""
        entry = LedgerEntry(
            step=step,
            kind="tool_call",
            tool=tool,
            args_hash=hash_obj(arguments),
            command=extract_command(arguments),
            paths=extract_paths(arguments),
        )
        self._append(entry)
        return entry

    def record_tool_result(self, step: int, tool: str, result_text: str) -> LedgerEntry:
        """Record a tool result with only the observable facts."""
        entry = LedgerEntry(
            step=step,
            kind="tool_result",
            tool=tool,
            result_hash=hash_obj(result_text),
            exit_code=extract_exit_code(result_text),
        )
        self._append(entry)
        return entry

    def record_error(self, step: int, detail: str, fingerprint: str | None = None) -> LedgerEntry:
        """Record an unresolved error."""
        entry = LedgerEntry(
            step=step,
            kind="error",
            detail=detail[:500],
            error_fingerprint=fingerprint,
        )
        self._append(entry)
        if fingerprint:
            self.unresolved_errors[fingerprint] = self.unresolved_errors.get(fingerprint, 0) + 1
        return entry

    def record_warning(self, step: int, detail: str) -> LedgerEntry:
        """Record a deterministic warning emitted by another component."""
        entry = LedgerEntry(step=step, kind="warning", detail=detail[:500])
        self._append(entry)
        return entry

    def _append(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            del self.entries[: len(self.entries) - self.max_entries]

    def snapshot(self) -> dict[str, Any]:
        """Return a deterministic snapshot of the ledger."""
        return {
            "entries": [entry.as_dict() for entry in self.entries],
            "unresolved_errors": dict(sorted(self.unresolved_errors.items())),
            "step_count": len([e for e in self.entries if e.kind == "tool_call"]),
        }

    def summary(self) -> dict[str, Any]:
        """Return a compact summary suitable for ``pocket status``."""
        tools: dict[str, int] = {}
        for entry in self.entries:
            if entry.kind == "tool_call" and entry.tool:
                tools[entry.tool] = tools.get(entry.tool, 0) + 1
        return {
            "steps": len([e for e in self.entries if e.kind == "tool_call"]),
            "tool_counts": dict(sorted(tools.items())),
            "unresolved_error_count": sum(self.unresolved_errors.values()),
            "distinct_unresolved_errors": len(self.unresolved_errors),
        }
