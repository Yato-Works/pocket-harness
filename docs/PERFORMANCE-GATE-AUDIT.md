# Performance Gate & Reproducibility Audit — Pocket Harness

**Date:** 2026-10-22
**Phase:** Performance Gate & Reproducibility Audit
**Verdict in one line:** the §11.5 performance gates are **not met**; they were
measured honestly, and the measured reason is that the local baseline failures are
dominated by class **W** while the upstream is **not reproducible**, so the current
task suite cannot resolve the effect the gate asks for.

Everything below is reproducible with the commands in §2. The raw records of every
run quoted here are in [`benchmarks/`](benchmarks/).

---

## 1. Git state

| | start of phase | end of phase |
|---|---|---|
| branch | `main` | `main` |
| HEAD | *no commits yet* (`fatal: your current branch 'main' does not have any commits yet`) | *no commits yet* |
| working tree | everything untracked (`.gitignore`, `Plan.md`, `README.md`, `bench/`, `pyproject.toml`, `scripts/`, `src/`, `tests/`) | same, plus the new files listed in §9 |
| stashes | none | none |

**The repository has no commit at all.** Every change made in this phase (and
everything that existed before it) is untracked. No destructive operation was run:
no reset, no checkout of another branch, no stash, no clean. Nothing was committed,
because committing was not requested — say the word and the tree can be committed as
the first checkpoint.

---

## 2. Environment and commands

**Machine:** Windows 10 (build 26300), Intel 6th-gen+ CPU, 16 logical cores,
15.86 GiB RAM, NVIDIA RTX 3050 (8 GiB VRAM).
**Runtime:** Python 3.11.15, `pocket_harness` installed from `src/` in `.venv`.
**Upstream:** Ollama 0.40.6, `http://127.0.0.1:11434/v1`.
**Model:** `qwen2.5:7b-instruct` (gguf, qwen2 family, 7.6B, Q4_K_M, digest
`845dbda0ea48`). Model card context 32768; the **runtime** context observed with
`ollama ps` was **8192**, which is the value the benchmark configuration uses.
**Machine-local note:** Ollama ignored the `seed` field and was not deterministic
(§6).

```bash
# tests and lint (the reported "126 tests / ruff clean" state)
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src tests scripts

# the §11-shaped comparison
.venv\Scripts\python.exe -m pocket_harness.cli bench --config bench\bench-local.toml \
    --suite repo --seeds 1,2,3 --split dev     --out .e2e\dev-final
.venv\Scripts\python.exe -m pocket_harness.cli bench --config bench\bench-local.toml \
    --suite repo --seeds 1,2,3 --split held-out --out .e2e\heldout-final

# the noise floor: one task, one condition, five identical requests
.venv\Scripts\python.exe -m pocket_harness.cli bench --config bench\bench-local.toml \
    --suite repo --seeds 1,2,3,4,5 --split dev --limit 1 --condition baseline \
    --out .e2e\noise-floor

# the upstream capability profile and the reproducibility probe
.venv\Scripts\python.exe scripts\probe_upstream.py http://127.0.0.1:11434 3
.venv\Scripts\python.exe scripts\probe_reproducibility.py http://127.0.0.1:11434/v1 \
    qwen2.5:7b-instruct 4
```

The exact configuration used is committed as
[`bench/bench-local.toml`](../bench/bench-local.toml).

---

## 3. Test and lint results

| | start of phase | end of phase |
|---|---|---|
| `pytest -q` | **126 passed** (reproduced) | **182 passed** |
| `ruff check src tests scripts` | clean (reproduced) | clean |

The reported 126 tests / clean lint state was **reproduced** before anything was
changed. The 56 new tests cover the failure taxonomy, the statistics, the gate
evaluation, the repository task suite, the benchmark agent, the text-delivered tool
call detection and the replay audit.

---

## 4. Replay and determinism audit

