# Pocket Harness

Local-first, deterministic **request/response proxy and execution-trace system** that sits
between an existing coding agent (Cline, OpenCode, …) and a local inference endpoint.

Pocket Harness does not train models and does not use a separate LLM to summarize context,
judge success, or choose interventions. Its core logic is inspectable transformation code,
schema validation, trace-derived state, and bounded recovery behavior.

See [`Plan.md`](./Plan.md) for the full design specification (v0.2-design).

## What it guarantees — and what it does not

| | claim | evidence |
|---|---|---|
| ✅ | The pipeline is a deterministic function of `(input, configuration, version)` — no network, no LLM, no wall clock in a transformed request | 238 tests, 6/6 mechanism scenarios |
| ✅ | A recorded run replays identically across a process restart: input hash, config hash, output hash and rule order all match | [`docs/benchmarks/persisted-replay-e2e-2026-10-23.json`](docs/benchmarks/persisted-replay-e2e-2026-10-23.json) |
| ✅ | Replay never fakes a success: a missing, corrupted or tampered body and a changed configuration each fail | same artifact |
| ✅ | Every failure is detected with a machine-readable class, and each detected loop carries exactly one attribution (`proxy_escalated` / `client_guard_handled`) | [`docs/benchmarks/criteria-measure-2026-10-23.json`](docs/benchmarks/criteria-measure-2026-10-23.json) |
| ✅ | The evidence log reconstructs what happened, rule by rule, and arguments are never invented | same artifact; ADR 0005 |
| ❌ | The model writes correct code, or tasks complete more often | **no completion-rate improvement is claimed** — the measured held-out difference is −12.6 points (p = 0.25) |
| ❓ | Benchmarks from earlier matrices, non-interference under unevaluated conditions, a general run-order effect | **unverified** — see [`docs/DECISION.md`](docs/DECISION.md) §9 |

The value hypothesis the evidence supports: a deterministic proxy makes the
recurring failures of an agent on a constrained local model *observable*,
*reproducible* and *auditable*. It does not turn them into completions.

## Install

```bash
uv venv
uv pip install -e ".[dev]"
```

## Quick start

```bash
# 1. Write a starter configuration.
pocket-harness init pocket.toml

# 2. Point it at your local inference server and check the connection.
pocket-harness doctor --config pocket.toml

# 3. Run the proxy.
pocket-harness serve --config pocket.toml
```

Then point your agent at the proxy:

```bash
# Cline / OpenCode: set Base URL to http://127.0.0.1:8787/v1
```

The proxy also exposes a few non-standard endpoints for inspection:

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness probe. |
| `GET /v1/models` | Lists the configured upstream model. |
| `GET /pocket/status` | Deterministic status report for the current run. |
| `POST /pocket/flush` | Returns the run's evidence events. |

## Commands

| Command | Purpose |
|---|---|
| `serve` | Run the local proxy. |
| `doctor` | Check upstream connectivity, model/context config, streaming, tool-call round trip. |
| `bench` | Run the fixed local task set and emit raw results + summary. |
| `replay` | Re-apply proxy transformations to captured payloads and compare output hashes. |
| `report` | Summarize completion, failure taxonomy, transformations, latency, usage. |
| `status` | Show a deterministic run/ledger status (no LLM involved). |
| `init` | Write a starter `pocket.toml`. |

Every command except `serve` accepts `--config`, `--upstream`, `--model`, `--context`,
`--capture`, and `--profile`, so a one-off run works without a configuration file.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. For `doctor` and `replay`, all checks passed / the hashes matched. |
| `1` | A check failed, a hash did not match, or the run could not be completed. |
| `2` | The configuration file could not be used (invalid TOML or invalid values). |

## Privacy defaults

- `capture = "metadata"` — hashes, IDs, rules, timings, token sources. No prompt bodies.
- `capture = "local-replay"` — explicit opt-in; stores minimal canonical payloads in
  content-addressed local blobs so `replay` can actually reconstruct requests.
- No telemetry. All outbound traffic goes to a user-configured upstream.
- Authorization headers are redacted before they reach the evidence log.

