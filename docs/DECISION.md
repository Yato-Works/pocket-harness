# Decision — Pocket Harness after the 42-task evaluation

**Date:** 2026-10-22
**Question:** does Pocket Harness improve the task completion rate of a coding
agent that uses a constrained local model?
**Answer, on the evidence that now exists:** **no improvement is demonstrated.**
The premise that the local failures are proxy-addressable *is* confirmed; the
intervention does not turn them into completions.

This document states what was measured, what the gates say, which of the three
possible outcomes this is, and what the project does next.

---

## 1. What was measured

Two suites, one manifest, predeclared conditions, one shot on the held-out
split.

| suite | tasks | runs | role |
|---|---|---|---|
| development | 27 | 27 × 3 seeds × 2 conditions = 162 | tuning, diagnosis, failure analysis |
| held-out | 15 | 15 × 3 seeds × 2 conditions = 90 | the one-shot evaluation |
| mechanism diagnostics | 6 deterministic scenarios | — | do the mechanisms work at all |

42 repository tasks in total (the plan's public release gate asks for 40). Every
task is proven **broken before** and **fixed after** a reference edit
(`scripts/check_task_suite.py`, 42/42). The categories follow §11.1: 29
failing-test repair, 3 configuration bug repair, 3 symbol rename, 3 small script
task, 4 small feature plus tests.

The conditions were declared in
[`benchmarks/heldout-2026-10-22-predeclared.json`](benchmarks/heldout-2026-10-22-predeclared.json)
before the held-out run: the task list, `temperature = 0`, the three seeds, the
retry budgets, the abort flag, the **system prompt hash** and the **tool error
contract version**. The tasks whose results had been inspected earlier were
moved to the development split before the evaluation.

## 2. The two tracks

The evaluation reports two tracks and never substitutes one for the other.

### Track A — mechanism diagnostics (does the machinery work?)

| mechanism | obligation | held-out result | development result |
|---|---|---|---|
| C1 | at most cap + pinned tools are forwarded | condition did not arise (the client sends three tools, the cap is six) | same |
| C2 | the request is compressed, the system prompt survives, the pairs stay valid | 14 compression events, 26 392 tokens saved | 15 events, 51 tokens saved |
| C3 | the call is detected, repaired when that is possible, otherwise passed through unchanged | 213 calls validated, 33 invalid detected, **0 repaired** | 369 validated, 53 invalid, 0 repaired |
| C4 | the repetition is detected and the escalation reaches the model | 10 detections, 10 escalations | 40 detections, 28 escalations |

The deterministic scenario suite (`pocket-harness diagnose`) checks the same
obligations against conditions that are created on purpose: **6 of 6 pass**, every
time, with no model and no network involved.

**Track A says:** the machinery does what it is supposed to do when its
condition arises — with one exception that is a *design* limit, not a bug: C3
repairs nothing, because the invalid calls the model produces are missing
required arguments, and Plan.md §6 C3 §3 forbids inventing them.

### Track B — task completion (do tasks get finished?)

Held-out split, 15 tasks × 3 seeds × 2 conditions, 90 runs, 75 eligible:

| condition | passed | rate | 95% Wilson | classes | excluded |
|---|---|---|---|---|---|
| baseline | 18/42 | 42.9 % | [29.1, 57.8] | M 14, W 9, S 1 | 3 |
| harness | 10/33 | 30.3 % | [17.4, 47.3] | M 15, W 6, L 2 | 12 |

Paired outcomes: **9 both passed, 18 both failed, 3 baseline only, 0 harness
only**, 15 pairs ineligible. Paired difference **−12.6 points**, exact McNemar
**p = 0.25**.

Development split, 27 tasks × 3 seeds × 2 conditions, 162 runs:

| condition | passed | rate | classes | excluded |
|---|---|---|---|---|
| baseline | 27/66 | 40.9 % | M 22, W 17 | 15 |
| harness | 25/69 | 36.2 % | M 18, W 19, L 7 | 12 |

Paired outcomes: 20 both passed, 34 both failed, **4 harness only, 3 baseline
only**, 20 ineligible. Paired difference **−4.7 points**, p = 1.0.

Proxy overhead (the §2 target is p95 ≤ 50 ms): **p50 0.99 ms, p95 7.99 ms**.

## 3. The gate verdicts

| gate | verdict | why |
|---|---|---|
| **G0 — problem fit** | **pass** | 15 of 24 eligible baseline failures (62 %) are attributable to O/M/L/S. The local failures really are malformed tool calls and repetitions — the classes the proxy addresses. On the development split it also passes (22 of 39, 56 %). |
| **G1 — demonstrated value** | **stop** | the held-out completion difference is **−12.6 points**. Below +10 the plan requires a stop and a re-evaluation of the premise. |
| **Public release gate** | **pass** | 42 tasks (≥ 40) with a separate held-out slice of 15, and the configurations, manifests, raw outcomes, failure examples and limitations are published in this repository. |
| §2 proxy overhead | **met** | p50 0.99 ms, p95 7.99 ms. |

G0 and G1 are not in conflict. G0 asks whether the *kinds* of failure the
proxy addresses actually occur — they do. G1 asks whether addressing them
*completes tasks* — it does not.

## 4. Which outcome this is

The three possibilities were: improvement confirmed / only local improvements
reproducible / no reproducible improvement at all.

**This is the second one: the local improvements reproduce, the completion rate
does not move.**

* Reproducible: the mechanisms fire (Track A), the invalid calls are detected
  and reported, the repetitions are detected and escalated, the context is
  compressed, the evidence log is complete, and the whole matrix is
  reproducible (36/36 cells identical across independent runs).
* Not reproducible: any completion-rate effect. The harness won 4 pairs and
  lost 3 on the development split, and won 0 and lost 3 on the held-out
  split. Neither is an effect; both are inside the measured noise.

### Why the intervention does not convert failures into completions

The dominant failure is class **M**: the model emits `write_file` with *no
arguments at all*. The chain is:

1. C3 detects the call and refuses to invent the missing values (§6 C3 §3).
2. The client answers with a precise, schema-derived error (ADR 0005) — it
   names the missing argument and its type.
3. The model usually repeats a broken call or stops.

So the failure is *not* a context problem, not a repetition the guard can
break, and not a call that can be repaired without guessing. It is the model
failing to produce a well-formed call in the first place, and the deterministic
tools available to the proxy cannot change that. Worse, the interventions
*perturb* the conversation: the upstream reacts to a changed request, and the
reaction is not systematically positive — on the held-out split the harness
condition lost 3 pairs and won none.

### The measurement has one known defect

The upstream is not perfectly stable across runs: in the order experiment
(`--reverse-order`, 4 tasks × 1 seed, each condition run in both orders) one
baseline run flipped its outcome between the two orders, and the *first* run of
a pair did better. The isolated probes (`probe_cache_order.py`,
`probe_byte_sensitivity.py`) show that neither the prompt cache nor the byte
encoding explains it — the upstream is simply sensitive in a way that a 3-task
suite cannot control. This is the reason the plan asks for 40+ tasks: the
measured noise band of a 3-task suite (±28 points) is larger than the +30
point threshold it has to resolve.

## 5. The decision

**Pocket Harness's value hypothesis is redefined.** The original hypothesis —
"the proxy raises the completion rate of a coding agent that uses a constrained
local model" — is **not supported** by the evidence and is withdrawn as the
project's claim. It is not disproved either: with 15 held-out tasks and a
noisy upstream, an effect of the size the gate asks for could not be detected.

The hypothesis that the evidence *does* support is narrower and, on this
evidence, better founded:

> **A coding agent that uses a constrained local model fails in a small number
> of identifiable ways — malformed tool calls, repeated calls, oversized
> context — and a deterministic proxy can make every one of them *observable*,
> *reproducible* and *auditable*, without guessing the model's intent and
> without changing the agent's API contract.**

That hypothesis has its own success criteria, which must be stated and
measured separately from the completion rate:

1. **Detectability.** Every malformed call and every repetition is detected
   and recorded, with a machine-readable class. (Measured: 33 invalid calls and
   10 repetitions detected on the held-out split; 6 of 6 diagnostic scenarios
   pass.)
2. **Determinism.** The same input, configuration and version produce the same
   transformed request and the same evidence log. (Measured: identical
   transformed-request hashes on replay; 36/36 identical cells across
   independent matrix runs.)
3. **Auditability.** A third party can reconstruct, from the evidence alone,
   what the proxy changed and why. (Measured: every request has a manifest, a
   rule list, hashes and a class.)
4. **Non-interference.** The proxy does not change the client's API contract
   and does not invent values. (Measured: no request is rewritten in a way that
   changes its meaning; no argument is ever invented; the C4 refusal is opt-in
   and typed.)

Note what is *not* in that list: a completion-rate claim. That would be the
third branch of the decision, and the evidence does not support it.

## 6. What is not claimed

* No completion-rate improvement is claimed. The measured held-out difference
  is **−12.6 points** and the paired test is not significant.
* The G1 "stop" verdict stands; it is not re-read as "diagnose" because the
  number happens to be near zero on the development split.
* p = 0.25 with three discordant pairs does not prove the absence of an
  effect. It says that this suite cannot resolve one.
* The mechanism diagnostics do not stand in for a completion-rate result. They
  are a different question with a different answer.

## 7. The next minimal step

Not another mechanism, and not a bigger suite yet.

1. **Fix the measurement defect first.** The run order changes outcomes; a
   comparison that inherits the upstream's state from the previous condition is
   not a controlled experiment. Either the two conditions run against
   independent upstream instances, or the order is randomised and the order
   effect is reported. Until that is fixed, no further completion-rate number
   from this upstream is worth reading.
2. **Then measure the new hypothesis.** Its criteria are the four above, and
   they can be measured on the existing 42-task suite without new model calls:
   detection rate, determinism, auditability and non-interference. That is a
   *different* evaluation from the completion rate and it needs a different
   report.
3. **Only then revisit the completion-rate question**, with an upstream whose
   variance is low enough to resolve a 30 point effect, or with a suite large
   enough to resolve it (the plan's public release gate at 40 tasks is a
   floor, not a guarantee of statistical power).

## 8. Evidence index

| artefact | what it holds |
|---|---|
| [`benchmarks/heldout-2026-10-22-predeclared.json`](benchmarks/heldout-2026-10-22-predeclared.json) | the conditions, declared before the held-out run |
| [`benchmarks/heldout-2026-10-22.json`](benchmarks/heldout-2026-10-22.json) | the 90 held-out runs |
| [`benchmarks/development-2026-10-22.json`](benchmarks/development-2026-10-22.json) | the 162 development runs |
| [`benchmarks/order-experiment.json`](benchmarks/order-experiment.json) | the run-order experiment |
| `pocket-harness diagnose` | the six deterministic mechanism scenarios |
| [`PERFORMANCE-GATE-AUDIT.md`](PERFORMANCE-GATE-AUDIT.md) | the phases before this one, with their measurements |
| [`decisions/`](decisions/) | ADR 0001–0005: the design decisions taken along the way |
| [`benchmarks/criteria-measure-2026-10-23.json`](benchmarks/criteria-measure-2026-10-23.json) | the §7.2 four-criteria measurement (Detectability, Determinism, Auditability, Non-interference), no model calls `scripts/measure_criteria.py` |
| [`benchmarks/persisted-replay-e2e-2026-10-23.json`](benchmarks/persisted-replay-e2e-2026-10-23.json) | the persisted-replay verification across a process restart, in an isolated state root (`scripts/verify_persisted_replay.py`) |
| [`benchmarks/fresh-install-check-2026-10-23.json`](benchmarks/fresh-install-check-2026-10-23.json) | the README flow reproduced from a fresh venv (install, init, doctor, serve chain, replay, report) |

### 8.2 The loop attribution split (2026-10-23)

Detection and actual mitigation are now separate indicators
(``taxonomy.classify_loop_attribution``, ``scripts/measure_criteria.py``).
The 24 detected loops of the 42-task artifacts attribute to
**15 ``proxy_escalated``** (the warning reached a later request) and
**9 ``client_guard_handled``** (the record carries the client's own
identical-failure stop evidence), with **0 ``detected_only``**. The
``L-escalation`` entry (15/24) above is kept unchanged for the before/after
comparison; a client-guard stop is data, not a proxy success.

Two repairs came with this: ``pocket replay`` now reproduces the C2 stub
``content_ref`` of the recorded requests (its pipeline lacked the recorded
blob store, so every dedupe-bearing request would have replayed to a
mismatch); and loop-attribution is replayable end to end
(``tests/test_loop_attribution.py``).

The persisted-replay path was then verified against the real processes: a
proxy recorded a run in an isolated state root under its own ``serve``
process, and fresh ``replay`` processes reproduced input hash, config hash,
output hash, and rule order across the restart, while a missing/corrupted
capture blob, an altered configuration, and a tampered body each stayed a
failure
(``scripts/verify_persisted_replay.py``,
``docs/benchmarks/persisted-replay-e2e-2026-10-23.json``).

### 8.3 What the four-criteria measurement verified — and what it did not

Measured on 2026-10-23 (`scripts/measure_criteria.py`, definitions fixed before
execution): M-detection 43/43, L-detection 24/24, mechanism scenarios 6/6;
outcome-level determinism across same-signature artifacts 18/18 cells; failure
attribution 142/142; no invented arguments, no invisible model calls, no silent
aborts across all 144 harness runs.

**Recorded as unverified, not assumed:**

* The **transformed-request hash replay** (Plan.md §2) could not be re-measured
  here: capture mode was `metadata` and the blob/run stores are empty, so no
  prompt bodies exist to replay. The unit-level replay suite still passes, but
  the end-to-end replay claim rests on the fixtures of the earlier phases.
* The audit's claim that **three independent matrix runs agreed in 36 of 36
  cells** rests on only one of the three runs being archived; the other two were
  not preserved and cannot be re-verified from what exists.
* The independent-instance rerun below is a **four-task, one-seed observation**:
  no pass-outcome flip was observed between runs in *that* configuration. It
  neither claims nor denies a run-order effect in general; the completion-rate
  question remains without a claim, as §6 states.

## 9. The assurance matrix (2026-10-23)

One table, three explicit sections. Every row carries its evidence; nothing
in the middle or bottom section may be read as satisfied.

### Guaranteed on the evidence that exists

| claim | scope | evidence |
|---|---|---|
| The pipeline is a deterministic function of `(input, configuration, pipeline version)` | every transformation: no network, no LLM, no wall-clock decision | unit suite 238 tests; 6/6 mechanism scenarios (`pocket-harness diagnose`) |
| The persisted replay reproduces a recorded run across a process restart | input hash, config hash, output hash and rule order all equal, after the proxy process exited | [`persisted-replay-e2e-2026-10-23.json`](benchmarks/persisted-replay-e2e-2026-10-23.json) |
| Replay never fakes a success | a missing, corrupted or tampered capture body and a changed configuration each end `ok: false` | same artifact, four deliberate-failure scenarios |
| The C2 reference stubs are content-addressed, not copies | a deleted content blob is re-derived idempotently and the output hash still matches | same artifact, positive control |
| Failures are detected with a machine-readable class | M-detection 43/43, L-detection 24/24 of the stored records | [`criteria-measure-2026-10-23.json`](benchmarks/criteria-measure-2026-10-23.json) |
| Each detected loop carries exactly one attribution label | 15 `proxy_escalated`, 9 `client_guard_handled`, 0 `detected_only`; labels are mutually exclusive with explicit evidence gates | same artifact; `tests/test_loop_attribution.py` |
| The evidence log reconstructs what happened, rule by rule | manifest with config hash, per-request hashes and rule lists | §8 index; 142/142 attribution, 144/144 proxy traces |
| A third party can follow README.md from a fresh install to doctor, the serve chain, replay and report | fresh uv venv, README command, fresh CLI for the whole chain; the default model's honest `ok: false` is itself part of the check | [`fresh-install-check-2026-10-23.json`](benchmarks/fresh-install-check-2026-10-23.json) |
| Arguments are never invented | 0 repairs of invalid calls across all 67 invalid calls (design: §6 C3 §3) | criteria measurement, non-interference section |

### Explicitly NOT provided

| claim | why |
|---|---|
| The model writes correct code, or tasks get completed more often | measured held-out difference **−12.6 points** (p = 0.25) — no effect is claimed, ever, without new evidence |
| The proxy fixes a model that will not emit a well-formed call | the dominant failure class M is unrepairable without guessing (§4) |
| Upstream reproducibility in general | Ollama 0.40.6 is only reproducible pinned (`temperature = 0` + seed); the order experiment showed a residual sensitivity the suite cannot control |

### Still unverified — stays open by decision §8.3

| item | status |
|---|---|
| The two unpreserved matrix runs behind the "36/36 cells identical" claim | re-verification impossible; the claim rests on the one archived run |
| A hash-replay of the historical 42-task captures | `capture = metadata` stored no bodies; only the fresh-isolated-run replay above is verified end to end |
| Non-interference under conditions not yet evaluated | the four-criteria measurement covers the stored 42-task artifacts only |
| A general run-order effect | only a four-task, one-seed observation with independent instances exists (§8.3); no general claim either way |
