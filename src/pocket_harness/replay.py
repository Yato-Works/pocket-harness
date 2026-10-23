"""``pocket replay`` — prove that a transformation is reproducible.

Replay re-applies the C0/C1/C2/C6 pipeline to a captured request payload and compares
the resulting request hash with the hash that was recorded at capture time.

The comparison is only possible when the request body was captured. With the default
``capture = "metadata"`` the proxy stores hashes and rule ids but not the payload
itself, so replay reports that the body is unavailable rather than pretending to
verify anything. With ``capture = "local-replay"`` the payload is stored in the local
content-addressed blob store and can be replayed exactly.

The body is found automatically from the run's ``request.captured`` events. An explicit
``--capture-file`` overrides that.

Two independent things are checked, and the report keeps them separate:

* the *transformation* hash, which shows the pipeline produced the same request, and
* the *configuration* hash, which shows the replay ran with the settings that were
  recorded when the run started.

A reproduction claim needs both.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pocket_harness.config import Config
from pocket_harness.pipeline import Pipeline, recent_context, recent_tool_uses
from pocket_harness.state import StateStore
from pocket_harness.version import PIPELINE_VERSION


def _load_capture(path: Path, config: Config) -> tuple[dict[str, Any] | None, str | None]:
    """Load a captured payload from a file or a blob reference."""
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except json.JSONDecodeError as exc:
            return None, f"the capture file is not valid JSON: {exc}"
    store = StateStore(config.state.resolved_root())
    raw = store.get_blob(str(path))
    if raw is None:
        return None, f"no capture found at {path}"
    try:
        return json.loads(raw.decode("utf-8")), None
    except json.JSONDecodeError as exc:
        return None, f"the captured blob is not valid JSON: {exc}"


def _find_captured_blob(store: StateStore, run_id: str) -> str | None:
    """Return the blob reference of the first captured request body in a run.

    With ``capture = "local-replay"`` the proxy records a ``request.captured`` event
    for every request. The first one is used, because the pipeline is deterministic
    per request and the first request is the one the run is normally replayed for.
    """
    for event in store.iter_events(run_id):
        if event.get("event") != "request.captured":
            continue
        payload = event.get("payload") or {}
        ref = payload.get("blob_ref")
        if isinstance(ref, str) and ref:
            return ref
    return None


def _recorded_config_hash(store: StateStore, run_id: str) -> str | None:
    """Return the configuration hash that was recorded when the run started."""
    for run in store.list_runs():
        if run.get("run_id") == run_id:
            manifest = run.get("manifest") or {}
            value = manifest.get("config_hash")
            return value if isinstance(value, str) else None
    return None


def run_replay(
    config: Config,
    run_path: Path,
    capture_ref: str | None = None,
) -> dict[str, Any]:
    """Replay a captured request and compare the hashes."""
    store = StateStore(config.state.resolved_root())
    run_id = run_path.name if run_path.is_dir() else str(run_path)

    recorded: list[dict[str, Any]] = []
    for event in store.iter_events(run_id):
        if event.get("event") == "request.transformed":
            payload = dict(event.get("payload") or {})
            # Keep the identity of the request so a mismatch can be located in
            # the event log instead of being reported without a context.
            payload["_request_id"] = event.get("request_id")
            payload["_seq"] = event.get("seq")
            recorded.append(payload)

    if not recorded:
        return {
            "ok": False,
            "run_id": run_id,
            "reason": "the run has no recorded request.transformed events",
        }

    # An explicit ``--capture-file`` always wins. Otherwise fall back to the body
    # that the proxy captured locally, if the run has one.
    source = capture_ref
    if not source:
        found = _find_captured_blob(store, run_id)
        if found:
            source = found

    if not source:
        return {
            "ok": False,
            "run_id": run_id,
            "reason": (
                "no request body was captured, so the transformation cannot be replayed. "
                "Re-run with --capture local-replay, or pass --capture-file <path>."
            ),
            "recorded_input_hashes": [r.get("input_sha256") for r in recorded],
            "recorded_output_hashes": [r.get("output_sha256") for r in recorded],
            "hint": (
                "with capture=metadata the proxy stores hashes and rule ids only; "
                "the payload itself is not retained"
            ),
        }

    payload, error = _load_capture(Path(source), config)
    if payload is None:
        return {"ok": False, "run_id": run_id, "reason": error}

    if isinstance(payload, dict) and "payload" in payload:
        payload = payload["payload"]

    # The recorded transformation produced C2 reference stubs that carry a
    # ``content_ref`` when the proxy had a blob store (create_app attaches one
    # always). The replay must therefore reproduce the stub with the same
    # content-addressed reference, or the output hash comparison would fail on
    # every request that dedupe touched. ``put_json_blob`` is idempotent, so
    # reproducing the reference does not rewrite existing blobs.
    pipeline = Pipeline(config, blob_store=store.put_json_blob)
    canonical, _ = pipeline.normalize(payload)
    result = pipeline.transform(
        canonical,
        recent_tool_uses=recent_tool_uses(canonical),
        recent_context=recent_context(canonical),
    )

    expected = recorded[0].get("output_sha256")
    matched = result.output_hash == expected
    input_matched = result.input_hash == recorded[0].get("input_sha256")

    # The configuration hash proves the replay used the same settings. A mismatch
    # does not by itself mean the transformation was wrong, but it does mean the
    # reproduction claim is not established, so it is surfaced explicitly.
    #
    # Three states are distinguished:
    #   True   the recorded hash equals the current one — the settings match.
    #   False  the recorded hash differs — the settings changed.
    #   None   no hash was recorded, so the settings could not be compared at all.
    # ``ok`` is only true when the transformation reproduced *and* the settings
    # were not shown to have changed; the unverifiable case stays visible through
    # ``config_matched: null`` and a warning.
    current_config_hash = config.config_hash()
    recorded_config_hash = _recorded_config_hash(store, run_id)
    if recorded_config_hash is None:
        config_matched: bool | None = None
        config_note = (
            "the run manifest did not record a configuration hash, so the settings "
            "could not be compared"
        )
    elif recorded_config_hash == current_config_hash:
        config_matched = True
        config_note = ""
    else:
        config_matched = False
        config_note = (
            "the configuration changed since the run was recorded; the hash "
            "comparison above was made with different settings"
        )

    warnings: list[str] = []
    if config_note:
        warnings.append(config_note)

    return {
        "ok": bool(matched) and config_matched is not False,
        "run_id": run_id,
        "pipeline_version": PIPELINE_VERSION,
        "config_hash": current_config_hash,
        "recorded_config_hash": recorded_config_hash,
        "config_matched": config_matched,
        "capture_source": source,
        "compared_request_index": 0,
        "compared_request_id": recorded[0].get("_request_id"),
        "compared_request_seq": recorded[0].get("_seq"),
        "recorded_request_count": len(recorded),
        "replayed_input_sha256": result.input_hash,
        "replayed_output_sha256": result.output_hash,
        "recorded_input_sha256": recorded[0].get("input_sha256"),
        "recorded_output_sha256": expected,
        "input_matched": input_matched,
        "matched": matched,
        "rules": [rule.get("id") for rule in result.rules],
        "warnings": warnings,
    }
