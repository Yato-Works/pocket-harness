"""Tests for the C4 LoopGuard and the C5 ExecutionLedger."""

from __future__ import annotations

from pocket_harness.components.loop_guard import (
    LoopGuard,
    LoopSignal,
    LoopState,
    call_fingerprint,
    error_fingerprint,
)
from pocket_harness.ledger import ExecutionLedger, extract_exit_code, extract_paths

# ------------------------------------------------------------------- C4


def test_repeated_identical_call_is_detected() -> None:
    """The same call repeated three times raises the loop signal."""
    guard = LoopGuard(repeated_call_threshold=3)
    state = LoopState()
    events = []
    for _ in range(3):
        events += guard.observe_call(state, "read_file", '{"path": "a.py"}')
    assert any(e.signal is LoopSignal.REPEATED_CALL for e in events)


def test_different_calls_are_not_a_loop() -> None:
    """Different calls do not trigger the loop signal."""
    guard = LoopGuard(repeated_call_threshold=3)
    state = LoopState()
    events = []
    for i in range(5):
        events += guard.observe_call(state, "read_file", f'{{"path": "{i}.py"}}')
    assert not any(e.signal is LoopSignal.REPEATED_CALL for e in events)


def test_key_order_does_not_change_the_fingerprint() -> None:
    """The same arguments in a different key order have the same fingerprint."""
    a = call_fingerprint("t", '{"a": 1, "b": 2}')
    b = call_fingerprint("t", '{"b": 2, "a": 1}')
    assert a == b


def test_whitespace_does_not_change_the_fingerprint() -> None:
    """Whitespace differences do not create a false loop."""
    assert call_fingerprint("t", '{"a": 1}') == call_fingerprint("t", '{ "a" : 1 }')


def test_different_arguments_change_the_fingerprint() -> None:
    """Semantically different arguments produce different fingerprints."""
    assert call_fingerprint("t", '{"a": 1}') != call_fingerprint("t", '{"a": 2}')


def test_oscillation_is_detected() -> None:
    """An A-B-A-B pattern is detected."""
    guard = LoopGuard(repeated_call_threshold=3)
    state = LoopState()
    events = []
    for i in range(4):
        events += guard.observe_call(state, "t", f'{{"i": {i % 2}}}')
    assert any(e.signal is LoopSignal.OSCILLATION for e in events)


def test_step_limit_is_enforced() -> None:
    """The hard step limit stops the task."""
    guard = LoopGuard(max_steps_per_task=3)
    state = LoopState()
    events = []
    for i in range(5):
        events += guard.observe_call(state, "t", f'{{"i": {i}}}')
    assert any(e.signal is LoopSignal.STEP_LIMIT for e in events)
    assert events[-1].action == "stop"


def test_repeated_error_is_detected() -> None:
    """The same error repeated is detected."""
    guard = LoopGuard(repeated_call_threshold=3)
    state = LoopState()
    events = []
    for _ in range(3):
        events += guard.observe_error(state, "FileNotFoundError: /tmp/missing.txt")
    assert any(e.signal is LoopSignal.REPEATED_ERROR for e in events)


def test_error_fingerprint_ignores_volatile_numbers() -> None:
    """Line numbers do not make two identical errors look different."""
    a = error_fingerprint("Error at line 12: boom")
    b = error_fingerprint("Error at line 99: boom")
    assert a == b


def test_guard_only_observes() -> None:
    """The guard only observes; it never changes the call it was given."""
    guard = LoopGuard()
    state = LoopState()
    guard.observe_call(state, "t", '{"a": 1}')
    assert state.step == 1
    assert len(state.call_fingerprints) == 1


# ------------------------------------------------------------------- C5


def test_ledger_records_only_observable_facts() -> None:
    """A tool call records the tool name and argument hash, not a conclusion."""
    ledger = ExecutionLedger()
    entry = ledger.record_tool_call(1, "read_file", {"path": "a.py"})
    assert entry.tool == "read_file"
    assert entry.args_hash
    assert entry.paths == ["a.py"]


def test_ledger_does_not_claim_a_file_was_changed() -> None:
    """The ledger does not infer that a file was modified."""
    ledger = ExecutionLedger()
    entry = ledger.record_tool_call(1, "write_to_file", {"path": "a.py", "content": "x"})
    assert entry.kind == "tool_call"
    # There is no "changed" or "modified" field: the ledger only reports facts.
    assert "changed" not in entry.as_dict()
    assert "modified" not in entry.as_dict()


def test_ledger_extracts_explicit_commands() -> None:
    """A command is taken from the structured arguments."""
    ledger = ExecutionLedger()
    entry = ledger.record_tool_call(1, "execute_command", {"command": "pytest -q"})
    assert entry.command == "pytest -q"


def test_ledger_extracts_exit_code_from_result() -> None:
    """An explicitly reported exit status is captured."""
    ledger = ExecutionLedger()
    entry = ledger.record_tool_result(1, "execute_command", "exit code: 1\n")
    assert entry.exit_code == 1


def test_exit_code_extraction_handles_common_formats() -> None:
    """The exit-code patterns are recognized."""
    assert extract_exit_code("Exit code: 0") == 0
    assert extract_exit_code("status=2") == 2
    assert extract_exit_code("no status here") is None


def test_ledger_counts_unresolved_errors() -> None:
    """Repeated unresolved errors are counted by fingerprint."""
    ledger = ExecutionLedger()
    ledger.record_error(1, "failed", fingerprint="abc")
    ledger.record_error(2, "failed", fingerprint="abc")
    summary = ledger.summary()
    assert summary["unresolved_error_count"] == 2
    assert summary["distinct_unresolved_errors"] == 1


def test_ledger_summary_is_deterministic() -> None:
    """The summary is sorted and reproducible."""
    ledger = ExecutionLedger()
    ledger.record_tool_call(1, "b_tool", {})
    ledger.record_tool_call(2, "a_tool", {})
    summary = ledger.summary()
    assert list(summary["tool_counts"]) == ["a_tool", "b_tool"]
    assert summary["steps"] == 2


def test_paths_are_only_taken_from_path_like_keys() -> None:
    """A non-path value is not recorded as a path."""
    assert extract_paths({"command": "ls"}) == []
    assert extract_paths({"path": "a.py"}) == ["a.py"]

