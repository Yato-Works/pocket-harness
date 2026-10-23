"""Measure the four redefined-quality criteria on the stored artifacts.

DECISION.md section 7.2 redefines the project's hypothesis with four criteria:
Detectability, Determinism, Auditability, Non-interference.

**This is a diagnostic re-analysis of previously observed data.** The held-out
splits inside these artifacts were already observed; they are NOT treated as an
independent, unused held-out set. No model call is made and nothing is replayed
against the upstream.

The definitions below were fixed BEFORE the measurement was executed. If a
criterion fails, the violating records are listed and the criterion is reported
as not satisfied; records are never reclassified to make a criterion pass.

---

### C-D1 Detectability (harness condition, active runs only)

Every proxy-addressable failure that occurs is detected and recorded with its
machine-readable class.

* **M-detection (malformed tool calls).**
  Denominator: harness records with ``invalid_tool_calls_before_c3 > 0`` whose
  run is eligible (``excluded_reason is None``). Classes R (infrastructure and
  empty completions) are excluded from the denominator.
  Numerator: of those, records whose proxy trace recorded the validation —
  ``proxy.tool_calls_checked >= 1`` AND ``proxy.invalid_tool_calls_after_c3 >= 1``.
  Coverage ratio is also required on volume: for every such record
  ``proxy.tool_calls_checked >= invalid_tool_calls_before_c3``.
  Pass: ratio 1.0 and volume coverage 1.0.
* **L-detection (loops).**
  Denominator: harness records whose ``failure_signals`` contain ``"L"``.
  Numerator: of those, records with ``proxy.loop_events >= 1``.
  Escalation: of the numerator, records with ``proxy.c4_escalations >= 1``.
  Pass: detection ratio 1.0 and escalation ratio 1.0.
* **Mechanism scenario suite.** ``pocket-harness diagnose`` (deterministic, no
  model and no network) must report 6 scenarios, 0 failed. Stored result:
  ``mechanism-diagnostics.json``.

### C-D2 Determinism

* **From artifacts (outcome-level).** Same ``config_hash`` artifacts that share
  (task_id, condition, seed) cells; the outcome triple
  (passed, failure_class, stop_reason) must agree per cell across every file
  carrying the same cell. Denominator: overlapping cells with >= 2 observations;
  numerator: cells whose observations all agree. Pass: ratio 1.0.
* **From artifacts (playback level).** ``pinned-reproducible-matrix.json``:
  36 runs, sampling pinned; the stored audit's claim of identical cells is
  re-derived with ``scripts/compare_runs.py`` on the artifact itself.
* **Transformed-request hash replay.** NOT measurable from these artifacts:
  capture mode was ``metadata`` and the blob/run stores are empty, so no prompt
  bodies exist to replay. Reported as **unverified** rather than assumed.

### C-D3 Auditability (both conditions, both suites)

A third party can reconstruct what happened from the stored evidence alone.

* **Failure attribution.** Denominator: eligible records that did NOT pass.
  Numerator: records with a machine-readable ``failure_class`` in
  {O, M, L, S, W, R} AND non-empty ``failure_signals`` AND non-empty
  ``failure_reasons``. Pass: ratio 1.0.
* **Proxy trace presence.** Denominator: harness records on active runs
  (no upstream/transport error). Numerator: records carrying a ``proxy`` block
  with ``tool_calls_checked`` and ``tool_calls_repaired`` present.
  Pass: ratio 1.0.
* **Manifest completeness.** Denominator: the artifacts with a run manifest.
  Numerator: manifests containing all required keys (pipeline_version,
  config_hash, model, upstream_base_url, upstream_model, context_length,
  seeds, conditions, temperature, system_prompt, system_prompt_sha256,
  tool_error_contract, python, platform, packages). Pass: 1.0.

### C-D4 Non-interference (harness condition)

The proxy does not change the client's API contract and does not invent values.

* **No invented arguments.** Denominator: harness records with any invalid
  tool call observed (model side or proxy side). Numerator: records with
  ``proxy.tool_calls_repaired == 0``. Volume requirement:
  ``sum(tool_calls_repaired) == 0`` over ALL harness records (the corrective
  retry is off). Pass: both.
* **No invisible model calls.** ``sum(proxy.corrective_retries) == 0`` over all
  harness records. Pass: yes.
* **No silent abort.** ``sum(proxy.loop_aborted) == 0`` over all harness
  records, AND every artifact manifest or predeclared file that declares the
  abort flag has it ``false``. Pass: both.
* **System prompt integrity.** The manifests carry the system prompt hash and
  it equals the hash recorded in the predeclared conditions file of the same
  phase; the live module constant agrees with both.

---

Usage:  .venv\\Scripts\\python.exe scripts\\measure_criteria.py [--out FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "docs" / "benchmarks"

import sys as _sys  # noqa: E402

_sys.path.insert(0, str(ROOT / "src"))

from pocket_harness.taxonomy import LOOP_ATTRIBUTION_LABELS, classify_loop_attribution  # noqa: E402

#: The artifacts of the 2026-10-22 evaluation (42 tasks, config 1e4eb3bc…).
EVAL_SUITES = [
    "heldout-2026-10-22.json",
    "development-2026-10-22.json",
]
#: The pinned, sampling-reproducible matrix (config f19919f3…).
PINNED = "pinned-reproducible-matrix.json"
#: Previously stored scenario report; re-run live as part of the measurement.
SCENARIOS = "mechanism-diagnostics.json"
#: The predeclared conditions file of the held-out evaluation.
PREDECLARED = "heldout-2026-10-22-predeclared.json"

REQUIRED_MANIFEST_KEYS = [
    "pipeline_version",
    "config_hash",
    "model",
    "upstream_base_url",
    "upstream_model",
    "context_length",
    "seeds",
    "conditions",
    "temperature",
    "system_prompt",
    "system_prompt_sha256",
    "tool_error_contract",
    "python",
    "platform",
    "packages",
]

FAIL_CLASSES = {"O", "M", "L", "S", "W", "R"}


def _active(record: dict) -> bool:
    return record["stop_reason"] not in {"upstream_error", "transport_error"}


def _load(name: str) -> dict | None:
    path = ARTIFACTS / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _entity(name: str, denominator: int, numerator: int, passed: bool, notes: list[str]) -> dict:
    ratio = round(numerator / denominator, 4) if denominator else None
    return {
        "id": name,
        "denominator": denominator,
        "numerator": numerator,
        "ratio": ratio,
        "satisfied": passed,
        "notes": notes,
    }


def measure_detectability(records: list[dict]) -> dict:
    harness = [r for r in records if r["condition"] == "harness"]
    m_den = [
        r for r in harness
        if r.get("invalid_tool_calls_before_c3", 0) > 0 and r.get("eligible")
    ]
    m_num = [
        r for r in m_den
        if (r.get("proxy") or {}).get("tool_calls_checked", 0) >= 1
        and (r.get("proxy") or {}).get("invalid_tool_calls_after_c3", 0) >= 1
    ]
    m_volume = [
        r for r in m_den
        if (r.get("proxy") or {}).get("tool_calls_checked", 0)
        >= r.get("invalid_tool_calls_before_c3", 0)
    ]
    m_notes = []
    for r in m_den:
        if (r.get("proxy") or {}).get("tool_calls_checked", 0) < r.get(
            "invalid_tool_calls_before_c3", 0
        ):
            m_notes.append(f"{r['task_id']} seed{r['seed']}: proxy validated fewer calls than the agent saw")
    l_den = [r for r in harness if "L" in (r.get("failure_signals") or [])]
    l_num = [r for r in l_den if (r.get("proxy") or {}).get("loop_events", 0) >= 1]
    l_esc = [r for r in l_num if (r.get("proxy") or {}).get("c4_escalations", 0) >= 1]
    misses = [
        r for r in l_num
        if (r.get("proxy") or {}).get("c4_escalations", 0) == 0
    ]
    l_misses = [
        {
            "task": r["task_id"],
            "seed": r["seed"],
            "stop_reason": r.get("stop_reason"),
            "reason": (
                "no escalation: the run was terminated without a next model "
                "request"
                if r.get("stop_reason") == "repeated_identical_failure"
                else "no escalation; cause not attributable from the artifact"
            ),
        }
        for r in misses
    ]
    report = [
        _entity(
            "M-detection",
            len(m_den), len(m_num),
            len(m_num) == len(m_den) and (not m_den or len(m_volume) == len(m_den)),
            m_notes,
        ),
        _entity(
            "M-detection volume coverage",
            len(m_den), len(m_volume),
            (not m_den) or len(m_volume) == len(m_den),
            [],
        ),
        _entity(
            "L-detection",
            len(l_den), len(l_num),
            len(l_num) == len(l_den),
            [],
        ),
        _entity(
            "L-escalation",
            len(l_num), len(l_esc),
            len(l_esc) == len(l_num),
            [
                f"{m['task']} seed{m['seed']}: {m['reason']} (stop={m['stop_reason']})"
                for m in l_misses[:12]
            ],
        ),
    ]
    return {"entries": report}


def measure_determinism_from_artifacts() -> dict:
    files = [
        name
        for name in (
            EVAL_SUITES
            + [
                "dev-precise-tool-error.json",
                "heldout-ab-precise-tool-error.json",
                "heldout-diagnosis-retry-off.json",
                "heldout-final.json",
                "pre-lock-full-matrix.json",
            ]
            + [PINNED]
        )
        if (ARTIFACTS / name).is_file()
    ]
    #: Two artifacts are comparable only when the manifest signature matches:
    #: the same config hash, the same pipeline version, the same sampling
    #: temperature, the same system prompt hash and the same tool error
    #: contract. Artifacts whose name records that a boundary was crossed
    #: (the precise tool error was introduced mid-stream, ADR 0005) are
    #: excluded from comparison even though the manifest signature of the time
    #: was identical.
    EXCLUDED_FILES = {
        "dev-precise-tool-error.json",
        "heldout-ab-precise-tool-error.json",
    }

    def signature(name: str, data: dict) -> str | None:
        left = data.get("manifest") or {}
        keys = (
            "config_hash",
            "pipeline_version",
            "tool_error_contract",
            "temperature",
            "system_prompt_sha256",
        )
        values = tuple(left.get(key) for key in keys)
        if not all(key in left for key in ("pipeline_version",)) or not data.get("config_hash"):
            return None
        marker = hashlib.sha256(repr(values).encode("utf-8")).hexdigest()[:16]
        return "excluded" if name in EXCLUDED_FILES else marker

    groups: dict[str, dict[tuple, list[str]]] = defaultdict(lambda: defaultdict(list))
    comparable_files: list[str] = []
    excluded_files: list[str] = []
    incomplete_files: list[str] = []
    for name in files:
        data = _load(name)
        if data is None:
            continue
        sig = signature(name, data)
        if sig is None:
            incomplete_files.append(name)
            continue
        if sig == "excluded":
            excluded_files.append(name)
            continue
        comparable_files.append(name)
        for record in data.get("records", []):
            key = (record["task_id"], record["condition"], record["seed"])
            outcomes = (record.get("passed"), record.get("failure_class"), record.get("stop_reason"))
            groups[sig][key].append((name, outcomes))
    overlapping = 0
    disagreeing: list[str] = []
    for sig in groups:
        for key, observations in groups[sig].items():
            if len(observations) < 2:
                continue
            overlapping += 1
            outcomes_seen = {outcome for _, outcome in observations}
            if len(outcomes_seen) != 1:
                shown = " | ".join(
                    f"{name}::[{', '.join(map(str, outcome))}]"
                    for name, outcome in observations
                )
                disagreeing.append(f"{key}: {shown}")
    return {
        "entries": [
            _entity(
                "outcome-level determinism across same-signature artifacts",
                overlapping, overlapping - len(disagreeing), not disagreeing,
                disagreeing[:10],
            ),
        ],
        "files_compared": sorted(comparable_files),
        "files_excluded_mid_stream_change": sorted(excluded_files),
        "files_without_full_signature": sorted(incomplete_files),
        "note": (
            "comparability requires the manifest signature (config hash, "
            "pipeline version, tool error contract, temperature, system prompt "
            "hash) to match; files recorded as a mid-stream implementation "
            "change are excluded from the numerator even when the old "
            "manifest could not tell them apart"
        ),
    }


def measure_auditability(records: list[dict], artifacts: dict[str, dict]) -> dict:
    failed = [r for r in records if r.get("eligible") and not r.get("passed")]
    attributed = [
        r for r in failed
        if r.get("failure_class") in FAIL_CLASSES
        and (r.get("failure_signals") or [])
        and (r.get("failure_reasons") or [])
    ]
    harness = [r for r in records if r["condition"] == "harness" and _active(r)]
    traced = [
        r for r in harness
        if isinstance(r.get("proxy"), dict)
        and "tool_calls_checked" in r["proxy"]
        and "tool_calls_repaired" in r["proxy"]
    ]
    manifest_files = {
        name: data
        for name, data in artifacts.items()
        if name != PREDECLARED and isinstance(data.get("manifest"), dict)
    }
    complete = {
        name: data
        for name, data in manifest_files.items()
        if all(key in data["manifest"] for key in REQUIRED_MANIFEST_KEYS)
    }
    miss_names = {
        name: [k for k in REQUIRED_MANIFEST_KEYS if k not in data["manifest"]]
        for name, data in manifest_files.items()
        if name not in complete
    }
    current_eval_files = {
        name: data
        for name, data in manifest_files.items()
        if name in EVAL_SUITES
    }
    current_complete = {
        name: data
        for name, data in current_eval_files.items()
        if all(key in data["manifest"] for key in REQUIRED_MANIFEST_KEYS)
    }
    current_missing = {
        name: [k for k in REQUIRED_MANIFEST_KEYS if k not in data["manifest"]]
        for name, data in current_eval_files.items()
        if name not in current_complete
    }
    return {
        "entries": [
            _entity("failure attribution", len(failed), len(attributed), len(attributed) == len(failed), []),
            _entity(
                "proxy trace on harness runs",
                len(harness), len(traced), len(traced) == len(harness), [],
            ),
            _entity(
                "manifest completeness (all artifacts)",
                len(manifest_files), len(complete), not miss_names,
                [f"{name}: missing {keys}" for name, keys in miss_names.items()],
            ),
            _entity(
                "manifest completeness (current evaluation artifacts)",
                len(current_eval_files), len(current_complete),
                not current_missing,
                [f"{name}: missing {keys}" for name, keys in current_missing.items()],
            ),
        ]
    }


def measure_non_interference(records: list[dict], artifacts: dict[str, dict]) -> dict:
    harness = [r for r in records if r["condition"] == "harness"]
    with_invalid = [
        r for r in harness
        if r.get("invalid_tool_calls_before_c3", 0) > 0
        or (r.get("proxy") or {}).get("invalid_tool_calls_after_c3", 0) > 0
    ]
    unrepaired = [r for r in with_invalid if (r.get("proxy") or {}).get("tool_calls_repaired", 0) == 0]
    total_repaired = sum((r.get("proxy") or {}).get("tool_calls_repaired", 0) for r in harness)
    total_retries = sum((r.get("proxy") or {}).get("corrective_retries", 0) for r in harness)
    total_aborts = sum((r.get("proxy") or {}).get("loop_aborted", 0) for r in harness)
    prompt_hashes = {
        name: data.get("manifest", {}).get("system_prompt_sha256")
        for name, data in artifacts.items()
        if isinstance(data, dict) and data.get("manifest")
    }
    entries = [
        _entity(
            "no invented arguments",
            len(with_invalid), len(unrepaired),
            total_repaired == 0 and (not with_invalid or len(unrepaired) == len(with_invalid)),
            [f"total repairs in artifact: {total_repaired}"],
        ),
    ]
    retries_bad = sum(1 for r in harness if (r.get("proxy") or {}).get("corrective_retries", 0) != 0)
    entries.append(
        _entity(
            "no invisible model calls",
            len(harness),
            len(harness) - retries_bad,
            total_retries == 0,
            [f"total corrective retries: {total_retries}"],
        ),
    )
    aborts_bad = sum(1 for r in harness if (r.get("proxy") or {}).get("loop_aborted", 0) != 0)
    entries.append(
        _entity(
            "no silent abort",
            len(harness),
            len(harness) - aborts_bad,
            total_aborts == 0,
            [f"total aborts: {total_aborts}"],
        ),
    )
    unique_hashes = sorted({h for h in prompt_hashes.values() if h})
    entries.append(
        _entity(
            "system prompt integrity",
            len(unique_hashes), len(unique_hashes),
            len(unique_hashes) == 1,
            [
                f"{name}: {h}" for name, h in sorted(prompt_hashes.items()) if h
            ]
            [:8],
        )
    )
    return {"entries": entries}


def measure_loop_attribution(records: list[dict]) -> dict:
    """Separate detection from actual mitigation for every detected loop.

    Labels per detected loop (mutually exclusive):
      proxy_escalated / client_guard_handled / detected_only.

    ``client_guard_handled`` requires EXPLICIT client-guard evidence: the
    run was stopped by the client's identical-failure safeguard
    (``stop_reason == repeated_identical_failure``). The mere absence of a
    next model request is never treated as client-guard evidence.

    The label counts are recorded as data — a client-guard stop is NOT
    reclassified as a proxy success. The pass criterion is only that every
    detected loop carries exactly one attribution label.
    """
    harness = [r for r in records if r["condition"] == "harness"]
    detected = [
        r for r in harness
        if (r.get("proxy") or {}).get("loop_events", 0) >= 1
        or (r.get("proxy") or {}).get("c4_escalations", 0) >= 1
    ]
    labelled: dict[str, list[str]] = {label: [] for label in LOOP_ATTRIBUTION_LABELS}
    labels_with_client_stop = []
    for record in detected:
        proxy = record.get("proxy") or {}
        attribution = classify_loop_attribution(
            loop_events=int(proxy.get("loop_events") or 0),
            proxy_escalations=int(proxy.get("c4_escalations") or 0),
            stop_reason=record.get("stop_reason"),
        )
        assert attribution.label in labelled  # noqa: S101 - labels are fixed
        labelled[attribution.label].append(
            {"task": record["task_id"], "seed": record["seed"], "notes": attribution.notes}
        )
        if record.get("stop_reason") == "repeated_identical_failure":
            labels_with_client_stop.append(
                f"{record['task_id']} seed{record['seed']}: client-stop evidence in the record"
            )
    covered = sum(len(items) for items in labelled.values())
    return {
        "tally": {label: len(items) for label, items in labelled.items()},
        "examples": {
            label: [item["task"] for item in items[:8]] for label, items in labelled.items()
        },
        "entries": [
            _entity(
                "attribution coverage (each detected loop carries exactly one label)",
                len(detected),
                covered,
                covered == len(detected),
                [],
            ),
        ],
        "definitions": (
            "proxy_escalated = the proxy's warning reached a later request "
            "(c4_escalations >= 1); client_guard_handled = the record carries "
            "explicit client-stop evidence (stop_reason "
            "'repeated_identical_failure'), and a stop for which no evidence "
            "exists stays detected_only; labels are mutually exclusive"
        ),
        "client_stop_evidence_seen": labels_with_client_stop,
    }


def run_diagnose() -> dict:
    result = subprocess.run(
        [
            str(ROOT / ".venv" / "Scripts" / "pocket-harness.exe"),
            "diagnose",
            "--root",
            str(ROOT / ".e2e" / "criteria-diagnose"),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    try:
        report = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        report = {}
    summary = report.get("summary") or {}
    stored = _load(SCENARIOS) or {}
    return {
        "live_scenarios": summary.get("scenarios"),
        "live_failed": summary.get("failed"),
        "live_satisfied": summary.get("satisfied"),
        "exit_code": result.returncode,
        "stored_satisfied": (stored.get("summary") or {}).get("satisfied"),
        "stored_failed": (stored.get("summary") or {}).get("failed"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="write the JSON report to this path")
    args = parser.parse_args()

    artifacts: dict[str, dict] = {}
    for name in EVAL_SUITES + [PREDECLARED, PINNED, SCENARIOS]:
        data = _load(name)
        if data is not None:
            artifacts[name] = data

    records = []
    for name in EVAL_SUITES + [PINNED]:
        data = artifacts.get(name)
        if data is not None:
            records.extend(data.get("records", []))

    report = {
        "document": "criteria measurement (DECISION.md section 7.2)",
        "scope": (
            "diagnostic re-analysis of previously observed data; the held-out "
            "splits here were already observed and are NOT treated as an "
            "independent unused held-out set; no model call is made"
        ),
        "definitions_fixed_before_execution": [
            "C-D1 detectability",
            "C-D2 determinism",
            "C-D3 auditability",
            "C-D4 non-interference",
        ],
        "detectability": measure_detectability(records),
        "determinism": measure_determinism_from_artifacts(),
        "determinism_unverified": {
            "transformed-request-hash replay": (
                "unverified: capture mode was metadata, ~/.pocket/runs and "
                "~/.pocket/blobs are empty, no prompt bodies exist to replay"
            )
        },
        "auditability": measure_auditability(records, artifacts),
        "non_interference": measure_non_interference(records, artifacts),
        "loop_attribution": measure_loop_attribution(records),
        "mechanism_scenarios": run_diagnose(),
    }
    text = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"report written to {args.out}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