Every item of the audit list was checked; nine of ten already held, one gap was
found and closed, and two defects were found on the way.

| Audit item | Result | Evidence |
|---|---|---|
| `local-replay` really stores the request body | **held** | `tests/test_replay_audit.py::test_local_replay_actually_stores_the_request_body` |
| the body is discoverable from a `request.captured` event | **held** | `::test_captured_body_is_discovered_from_the_event` |
| `--capture-file` wins over auto-discovery | **held** | `::test_capture_file_wins_over_auto_discovery` |
| a changed input fails `input_matched` | **held** (new test) | `::test_a_changed_input_fails_the_input_match` |
| a changed configuration fails `config_matched` | **held** | `tests/test_replay.py::test_replay_detects_a_changed_configuration` |
| `ok` is not true when either fails | **held** | the two tests above assert `ok is False` |
| proxy and replay use the same rule for `recent_tool_uses` / `recent_context` | **held** (new test) | `::test_proxy_and_replay_use_the_same_recent_tool_rule` — a conversation with tool calls replays to the identical output hash |
| a missing blob / corrupted blob / invalid event sequence is handled | **held** (new tests) | `::test_a_missing_blob_is_reported_without_claiming_success`, `::test_a_corrupted_blob_…`, `::test_an_invalid_event_sequence_is_handled` |
| the log and the replay result identify what matched and what did not | **gap found → fixed** | the report did not say *which* recorded request was compared. `replay` now reports `compared_request_index`, `compared_request_id`, `compared_request_seq` and `recorded_request_count`. `::test_replay_identifies_the_compared_request` |
| an unrecorded configuration hash is not reported as a match | **gap found → fixed** | `config_matched` was `True` when the manifest recorded no hash. It is now `null` (unverifiable) with a warning, and the field is documented in the README. `::test_unrecorded_configuration_is_reported_as_unverifiable` |

Two further defects were found while auditing the public surface:

* **`--config <missing file>` silently ran with the built-in defaults** (exit 0).
  The CLI now refuses it with exit code 2. The library-level behaviour of
  `load_config()` (defaults for a missing file) is unchanged and still tested.
* **The Windows-path hint was never shown** for the most common error
  (`"C:\Users\me\.pocket"` → *Invalid hex value*), although the README promised it.
  The hint now fires for that case as well; both the message and exit code 2 are
  asserted by a test.

**Not a defect, but worth stating:** the proxy's C4 loop guard *detects* a repeated
call but does not act on it. Plan.md C4 §2 asks the proxy to "prevent silent endless
repetition". Making it act would change the response the client receives; that is a
product decision, not a bug fix, and it was left alone in this phase.

---

## 5. The §11 conditions vs. the benchmark that existed

This is the core of the audit: **the existing benchmark could not evaluate the
performance gate**, so the results of the old benchmark must not be mixed with the
gate evaluation.

| Plan.md §11 requirement | `bench/tasks` (the old smoke set) | `bench/repos` (the new repository suite) |
|---|---|---|
| 20 repository tasks | 8 single-turn prompts | 6 repository tasks (**of the required 20**) |
| 10 development / 10 held-out, split before tuning | one undifferentiated set | 3 / 3, declared in `task.json` |
| pass/fail by test-command exit status | substring / exact / tool-name match | test-command exit status + forbidden-path check |
| fixed repository snapshot, clean worktree per run | n/a | snapshot copy per run |
| same agent in both conditions | n/a (no agent) | one scripted agent, two endpoints |
| 3 predeclared seeds | one `--seed` | `--seeds 1,2,3` (see §6: the upstream ignores the seed) |
| O/M/L/S/W/R counts | `task_not_satisfied` / `upstream_unavailable` | the full taxonomy with a documented precedence |
| 95% Wilson intervals | none | Wilson, per condition |
| paired test | none | exact McNemar (two-sided binomial on the discordant pairs) |
| steps, wall clock, TTFT, proxy p50/p95, token counts, model id | latency only | all recorded |
| excluded runs with reasons | runs were silently dropped | recorded with a reason |
| run manifest with versions and hardware | config hash + model | model, upstream, server version, package versions, hardware, seeds, task-suite hash, config hash |