The capture mode is a hard switch: with `metadata` **no** request body is written to
disk at all, and `replay` will say so instead of pretending to verify anything.

**Where the bodies go.** With `capture = "local-replay"` the canonical request body is
written to `<state.root>/blobs/<sha256>` and the file name *is* the content hash; the
run's `events.jsonl` only stores the reference. With the default `metadata` mode
nothing is written at all — the run log holds hashes, rule ids, token counts and
timings. `capture` is enabled per configuration file or per command line
(`--capture local-replay`); it is never enabled by the proxy itself.

**What can still be in the log.** The metadata mode stores hashes, the requested and
observed model ids, the rule ids that fired, token counts and error classes — no
message bodies. The `local-replay` mode stores the request body, which can contain
source code, file contents, prompts and any credential that happened to be inside a
message. Redaction of authorization headers is best-effort and is not a guarantee that
arbitrary secrets are detected.

## Configuration

The configuration is a TOML file. Every value that can change a transformation outcome
participates in the **configuration hash**, which is recorded in each run's manifest so a
replay can prove it used the same settings.

```toml
[server]
host = "127.0.0.1"
port = 8787

[state]
root = "~/.pocket"          # runs, evidence events, and the local blob store

[upstream]
base_url = "http://127.0.0.1:11434/v1"
model = "qwen2.5-coder:7b"
context_length = 32768      # required for C2 to enforce a budget
supports_count_endpoint = false
unsupported_fields = []
optional_fields = []

[client]
profile = "cline"
pinned_tools = []
destructive_tools = []

[context]
start_at = 0.85            # begin compressing above this fraction of the budget
target_at = 0.70           # compress down to this fraction
keep_recent_turns = 4
system_prompt_mutation = false
reserved_output_tokens = 1024
safety_margin_tokens = 256
max_tool_output_bytes = 8192

[tools]
max_non_pinned = 6
sticky_unused_turns = 5
fuzzy_tool_name_repair = false
max_description_chars = 200

[repair]
max_corrective_retries_per_request = 1
max_corrective_retries_per_task = 3
allow_fuzzy_tool_name = false
allow_schema_defaults = false

[loop_guard]
repeated_call_threshold = 3
max_steps_per_task = 40
max_wall_seconds = 1800
detect_oscillation = true

[logging]
capture = "metadata"       # or "local-replay"
telemetry = false

[routing]
enabled = false
allow_paid_fallback = false
```

> **Windows paths.** A path with backslashes must use a literal string
> (`root = 'C:/Users/me/.pocket'`) or forward slashes. In a basic string `"\U"` is an
> invalid escape sequence and the loader will reject the file with a hint.

The configuration file is looked up in this order: `--config`, then `~/.pocket/pocket.toml`,
then `./pocket.toml`, then built-in defaults.



## Status

The completion-rate hypothesis was **not** supported and is withdrawn as a
claim (G1 "stop"); the value hypothesis is the redefined one — detection,
determinism, auditability, non-interference — with its own measured criteria
and its own open items.
The decision, the assurance matrix and the full evidence index are in
[`docs/DECISION.md`](docs/DECISION.md).
The transformation pipeline below is described as designed; the verified status
of each piece is in the decision document.


## The transformation pipeline

| ID | Component | What it does |
|---|---|---|
| C0 | Canonical request | Normalizes any supported client request into one internal representation. |
| C1 | Tool selector | Exposes a smaller, deterministic subset of the client's tools. |
| C2 | Context budgeter | Compresses tool output and old turns to fit the context window. |
| C3 | Tool-call repair | Validates a model tool call against the **original** client schema and repairs it at the syntax level only. |
| C4 | Loop guard | Detects repeated calls, oscillation, and step/wall-clock overruns, and escalates with a deterministic warning. |
| C6 | Router | Resolves the request to an upstream route. |

Every step is a pure function of `(input, configuration, pipeline version)`. No network
call, no LLM, and no wall-clock-dependent decision influences a transformed request —
which is exactly what makes `replay` able to reproduce a request hash.

### Do the mechanisms actually fire?

A mechanism that never fires cannot improve anything, and its absence in a
benchmark says nothing about the mechanism. `tests/test_mechanism_firing.py`
builds the condition each mechanism targets and checks that it fires, with the
right result and the right invariants:

