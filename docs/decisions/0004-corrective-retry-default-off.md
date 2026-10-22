# ADR 0004 — The corrective inference retry is opt-in, because it measurably hurts

**Status:** accepted (implemented, **default off**)
**Date:** 2026-10-22
**Affects:** `src/pocket_harness/proxy.py`, `src/pocket_harness/config.py`

## Context

Plan.md §6 C3 §6 describes a mechanism that was specified but never
implemented:

> "If local repair fails, an optional one-shot corrective inference retry may
> be issued only while the configured retry budget permits it. This is a
> repeated normal model request—not a hidden judge or summarizer—and it must be
> counted in request/token/cost accounting."

The configuration had the budget fields
(`max_corrective_retries_per_request`, `max_corrective_retries_per_task`) but
nothing used them. The measurement showed that the dominant failure class of
the local baseline is **M** — the model emits a tool call whose arguments do
not validate against the client's schema (most often `write_file` with *no
arguments at all*). §6 C3 §3 forbids inventing the missing arguments, so the
corrective retry is the only sanctioned way to address that class.

It was therefore implemented — and then measured on the fresh, untouched
held-out set.

## The measurement

Three runs of the same held-out set (3 tasks × 3 seeds × 2 conditions), with
everything else identical (temperature 0, fixed seeds, same agent, same
configuration, reproducible 36/36):

| configuration | baseline | harness | paired difference |
|---|---|---|---|
| corrective retry **on** (the predeclared condition) | 3/9 | **0/9** | **−33.3 points** |
| corrective retry off (C4 warning on) | 3/9 | 3/9 | 0.0 points |
| corrective retry off, C4 warning off (pure pass-through) | 3/9 | 3/9 | 0.0 points |

The retry is the harmful intervention: with it the harness condition scores
**zero** while the baseline scores three, and the number of tool calls that
still fail validation afterwards rises from 21 to 54.

## Why it hurts

The retry replaces the model's answer with the answer of a *second* request
that carries a corrective hint. Two things go wrong:

1. The model's first answer is often a *usable* plan that C3 rejected for a
   structural reason (a missing required argument). The retry does not repair
   that answer — it asks for a new one — and the new answer is often a
   different, worse plan.
2. The corrective hint tells the model that its call was invalid, which pushes
   it into a second attempt with less context than the first, and the loop
   guard then reports the repetition.

## Decision

**The corrective retry stays implemented but is disabled by default**
(`max_corrective_retries_per_request = 0`), and the operator has to enable it
explicitly. This is consistent with Plan.md §6 C3 §6, which calls the retry
*optional*, and with the requirement that the retry budget must never be
hidden.

The evidence for the default is recorded in
[`PERFORMANCE-GATE-AUDIT.md`](PERFORMANCE-GATE-AUDIT.md) §16: with the
retry disabled the measured difference is 0.0 points, with it enabled it is
−33.3 points.

## What this says about the mechanism, not about the implementation

The measurement does not say that a corrective retry can never work. It says
that *this* retry, applied to *this* failure class (a model that omits
required arguments), replaces a usable answer with a worse one. A retry that
repaired the arguments in place — instead of asking for a new answer — would
not have that property, but that is exactly the "guessing" that §6 C3 §3
forbids.

The open question for the next phase is therefore not "how do we retry" but
"what *is* the right response to a tool call that cannot be validated without
guessing": a synthetic tool error from the client, a refusal, or nothing at
all. The client already returns a tool error (`error: invalid path`) and the
model repeats the broken call anyway, which is the finding that the loop
guard's escalation has to answer.
