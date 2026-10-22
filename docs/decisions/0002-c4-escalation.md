# ADR 0002 — What C4 does when it detects a loop (escalation)

**Status:** accepted
**Date:** 2026-10-22
**Affects:** `src/pocket_harness/components/loop_guard.py`,
`src/pocket_harness/pipeline.py`, `src/pocket_harness/proxy.py`

## Context

Plan.md §6 C4 defines four loop signals (repeated call, A-B-A-B oscillation,
repeated error, task budget) and three escalation steps:

1. *"Append a concise deterministic warning to the next request or return a
   synthetic tool result explaining the repetition."*
2. *"For an exact repeated call, prevent silent endless repetition by requiring
   the agent to change the call or stop; do not modify tool permissions."*
3. *"If the same task remains stuck or hits the hard budget, stop and surface a
   clear status to the user."*

The implementation before this decision detected all four signals, recorded
them in the evidence log and the ledger — and then **did nothing**. The
`LoopGuard.warning_text()` helper existed but was never called. The measured
consequence was 14 detections and 0 interventions in the benchmark: the
mechanism was a reporter, not an actor.

## Decision

**C4 escalates by appending a deterministic warning to the next request, and
the detection is derived from the request itself.**

1. *Detection is request-derived.* The pipeline counts the fingerprints of the
   tool calls in the canonical request's message history and applies the same
   repeat and oscillation rules as the runtime guard. The state is therefore a
   pure function of `(input, configuration, version)`.
2. *The warning is part of the transformation.* When the rules fire, the
   pipeline appends one deterministic `user` message carrying the warning text
   and records a `C4.repeat_warning` rule. The warning therefore appears in the
   transformed request, in the output hash and in the evidence log.
3. *The warning runs before C2.* The context budgeter runs after the warning, so
   the budget still holds for the message the proxy added.
4. *The runtime guard stays.* The session-scoped guard keeps emitting
   `loop.detected` events for the ledger and for `GET /pocket/status`, including
   the task-budget and wall-clock signals that cannot be derived from a request.

The warning text is the existing deterministic `LoopGuard.warning_text()`: it
names the signal and ends with *"Change the approach or stop; repeating the
identical call will not make progress."*

### What C4 deliberately does **not** do

* **It does not abort the client's request.** Returning an error instead of a
  completion breaks the client's API contract, which Plan.md §1.2 requires to be
  preserved. The status is surfaced through the evidence log, the ledger and
  `GET /pocket/status` instead.
* **It does not modify tool permissions, the model, the temperature or the
  seed.** Plan.md §6 C4 forbids it.
* **It does not fabricate a tool result.** The proxy does not own tool
  dispatch, so it cannot answer a tool call on the client's behalf.
* **It does not repair the arguments of a call it only observed.** That is C3's
  job (ADR 0001).

## Consequences

* A repeated call now changes what the model sees: the escalation is
  observable in the transformed request, in the output hash and in the replay.
* Because the detection is request-derived, a replay of a captured request
  reproduces the warning exactly — the escalation does not break the
  reproducibility contract.
* The warning is re-derived on every request while the condition holds, so the
  model is told again on the next turn instead of once.
* A client that does not echo its tool-call history gets no warning (there is
  nothing to derive the state from); the runtime guard still detects and
  reports it. This limitation is stated in the README.