**The old smoke set measures the proxy, not the product.** Its numbers were not
reused.

### The task suite

Six tasks, each a small Python project with a *failing* test, in the categories
§11.1 asks for: failing-test repair (3), configuration bug repair (1), symbol
rename (1), small feature plus tests (1), small script task (1) — with a
forbidden-path rule on every task.

`scripts/check_task_suite.py` proves each task is **broken before** and **fixed
after** a reference fix:

```
OK  dev-01-fix-add: before exit=1 passed=False | after exit=0 passed=True
OK  dev-02-config-timeout: before exit=1 passed=False | after exit=0 passed=True
OK  dev-03-rename-symbol: before exit=2 passed=False | after exit=0 passed=True
OK  heldout-01-add-palindrome: before exit=2 passed=False | after exit=0 passed=True
OK  heldout-02-fix-report-script: before exit=1 passed=False | after exit=0 passed=True
OK  heldout-03-slice-off-by-one: before exit=1 passed=False | after exit=0 passed=True
6/6 tasks are broken-before / fixed-after
```

---

## 6. The decisive measurement: the upstream is not reproducible

Plan.md §11.2 requires predeclared seeds and says: *"If a server/model ignores seed
or is nondeterministic, report that limitation instead of claiming fully seeded
reproducibility."*

**Measured, Ollama 0.40.6, `qwen2.5:7b-instruct`:**

1. The `seed` field is accepted but **ignored**: the same request with `seed=42`
   and with `seed=7` produced byte-identical answers.
2. The **same request, sent four times in the same session, produced four
   different answers** — one structured tool call and three different text
   answers (`scripts/probe_reproducibility.py`).
3. The noise floor of the benchmark: **one task, one condition, five identical
   requests → 0/5 passed**, with the outcome varying between W, M and R
   ([`benchmarks/noise-floor-identical-requests.json`](benchmarks/noise-floor-identical-requests.json)).

Consequence: the three "seeds" are three samples of a noisy process. Any
difference the benchmark measures is dominated by that noise, and the paired test's
independence assumption does not hold. **This is reported, not worked around.**

---

## 7. Gate verdicts

### Pre-lock full matrix (6 tasks × 3 seeds × 2 conditions = 36 runs)

Raw: [`benchmarks/pre-lock-full-matrix.json`](benchmarks/pre-lock-full-matrix.json)

| condition | passed | completion rate | 95% Wilson | failure classes |
|---|---|---|---|---|
| baseline | 6/17 | 0.353 | [0.173, 0.587] | M 1, S 1, **W 9** |
| harness | 4/15 | 0.267 | [0.109, 0.519] | L 3, M 2, S 1, **W 5** |

Paired difference **−7.1 points**, exact McNemar **p = 1.0** (b=3, c=1).

### Post-lock, held-out split only (the single held-out execution)

Raw: [`benchmarks/heldout-post-lock.json`](benchmarks/heldout-post-lock.json)

| condition | passed | completion rate | 95% Wilson | failure classes |
|---|---|---|---|---|
| baseline | 3/7 | 0.429 | [0.158, 0.750] | M 2, **W 2** |
| harness | 1/5 | 0.200 | [0.036, 0.625] | M 2, **W 2** |

Paired difference **0.0 points** over 5 usable pairs, exact McNemar **p = 1.0**.
8 of the 18 runs were **ineligible** (upstream delivered an empty completion or
errored) and are reported separately, never silently dropped.

### The gates

