# ADR 0005 — The client answers an unusable tool call with a precise, schema-derived error

**Status:** accepted (default behaviour of the benchmark client)
**Date:** 2026-10-22
**Affects:** `src/pocket_harness/agent.py` (the benchmark client),
`tests/test_tool_error_contract.py`

## Context

The measured failure of the local baseline is dominated by class **M**: the
model emits a tool call whose arguments do not satisfy the client's schema —
most often `write_file` with *no arguments at all*. Plan.md §6 C3 §3 forbids
inventing the missing values, and §6 C3 §6's corrective retry was measured to
make the outcome *worse* (ADR 0004).

The remaining lever is what the **client** answers with. The benchmark client
used to answer with a generic string (`error: invalid path`), which tells the
model almost nothing.

## Decision

**The client refuses an unusable call before executing it and answers with a
precise, deterministic error derived from the client's own tool schema.**

The error states:

* which required arguments are missing, with their expected type;
* which argument has the wrong type, and which type it actually has;
* which argument is outside the allowed values, and what they are;
* that the call was **not executed**;
* that the call has to be sent again with arguments that satisfy the schema.

It never contains a guessed value, path or command: the text is derived from
the schema alone, and the same call always produces the same error.

The client also stops a task that repeats the *same* failing call
`max_identical_failures` times (default 3) instead of burning the step
budget, and reports a status that says what happened.

## Why this is a client behaviour and not a proxy feature

The proxy does not own tool dispatch: the client executes the call and builds
the next request. Only the client can therefore answer a tool call with a
tool result. The proxy keeps its own side of the contract: it validates the
call, records the outcome, and (when enabled) repairs or refuses it.

## Consequences

* The invalid tool calls that reach the model drop sharply (54 → 9 in the
  measured A/B) and the targeted failure class turns into completed tasks in
  the cases that were measured (5 runs moved from an M failure to a pass).
* The error text is part of the conversation, so it changes every later turn.
  The A/B measurement shows that this moves runs in **both** directions: 5 runs
  were fixed and 6 previously passing runs were lost to a different upstream
  failure. The net effect on 18 runs is indistinguishable from zero — see
  [`PERFORMANCE-GATE-AUDIT.md`](PERFORMANCE-GATE-AUDIT.md) §17 for the
  sample-size argument that follows from it.
* The behaviour is symmetric: both conditions run the same client.