| Mechanism | The condition that must make it fire | What is asserted |
|---|---|---|
| C1 | more tools than the cap | the forwarded request exposes at most `max_non_pinned` + the pinned tools, the `C1.select` rule is recorded, the selection is sticky, and a call to a *hidden* tool is still validated |
| C2 | a request above 85 % of the budget | the request is compressed, the system prompt survives verbatim, tool-call/result pairs stay valid, applying the policy twice is idempotent, and the result stays inside the budget |
| C3 | a malformed tool call | the client receives the repaired arguments, an unrepairable call is passed through untouched, a destructive tool is never renamed, and a call that was already usable is left alone |
| C4 | a repeated call or an A-B-A-B oscillation | the escalation is appended to the next request, the `C4.repeat_warning` rule is recorded, the model/temperature/seed are never touched, and the same input yields the same hash |

Two design decisions that these tests depend on are recorded in
[`docs/decisions/`](docs/decisions/):

* [ADR 0001 — where a repaired tool call is applied](docs/decisions/0001-c3-application-point.md)
* [ADR 0002 — what C4 does when it detects a loop](docs/decisions/0002-c4-escalation.md)
* [ADR 0003 — whether the proxy may refuse a request](docs/decisions/0003-c4-abort-path.md)
* [ADR 0004 — the corrective retry is opt-in](docs/decisions/0004-corrective-retry-default-off.md)
* [ADR 0005 — the client answers an unusable tool call precisely](docs/decisions/0005-precise-tool-error.md)

## Evidence and state

State lives under `[state].root` (default `~/.pocket`):

```
~/.pocket/
├── state.sqlite            # run and event index
├── runs/<run-id>/events.jsonl
└── blobs/<sha256>          # content-addressed request bodies (local-replay only)
```

Each request produces an ordered set of evidence events:

| Event | Meaning |
|---|---|
| `request.received` | The client request was accepted. |
| `request.transformed` | The pipeline ran; carries the input/output hashes, the rules applied, the token counts and `transform_ms` (the proxy-only cost). |
| `request.routed` | The route that was resolved. |
| `request.captured` | The request body was stored locally (only with `local-replay`). |
| `toolcall.validated` | The model's tool calls were checked against the client's schema. `delivered_as_text` appears when the upstream wrote the call into the message text instead of the structured field. |
| `response.completed` | The upstream response was returned. |

### What the hashes, the model id and the error fields mean

* **Hashes** are SHA-256 over a *canonical JSON* encoding (sorted keys, no
  insignificant whitespace, UTF-8). `input_sha256` is the hash of the canonical
  request **as C0/C1/C2/C6 received it**; `output_sha256` is the hash of the
  canonical request **after** the pipeline. Two runs with the same input, the
  same configuration and the same `pipeline_version` must produce the same
  output hash — that is what `pocket replay` checks. A blob reference
  (`sha256:…`) names the file in `blobs/` that holds the same bytes.
* **The model id** in the evidence log is the id the *upstream* reported for
  the request, next to the id the client asked for. When the upstream does not
  report one, the field stays empty — the configured id is not passed off as
  the observed one.
* **Error fields** carry the HTTP status the upstream returned, the error
  class of a failed tool call (`M.parse`, `M.schema`, `M.name`), the proxy's
  own error type (`upstream_unavailable`, `context_overflow`,
  `client_unsupported_field`) and the number of retries. A failure is never
  reported as a success and never silently retried into a different provider.

## Replay

`replay` re-applies the pipeline to a captured request body and compares the resulting
hash with the one recorded at capture time.

```bash
# Uses the body the proxy captured locally, if there is one.
pocket-harness replay --run run-1792596871-7635b2d9

# Or replay an explicit payload file.
pocket-harness replay --run run-1792596871-7635b2d9 --capture-file capture.json
```

The report distinguishes three things, and does not conflate them:

- `matched` — the replayed output hash equals the recorded output hash.
- `config_matched` — the configuration hash equals the one recorded in the run manifest.
  A mismatch means the reproduction claim is **not** established, even if the hashes
  happen to agree.