| Gate | Criterion | Verdict | Why |
|---|---|---|---|
| **G0 — problem fit** | more than half of the eligible baseline failures are attributable to O/M/L/S rather than W; R excluded | **fail** | 2 of 11 eligible baseline failures (18%) in the full matrix, 2 of 4 (50%) in the held-out run. The majority is **W**. |
| **G1 — demonstrated value** | held-out completion improves by ≥ +30 points; < +10 points ⇒ stop | **stop** | the held-out point estimate is −22.9 points (post-lock) / +5.4 points (pre-lock). Below +10 either way: *"stop feature expansion and re-evaluate the premise."* |
| **Public release gate** | ≥ 40 tasks, a separate held-out slice, published manifests/outcomes/failures | **fail** | 6 tasks (3 held-out). |
| **§2 proxy overhead** | p95 ≤ 50 ms of local transformation work | **met** | p50 0.88 ms, p95 4.00 ms over 22 transformations (p50 1.03 ms / p95 4.57 ms over 112 in the full matrix). |
| **§2 reproducibility** | the same captured input with the same version/config yields the same transformed-request hash | **met for the transformation** | `pocket replay` reproduces the recorded output hash; the *upstream* is not reproducible (§6). |
| **§2 broken tool-call rate** | at most half of the baseline | **not evaluable** | there were too few invalid tool calls in the harness runs (3 in total) to compare a rate. |

**No completion-rate improvement is claimed.**

---

## 8. Failure classification and the dominant cause

### By class (the plan's taxonomy)

| class | meaning | count (full matrix, baseline) |
|---|---|---|
| **O** | context overflow / request too large | 0 |
| **M** | malformed or schema-invalid tool call | 1 |
| **L** | loop / unproductive repetition | 0 |
| **S** | stopped before completion (budget exhausted) | 1 |
| **W** | wrong semantic edit / reasoning failure | 9 |
| **R** | upstream rejection / rate limit / provider outage | 1 (excluded from the denominator) |

(The full matrix contains 12 baseline failures; one of them is class R and is
therefore excluded from the efficacy denominator, leaving the 11 used by G0.)

### By mechanism (the sub-cause behind the class)

Full matrix, re-classified with the corrected rules
([`benchmarks/pre-lock-full-matrix.json`](benchmarks/pre-lock-full-matrix.json));
only the *eligible* failures are listed, so the counts sum to the G0 denominator.

| mechanism | baseline | harness |
|---|---|---|
| stopped without acting (the model explained, then stopped) | 5 | 5 |
| the upstream delivered the tool call as **text** | 2 | 1 |
| the model edited a forbidden path (the tests) | 1 | 2 |
| the model changed the code but the test still failed | 3 | 1 |
| the model broke the project syntax | 0 | 2 |
| **eligible failures** | **11** | **11** |

A further 1 baseline and 3 harness runs were upstream faults (class **R**) and are
excluded from the table and from the G0 denominator.

**The dominant cause is not the proxy's domain.** The two largest mechanisms are
"the model stopped without acting" and "the upstream delivered the tool call as
text instead of structurally". In the second case the client has nothing to execute
and the task makes no progress. The rest is model quality (class W): the model
explains what it would do and stops, or it edits the test file to make the import
work.

### The interventions were never exercised

| intervention | events in the full matrix (harness runs) |
|---|---|
| C1 tool selection | **0** (the agent sends three tools, the cap is six) |
| C2 context compression | 2 events, 57 tokens saved, in 1 of 15 runs |
| C3 tool repair | **0** repairs (3 invalid calls, none repairable) |
| C4 loop detection | 14 events in 3 runs — **detected but not acted upon** |

A comparison in which the interventions essentially never fire cannot show an
effect. The measured difference between the conditions is therefore not evidence
*for* or *against* the proxy — it is evidence that **this** task suite and **this**
configuration do not exercise the proxy's mechanisms at all.

---

## 9. What was changed, and why

