# Pocket Harness

Local-first, deterministic **request/response proxy and execution-trace system** that sits
between an existing coding agent (Cline, OpenCode, …) and a local inference endpoint.

Pocket Harness does not train models and does not use a separate LLM to summarize context,
judge success, or choose interventions. Its core logic is inspectable transformation code,
schema validation, trace-derived state, and bounded recovery behavior.

See [`Plan.md`](./Plan.md) for the full design specification (v0.2-design).

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

Implementation in progress. The benchmark numbers are **not** claimed yet; see `Plan.md` §11–§12
for the gates that must pass before any performance claim is made.


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
outcomes, an exact McNemar test, the O/M/L/S/W/R failure classes and the
**intervention coverage** — how often C1, C2 and C3 actually changed something. A
comparison in which no intervention ever fired cannot show an effect, whatever the
completion numbers look like.

The gate verdicts (G0 problem fit, G1 demonstrated value, public release) are in
[`docs/PERFORMANCE-GATE-AUDIT.md`](docs/PERFORMANCE-GATE-AUDIT.md). **They are not
met.** The raw records of every run quoted there are in
[`docs/benchmarks/`](docs/benchmarks/).

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
and the client receives the model's text instead. `pocket doctor` reports this as a
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
the proxy and its evidence log, the replay round trip, the failure taxonomy, the
statistics, the repository benchmark and the gate evaluation.

**Implemented and demonstrated against a real Ollama 0.40.6 upstream** —
`doctor` passes all six checks; the `serve → request → status → report → replay`
chain completes with a matching replay hash; the proxy-only overhead is p50 1.2 ms /
p95 5.6 ms, well inside the plan's p95 ≤ 50 ms target; the benchmark matrix is
**fully reproducible** (36/36 cells identical across three independent runs) once
the sampling is pinned and the volatile parts of the tool output are removed.

**Implemented but *not* demonstrated** — the performance gates of `Plan.md` §11.5.
G0 and G1 were measured and **both fail**, now under conditions that are
reproducible and in which the mechanisms actually fire (C2 compressed 11 211
tokens, C4 escalated three times). The completion difference is **0.0 points**:
the interventions fired and changed nothing. The measured reason is that the
local baseline failures are dominated by class **W** (wrong semantic edit,
constraint violation, the model not acting) rather than by O/M/L/S, and that half
of the runs are lost to upstream empty completions. See
[`docs/PERFORMANCE-GATE-AUDIT.md`](docs/PERFORMANCE-GATE-AUDIT.md) for the
numbers, the raw records and the reasoning.

Still open:

- The §11.1 task suite has 6 tasks, not 20, and the public release gate needs 40.
- C1 never fires on this suite (the agent sends three tools, the cap is six) and
  C3 never has anything to repair (9 invalid calls, 0 repairable).
- No MoE model, no LM Studio, no Anthropic Messages API / Claude Code adapter.
- The seeded-reproducibility requirement can only be met in the *reproducibility*
  sense: the seed is accepted and ignored, so the three seeds are three
  identical replicates.