- `input_matched` — the replayed input hash equals the recorded input hash.

`ok` is true only when both the transformation and the configuration match.

| `upstream.error` | The upstream failed. The proxy does not hide it. |
| `request.rejected` | The client request was invalid. |
| `request.context_overflow` | The request could not fit the budget without corrupting it. |
| `loop.detected` | The loop guard fired. |


## Benchmark

`bench` runs the fixed local task set against the configured upstream in two conditions
(`harness` and `baseline`) and writes the raw records plus a summary.

```bash
pocket-harness bench --limit 5 --seed 3 --out ./bench-results
```

Success is judged by **deterministic checks only**. No LLM judge is used, and the raw
output carries an explicit note saying so.

### The repository suite (`--suite repo`)

The single-turn set above is a *proxy smoke test*: it cannot answer the questions of
Plan.md §11, which are about coding tasks, worktrees, a development/held-out split,
several seeds and a failure taxonomy. `--suite repo` runs the suite in
`bench/repos/`:

* a task is a repository snapshot plus a prompt, a test command and optional
  forbidden-file constraints;
* every run gets a fresh copy of the snapshot (a clean worktree);
* pass/fail is the **exit status of the test command** plus the forbidden-file
  check — never an LLM judge;
* the same agent code drives both conditions; only the endpoint differs;
* every run records the model id, the steps, the timings, the token counts, the
  failure class and the interventions that actually fired;
* runs that cannot be evaluated are recorded as excluded **with a reason** and are
  never silently dropped.

```bash
# the whole matrix
pocket-harness bench --config bench/bench-local.toml --suite repo --seeds 1,2,3 \
    --out ./bench-results

# only the tuning split, or only the held-out split
pocket-harness bench --suite repo --split dev
pocket-harness bench --suite repo --split held-out

# exit non-zero when a gate is not met
pocket-harness bench --suite repo --require-gates
```

The summary contains the completion rate with a 95% Wilson interval, the paired
outcomes, an exact McNemar test, the O/M/L/S/W/R failure classes, the
**intervention coverage** and the **two evaluation tracks** (mechanism
diagnostics and task completion). A comparison in which no intervention ever
fired cannot show an effect, whatever the completion numbers look like.

### The mechanism diagnostics (`diagnose`)

```bash
pocket-harness diagnose --config bench/bench-local.toml
```

Six deterministic scenarios create each mechanism's condition on purpose — more
tools than the cap, a conversation over the budget, a repairable and an
unrepairable tool call, a repeated call and an oscillation — and check the
mechanism's obligation. No model and no network are involved, so the result is
reproducible by construction. **This is the diagnostic track: it says whether a
mechanism did its job, never that a task got completed.**

The gate verdicts (G0 problem fit, G1 demonstrated value, public release) and
the decision that follows from them are in
[`docs/DECISION.md`](docs/DECISION.md). The raw records of every run quoted
there are in [`docs/benchmarks/`](docs/benchmarks/).

## Tested upstream capability profile

Plan.md §4.1 requires a *tested* capability profile rather than an assumption of full
OpenAI API parity. The table below records what was actually observed.

**Ollama 0.40.6, endpoint `http://127.0.0.1:11434/v1`, measured 2026-10-22.**
Each cell is the number of **3 identical requests** that produced a structured
`tool_calls` entry. The upstream is not reproducible (see below), so a rate is
reported instead of a yes/no.

| Model | `/v1/models` | Unary chat | Streaming | Structured `tool_calls` (3 probes) | What it returns instead |
|---|---|---|---|---|---|
| `qwen2.5:7b-instruct` | yes | yes | yes | **2/3** | one probe answered in text |
| `qwen2.5:7b-instruct-q4_K_M` | yes | yes | yes | **3/3** | same weights as the tag above (digest `845dbda0ea48`) |
| `llama3.1:8b-instruct-q4_K_M` | yes | yes | yes | **3/3** | — |
| `qwen2.5:1.5b` | yes | yes | yes | **1/3** | the call as JSON text |
| `qwen2.5-coder:7b` | yes | yes | yes | 0/3 | the call as JSON text |
| `gpt-4o:latest` | yes | yes | yes | 0/3 | the call as JSON text |
| `goldstate-coder:latest` | yes | yes | yes | 0/3 | the call inside a `<tools>` block |
| `qwen3:4b` | yes | yes | yes | 0/3 | an empty message (3/3 structured on the native endpoint) |
| `deepseek-r1:1.5b` | yes | yes | yes | 0/3 | an empty message |
| `phi4-mini:latest` | yes | yes | yes | 0/3 | `[tool({"..."})]` — not JSON |