| File | Change | Reason |
|---|---|---|
| `src/pocket_harness/taxonomy.py` | **new** — the O/M/L/S/W/R taxonomy and the classification of a run from the trace | §11.4 requires the class counts; there was no way to derive them |
| `src/pocket_harness/stats.py` | **new** — Wilson interval, exact McNemar, paired difference, latency summary | §11.4 requires the intervals and the paired test |
| `src/pocket_harness/evaluation.py` | **new** — the G0/G1/public-release verdicts, computed from the raw records | the gates had no implementation at all |
| `src/pocket_harness/agent.py` | **new** — the scripted coding agent (identical in both conditions) | §11.2 compares *the same agent* with and without the proxy |
| `src/pocket_harness/repo_bench.py` | **new** — the repository benchmark: clean worktree per run, test-command exit status, forbidden paths, seeds, splits, manifest, raw records, excluded runs | the old bench could not express any §11 condition |
| `src/pocket_harness/toolcall_text.py` | **new** — deterministic detection of a tool call the upstream delivered as text | the dominant failure mechanism was invisible in the evidence; it is now classified M instead of W |
| `src/pocket_harness/proxy.py` | emits `transform_ms`; records `delivered_as_text` in `toolcall.validated` | §11.4 requires the proxy-only overhead; §11.4 requires the invalid-tool-call counts, and the delivery fault must be visible in the evidence |
| `src/pocket_harness/taxonomy.py` | a run that **passed** has no failure class; an empty completion is class **R** | a passed run was counted as a failure; an upstream fault was counted as a model fault — both distort G0 |
| `src/pocket_harness/evaluation.py` | **G0 excluded class R from the denominator by class, not only by the eligibility flag** | a regression test caught that an R-classed run could still enter the denominator |
| `src/pocket_harness/config.py` | the Windows-path hint now fires for *Invalid hex value* | the README promised a hint that the code did not print |
| `src/pocket_harness/cli.py` | an explicitly named `--config` file must exist (exit 2); `bench --suite repo --seeds --split --require-gates --keep-worktrees` | a missing file silently ran with the defaults; the repo suite needs its own options |
| `src/pocket_harness/replay.py` | the report names the compared request; `config_matched` is `null` when it cannot be verified | diagnosability and honesty of the reproduction claim |
| `bench/repos/**` | **new** — the six repository tasks with their snapshots | the §11.1 task shape |
| `bench/bench-local.toml` | **new** — the exact configuration of the measured runs | §11.2 requires the configuration to be published |
| `scripts/*` | **new** — `check_task_suite.py`, `show_records.py`, `analyse_failures.py`, `analyse_subcauses.py`, `analyse_text_toolcalls.py`, `reclassify_records.py`, `summarise_run.py`, `probe_forwarded_request.py`, `probe_conversation_fidelity.py`, `probe_reproducibility.py`, `probe_upstream_empty.py` | the analysis and the reproduction of the numbers above |
| `tests/test_evaluation.py`, `tests/test_replay_audit.py`, `tests/test_toolcall_text.py` | **new** — 56 tests | regression cover for everything above |
| `src/pocket_harness/agent.py` | the first user message now contains the workspace file list | the agent must look like the real client the plan targets; see §10 |

---

## 10. The one improvement that was tried, and its honest result

**Change.** The benchmark agent now shows the model the workspace file list, the way
a real coding agent (Cline, OpenCode) does. Without it the model invents paths
(`src/calc.py`, `code_snippet.py`) or stops after reading. The change applies
identically to both conditions, so the comparison stays valid.

**Before/after on the tuning (dev) split**, same configuration otherwise
(before = the pre-lock matrix, dev subset; after = the locked configuration):

| | before | after |
|---|---|---|
| baseline — eligible runs | 8 | 8 |
| baseline — passed | 3 | 2 |
| harness — eligible runs | 8 | 5 |
| harness — passed | 1 | 1 |
| runs excluded as upstream faults | 1 | 5 |
| stopped without acting | 5 | 2 |
| tool call delivered as text | 1 | 4 |
| edited a forbidden path | 3 | 3 |
| broke the project syntax | 2 | 2 |
| changed the code, test still failed | 2 | 0 |

