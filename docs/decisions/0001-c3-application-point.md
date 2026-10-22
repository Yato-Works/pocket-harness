# ADR 0001 — Where a repaired tool call is applied (C3)

**Status:** accepted
**Date:** 2026-10-22
**Affects:** `src/pocket_harness/proxy.py`, `src/pocket_harness/components/tool_call_repair.py`

## Context

C3 validates the tool calls of a response against the **client's original**
tool schemas and repairs structural defects (Plan.md §6 C3). The question this
record answers is *where* the repaired call is applied.

The implementation before this decision repaired the call, recorded the repair in
the evidence log and then **did not** use the repaired arguments for the client:
the client received the original, invalid call. The consequence was measured:
C3 could change the evidence but never a task outcome, so the mechanism could
not influence any result — 0 repairs were observed in the benchmark.

Two parts of the design speak to the question:

* Plan.md §4.3 (streaming contract): *"Buffer tool-call deltas until the tool
  call is complete enough to parse and validate. Only then emit the
  **reconstructed** tool call in the client protocol."* The reconstructed call is
  the one the client is meant to receive.
* Plan.md §6 C3 §4: *"Validate against the original JSON Schema. Record original
  and repaired hashes plus a structured diff."* A repaired hash only exists if a
  repaired artefact exists.

## Decision

**A repaired tool call is applied to the tool call the proxy emits to the
client**, under these conditions and no others:

1. the repair was produced by C3 itself,
2. the repaired arguments validate against the client's original schema,
3. the *original* arguments were **not** usable as they stood — they did not
   parse, or they did not validate against the schema,
4. the repair was one of the permitted kinds: JSON parse tolerance (code
   fences, trailing commas, single quotes), exact / case-insensitive / alias
   tool-name resolution, lossless schema conversions, or an explicit schema
   default that the profile permits.

Condition 3 matters: a re-serialisation of *already valid* JSON (for example
`{"path":"calc.py"}` becoming `{"path": "calc.py"}`) is not a repair. The
client can use the model's own text, so the proxy must not touch it and must
not report a repair. A benchmark that counts such re-serialisations would
inflate the intervention coverage with work that changed nothing.

Everything else is preserved byte for byte: the call id, the index, the tool
name and the argument *values* that C3 did not change. A call that could not be
repaired is passed through unchanged and recorded as `failed`. The proxy never
drops a call, never invents one and never renames a destructive tool.

The original text, the repaired text, both hashes and the structured diff are
recorded in the `toolcall.validated` event, so the change is auditable.

## Consequences

* The client's dispatch receives a call whose arguments validate. This is the
  behaviour Plan.md §4.3 asks for and the only way C3 can affect an outcome.
* The evidence log keeps both versions, so the repair is never silent.
* The response body is now a *derived* artefact. The request hashes are
  unaffected: the repair depends on the response and the client's tool list,
  never on the request.
* **Not** applied on the streaming path. There the upstream's SSE lines are
  forwarded verbatim, and rewriting a streamed tool call would require
  buffering the deltas and re-emitting them — a change to the stream's timing
  and to the client's incremental parsing. Until that is implemented, a streamed
  tool call is validated and reported but not rewritten. This limitation is
  stated in the README.

## Guardrails that remain in force

* Fuzzy tool-name matching stays off by default and never applies to
  destructive tools.
* Argument values are never invented; a missing required argument is a failure,
  not a guess.
* C1 is not an authorisation boundary: a call to a tool that C1 hid is still
  validated against the client's original schema.