Two corrections to the earlier version of this table, both backed by the
measurements above: `llama3.1:8b-instruct-q4_K_M` **does** return structured
tool calls, and `qwen2.5:7b-instruct` returns them **unreliably** (2 of 3).

The practical consequence: **model choice determines whether tool calling works at
all, and even then only sometimes.** When the upstream does not return structured
`tool_calls`, Pocket Harness cannot validate a tool call (C3 has nothing to validate)
and the client receives the model's text instead. `pocket-harness doctor` reports this as a
failed `tools.round_trip` check rather than pretending the round trip works.

Note also that Ollama's **native** endpoint (`/api/chat`) returns structured tool calls
for some models that its OpenAI-compatible endpoint does not (`qwen3:4b`: 3/3 native,
0/3 OpenAI-compatible). Pocket Harness v0.1 talks only to the OpenAI-compatible endpoint.

Verify your own setup with:

```bash
pocket-harness doctor --upstream http://127.0.0.1:11434/v1 --model <your-model>
python scripts/probe_upstream.py http://127.0.0.1:11434 3   # every model, 3 probes each
```

### The upstream is not reproducible — unless it is pinned

Ollama 0.40.6 accepts a `seed` field but **ignores** it: the same request with
`seed=42` and with `seed=7` produced byte-identical answers. More importantly, the
**same request sent repeatedly produces different answers** — four identical
multi-turn requests returned one structured tool call and three different text
answers (`scripts/probe_reproducibility.py`).

The cause turned out to be the *sampling*, not the server
(`scripts/probe_determinism.py`, 5 rounds per model and setting):

| setting | reproducible models (of 10 tags) |
|---|---|
| default sampling | 0 |
| `temperature = 0` | 8 |
| **`temperature = 0` + `seed = 42`** | **10** |

The seed is accepted and ignored: with greedy decoding the three predeclared
seeds produce the *same* answer, so they are three reproducible replicates and
not three different samples. The upstream's server-side prefix cache is *not*
the cause — the same answer comes back even when other traffic is interleaved
(`scripts/probe_cache_state.py`).

The benchmark therefore pins `temperature = 0` and the seed
(`bench --temperature 0 --seeds 1,2,3`) and reports the limitation instead of
claiming seeded reproducibility.

### The upstream sometimes answers with nothing

With `temperature = 0` the model occasionally emits a generation that decodes
into **no content and no tool call**: `done_reason: stop`, `eval_count: 65`,
and an empty message — on both the native and the OpenAI-compatible endpoint,
and the raw stream carries nothing (`scripts/probe_empty_endpoints.py`,
`scripts/probe_empty_stream.py`). The trigger is a brittle interaction between
the system prompt and a conversation that already contains two substantial
tool results; it is deterministic, so a retry of the *same* request cannot
recover it.

Asking the model to continue *does* recover it
(`scripts/probe_empty_recovery.py`), so the benchmark's client asks once more
and counts the retry.

## Development

```bash
uv pip install -e ".[dev]"
pytest tests -q
ruff check src tests
```

The test suite uses a mock OpenAI-compatible upstream, so no inference server is needed
and nothing depends on a model's behaviour.

Two scripts help with real-device verification:

```bash
# Drive the full CLI chain (serve -> request -> status -> report -> replay).
python scripts/e2e_cli_check.py
python scripts/e2e_cli_check.py --real http://127.0.0.1:11434/v1 qwen2.5:7b-instruct

# Verify a different installation (e.g. a fresh venv) the way a third party runs it.
$env:PH_E2E_PYTHON = "path/to/other/venv/Scripts/python.exe"; python scripts/e2e_cli_check.py

# Probe what a real upstream actually supports.
python scripts/probe_upstream.py http://127.0.0.1:11434
```