**Result: the mechanism improved, the completion rate did not.** The targeted
sub-cause ("stopped without acting") fell from 5 to 2, but the upstream delivery
fault ("tool call delivered as text") rose from 1 to 4 and the number of runs lost
to upstream faults rose from 1 to 5. The completion numbers moved within the noise
established in §6. With n=9 pairs and a non-reproducible upstream, **no improvement
can be confirmed** — and none is claimed.

**A second, larger change was made to the measurement, not to the product:** the
failure classification now distinguishes an upstream delivery fault from a model
failure. Re-applying it to the *same* pre-lock runs moved 3 baseline failures out of
the W bucket (2 into R, 1 into M) and changed G0 from "2 of 12" to "2 of 11" — the
verdict did not change, but the denominator became correct.

**A product change that was deliberately *not* made:** turning a text-delivered tool
call into an executable one. It would address the largest failure mechanism, but it
means guessing the model's intent, which Plan.md §1.3 forbids ("do not guess semantic
intent"), and it would put tool dispatch into the proxy, which the design assigns to
the client. It needs a design decision, not a bug fix.

---

## 11. Remaining bugs and unverified items

**Bugs / gaps found, not fixed (they need a decision, not a patch):**

1. **C4 detects but does not act.** A repeated call is detected (14 events in the
   full matrix) and the run continues until the step budget runs out. Plan.md C4 §2
   asks the proxy to prevent silent endless repetition.
2. **C3 repairs are not applied to the client's response.** By design (the client
   owns dispatch) — but it means C3 cannot change a task outcome, only the evidence.
3. **The upstream's tool-call delivery is unreliable** and the proxy cannot repair a
   call that was never delivered structurally.
4. **`capture = "local-replay"` stores the request body**, which can contain source
   code and secrets. Documented, opt-in, and covered by a test — but it is the
   sharpest edge in the privacy model.

**Unverified:**

* Everything in §11.1 at full scale: 20 tasks, 10/10 split, 120 runs. The suite
  here has 6 tasks / 3 held-out.
* The public release gate (40 tasks).
* Any claim about a real coding agent (Cline, OpenCode): the benchmark uses a
  scripted agent, because no client is installed in this environment. The agent is
  identical in both conditions, but it is not a client.
* LM Studio and llama.cpp server: only Ollama was measured.
* Claude Code / Anthropic Messages API: deferred by the design.
* Seeded reproducibility: impossible on this upstream (§6).

---

## 12. The next minimal step

**Do not add features.** The evidence says the premise is unproven, not that the
implementation is incomplete. The next smallest step that would change the
decision is:

> **Make the interventions reachable in the measurement, and measure with a
> reproducible configuration.** Extend the task suite with tasks that create the
> conditions C1 and C3 target (a tool set larger than the cap, a model that
> emits broken tool calls) and with tasks that produce the O/M/L/S failure
> modes, then re-run the dev split and check the *intervention coverage*
> counters before looking at completion rates again.

Phase two did the second half of that (a reproducible configuration, C2 and C4
firing) and the answer was still 0.0 points. If, with C1 and C3 also firing,
the baseline failures are still dominated by W, then Plan.md §3's escape clause
applies in full: revisit the product premise rather than deepening the proxy.

Two smaller items belong next to it:

