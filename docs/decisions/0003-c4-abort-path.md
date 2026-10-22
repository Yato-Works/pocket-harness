# ADR 0003 — Whether the proxy may refuse a request that repeats a call

**Status:** accepted (implemented, opt-in, **default off**)
**Date:** 2026-10-22
**Affects:** `src/pocket_harness/proxy.py`,
`src/pocket_harness/config.py`, `src/pocket_harness/components/loop_guard.py`

## Context

Plan.md §6 C4 lists three escalation steps:

1. append a deterministic warning to the next request (implemented, ADR 0002),
2. *"For an exact repeated call, prevent silent endless repetition by requiring
   the agent to change the call or stop; do not modify tool permissions."*
3. *"If the same task remains stuck or hits the hard budget, stop and surface a
   clear status to the user."*

Step 2 and 3 imply that the proxy may *stop*. The benchmark shows why this
matters: on the measured tasks the model emits a `write_file` call with **no
arguments at all**, C3 correctly refuses to invent them (§6 C3 §3), the agent
cannot execute the call, and the model repeats the *identical* broken call up
to six times in one run. One run burned twelve steps on the same dead call.

## The client-contract check

Before implementing a refusal, the question is whether it breaks the contract
that Plan.md §1.2 asks to preserve.

* The proxy already answers with typed errors: `400 client_invalid_request`,
  `400 client_unsupported_field`, `413 context_overflow`, `502
  upstream_unavailable`. A refusal is therefore part of the existing surface,
  not a new one.
* The refusal must be a **4xx**, never a 5xx: an OpenAI-compatible client
  retries 5xx, and a retried refusal would loop on the client side.
* The refusal must be *deterministic*: it is derived from the request history,
  so the same request is always refused and a replay reproduces it.
* The refusal must not modify tool permissions, the model, the temperature or
  the seed — the same prohibitions as the warning escalation.
* The client receives a clear status: the error body names the repeated call
  and says that the agent must change the call or stop. That is exactly what
  Plan.md §6 C4 step 3 asks for ("surface a clear status to the user").

## Decision

**The proxy may refuse a request whose history shows an exact repeated tool
call, but only when the operator opts in** (`[loop_guard] abort_on_repeat =
true`). The default stays **off**, because:

* the refusal cannot make a failing task pass — it can only stop the waste —
  and a refusal that ends a run the model would have recovered from would
  *reduce* the completion rate;
* the measurement has to decide that, and the measurement is the next step.

The refusal:

* uses the same request-derived detection as the warning (ADR 0002), so it is
  reproducible;
* answers `409` with the error type `loop_repeated_call`;
* is recorded in the evidence log as `loop.aborted` with the offending
  fingerprint and the repetition count;
* never fires for a repetition that is *not* exact (the fingerprint covers the
  tool name and the normalised arguments).

## Consequences

* With `abort_on_repeat = false` (the default) the behaviour is unchanged: the
  warning escalation of ADR 0002.
* With it enabled, a repeated call costs one refused request instead of the
  remaining step budget, and the run ends with a status the client can show.
* The measurement of both settings is in
  [`PERFORMANCE-GATE-AUDIT.md`](PERFORMANCE-GATE-AUDIT.md) §15.5.