## Limitations

- Only the `openai-chat-completions` client protocol and `openai-compatible` upstreams
  are supported in v0.1.
- Token counts are estimates unless the upstream exposes a count endpoint
  (`supports_count_endpoint = true`).
- Context budgeting is disabled when the context window is unknown; the proxy warns
  rather than assuming a large window.
- Tool-call repair is limited to the syntax level. It never changes a tool's argument
  schema, and destructive tools are never repaired beyond that. A repaired call is
  *reported* in the evidence log, not substituted into the client's response: the
  client owns tool dispatch.
- Structured tool calls are only possible when the upstream returns a `tool_calls`
  array. See the capability profile above; even then the rate is not 100%.
- The performance gates of `Plan.md` §11.5 are **not met**. No completion-rate
  improvement is claimed.

## Status

v0.1. The core pipeline, proxy, evidence system, replay, report and both benchmark
suites are implemented and covered by tests.

**Implemented and verified by tests** — the transformation pipeline (C0/C1/C2/C6),
the proxy and its evidence log, the replay round trip (including the persisted,
cross-process variant), the failure taxonomy with the loop-attribution split,
the statistics, the repository benchmark and the gate evaluation.

**Implemented and demonstrated against a real Ollama 0.40.6 upstream** —
`doctor` passes all six checks; the `serve → request → status → report → replay`
chain completes with a matching replay hash; the proxy-only overhead is p50 1.2 ms /
p95 5.6 ms, well inside the plan's p95 ≤ 50 ms target; and the replay was proven
end to end across a process restart (input/config/output hashes and rule order,
with the failure modes refusing to fake a success —
[`docs/benchmarks/persisted-replay-e2e-2026-10-23.json`](docs/benchmarks/persisted-replay-e2e-2026-10-23.json)).

The archived benchmark matrix reproduced 36/36 cells identically
(`docs/benchmarks/` holds the one archived run; the other two were not
preserved, so the three-way agreement claim rests on it — DECISION.md §8.3).

**Implemented but *not* demonstrated** — the performance gates of `Plan.md` §11.5.
The evaluation now uses **42 repository tasks** (27 development, 15 held-out, all
proven broken-before/fixed-after) and reports **two separate tracks**:

| track | question | result |
|---|---|---|
| **A — mechanism diagnostics** | do C1–C4 do their job when their condition arises? | **yes**: 6 of 6 deterministic scenarios pass; on the held-out split C3 validated 213 calls and detected 33 invalid ones, C2 compressed 14 times (26 392 tokens), C4 detected 10 repetitions and escalated all of them |
| **B — task completion** | do tasks get finished? | **no improvement**: baseline 18/42 (42.9 %) vs harness 10/33 (30.3 %), paired difference **−12.6 points**, exact McNemar p = 0.25; paired outcomes 9 both passed, 18 both failed, **3 baseline only, 0 harness only** |

| gate | verdict | why |
|---|---|---|
| G0 — problem fit | **pass** | 15 of 24 eligible baseline failures (62 %) are attributable to O/M/L/S: the local failures really are malformed tool calls and repetitions |
| G1 — demonstrated value | **stop** | the held-out difference is **−12.6 points**; below +10 the plan requires a stop and a re-evaluation of the premise |
| Public release gate | **pass** | 42 tasks (≥ 40) with a separate held-out slice, and the configurations, manifests, raw outcomes, failure examples and limitations are published here |
| §2 proxy overhead | **met** | p50 0.99 ms, p95 7.99 ms |

**No completion-rate improvement is claimed.** The dominant failure is class
**M** — the model emits `write_file` with *no arguments at all*. C3 detects it
and must not invent the missing values (§6 C3 §3), the client answers with a
precise schema-derived error ([ADR 0005](docs/decisions/0005-precise-tool-error.md)),
and the model usually repeats the call. The deterministic tools cannot fix a
model that will not produce a well-formed call.

The outcome of the evaluation, the decision that follows from it and the next
minimal step are in **[`docs/DECISION.md`](docs/DECISION.md)**.