* **Decide C4's abort path.** The escalation is now a warning; whether the
  proxy may also refuse a request (and break the client's API contract) is
  still open and is a design decision, not a bug fix.
* **Fix the upstream's empty completions or exclude them by construction.**
  Half of the runs are lost to class R. A model that answers reliably, or a
  task set that does not trigger it, is a precondition for measuring anything
  at this sample size.

---

## 14. Phase two — making the comparison valid, and making the mechanisms act

*(This section was added by the second phase; the sections above are the first
phase and are left as they were measured.)*

The first phase measured the gate and found it unmet, but the comparison was
not yet sound: the upstream answered the same request differently, so the
measured difference could not be separated from the upstream's noise. This phase
did three things, in the order the review asked for.

### 14.1 The source and the evidence are preserved

The whole tree was untracked and the repository had **no commit at all**. The
files that were about to be committed were checked (no credential-shaped
content outside the redaction test fixtures, `scripts/check_for_secrets.py`),
and the tree was committed as the first checkpoint:

```
91d725d  Checkpoint: v0.1 implementation plus the performance-gate audit
```

The commit message states the measured verdict, so the history itself cannot
be read as a success claim.

### 14.2 The upstream's reproducibility was measured properly

The first phase concluded "the upstream is not reproducible". That was true
but incomplete: the *sampling* was the cause, not the server.

| probe | result |
|---|---|
| `probe_reproducibility.py` — the same multi-turn request four times | 4 different answers |
| `probe_determinism.py` — 10 models × 3 settings × 5 rounds | default: 0/10 reproducible; `temperature=0`: 8/10; **`temperature=0` + `seed=42`: 10/10** |
| `probe_seed_effect.py` — seeds 1/2/3 at `temperature=0` | stable per seed, but the three seeds give the *same* answer: the seed is accepted and ignored |
| `probe_cache_state.py` — the same request, with other traffic interleaved | identical answer: the server-side prefix cache is **not** the cause |

So a reproducible upstream configuration exists: greedy decoding plus a fixed
seed. The benchmark now pins it (`--temperature 0`, `--seeds 1,2,3`) and records
the temperature in the run manifest.

### 14.3 The benchmark had a reproducibility leak of its own

With the sampling pinned, the matrix still changed between two runs (12 of 36
cells). The cause was in the harness, not the upstream: the pytest output
contains a duration (`1 failed, 1 passed in 0.41s`), and that duration entered
the model's context. A different duration is a different prompt, and a
different prompt is a different answer.

The agent now sanitises the volatile parts of a tool result (durations,
timestamps, the absolute workspace path) before they reach the model. The
result: **three independent runs of the whole matrix agree in 36 of 36 cells**
(`scripts/compare_runs.py`).

### 14.4 The mechanisms now fire where they are needed

`tests/test_mechanism_firing.py` (18 tests) builds the condition each mechanism
targets and checks that it fires, with the right result and the right
invariants. Two of them found real defects:

* **C2 silently deleted the system prompt.** When the budgeter had to omit old
  exchanges, the slice it removed started at message 0, so the leading system
  message went with it. Plan.md §6 C2 forbids exactly this ("do not silently
  delete the system prompt"). Fixed, with a regression test
  (`test_the_system_prompt_survives_the_omission_of_old_exchanges`).
* **C3 counted cosmetic re-serialisations as repairs.** The model emitted
  `{"path":"calc.py"}`; C3 re-serialised it as `{"path": "calc.py"}` and
  reported a repair. A benchmark that counts such re-serialisations inflates
  the intervention coverage with work that changed nothing. A repair is now only
  reported and applied when the original arguments were **not usable as they
  stood**.

The two design decisions that the mechanisms needed are recorded:

* [ADR 0001 — where a repaired tool call is applied](decisions/0001-c3-application-point.md):
  a repaired call is what the client receives (Plan.md §4.3 "emit the
  reconstructed tool call"), provided the original was not usable.
* [ADR 0002 — what C4 does when it detects a loop](decisions/0002-c4-escalation.md):
  the detection is derived from the request history, so the escalation is part
  of the deterministic transformation and a replay reproduces it.

### 14.5 The gate, measured again under valid conditions

Raw: [`benchmarks/pinned-reproducible-matrix.json`](benchmarks/pinned-reproducible-matrix.json)
(36 runs, 6 tasks × 3 seeds × 2 conditions, `temperature=0`, reproducible)

| condition | passed | completion rate | 95% Wilson | failure classes |
|---|---|---|---|---|
| baseline | 3/9 | 0.333 | [0.121, 0.646] | M 2, **W 4** |
| harness | 3/9 | 0.333 | [0.121, 0.646] | M 3, **W 3** |

Paired difference **0.0 points**, exact McNemar **p = 1.0**. 18 of the 36 runs
were lost to upstream empty completions (class R) and are reported separately.

**Intervention coverage in this run** — the mechanisms were *not* idle this time:

| mechanism | events |
|---|---|
| C1 tool selection | 0 (the agent sends three tools, the cap is six — the condition does not arise) |
| C2 context compression | **12 events in 3 of 9 runs, 11 211 tokens saved** |
| C3 tool repair | 0 repairs (9 invalid calls, none repairable) |
| C4 loop detection / escalation | **3 detections, 3 escalations** (the warning reached the model) |

| Gate | Verdict | Why |
|---|---|---|
| **G0 — problem fit** | **fail** | 2 of 6 eligible baseline failures (33 %) are attributable to O/M/L/S; the majority is **W** |
| **G1 — demonstrated value** | **stop** | the held-out difference is **+0.0 points**; below +10 the premise is to be re-evaluated |
| **Public release gate** | **fail** | 6 tasks; the gate needs 40 |
| **§2 proxy overhead** | **met** | p50 1.18 ms, p95 5.60 ms |

**The important change is not the numbers, it is their quality.** Before this
phase the comparison could not separate the proxy from the upstream's noise.
Now it can: the runs are reproducible, the interventions fire, and the answer
is still **0.0 points**. The interventions changed the request (11 211 tokens
of context removed, three loop escalations delivered) and the completion rate
did not move.

That is a much stronger statement than "the gate is unmet": on the failure modes
this suite contains, the proxy's mechanisms are not the bottleneck. The
bottleneck is the model's own behaviour (class W) and the upstream's own
failure to answer (class R).

### 14.6 What the two phases together say

* The proxy works, is fast, is deterministic and is reproducible.
* Its mechanisms are implemented, fire where the conditions arise and keep
  their invariants.
* On the six tasks measured, they do not change the completion rate.
* The dominant failure classes are W (model behaviour) and R (upstream empty
  completions) — not the classes the proxy addresses.

The honest conclusion is therefore the one Plan.md §3 prepares for: the
premise of the first experiment is not supported by its evidence, and the next
step is to redefine the hypothesis and the target, not to add features.

---

## 15. Reproducing this document

```bash
git clone <this repository> && cd Pocket-Harness
uv venv && uv pip install -e ".[dev]"
# the numbers above
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src tests scripts
.venv\Scripts\python.exe scripts\check_task_suite.py

# the reproducible, pinned matrix (phase two)
.venv\Scripts\python.exe -m pocket_harness.cli bench --config bench\bench-local.toml \
    --suite repo --seeds 1,2,3 --temperature 0 --out ./bench-results
.venv\Scripts\python.exe scripts\summarise_run.py ./bench-results\bench-repo-raw.json

# the upstream probes
.venv\Scripts\python.exe scripts\probe_determinism.py http://127.0.0.1:11434/v1 5
.venv\Scripts\python.exe scripts\probe_seed_effect.py http://127.0.0.1:11434/v1 3
.venv\Scripts\python.exe scripts\probe_cache_state.py http://127.0.0.1:11434/v1 qwen2.5:7b-instruct 4
```

The recorded raw records in [`benchmarks/`](benchmarks/) are the ones the tables
above were computed from; `scripts/reclassify_records.py` reproduces the
re-classification of the pre-lock matrix without spending new model calls, and
`scripts/compare_runs.py` reproduces the reproducibility check.
