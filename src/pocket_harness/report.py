"""``pocket report`` — summarize a run from its evidence events.

The report is computed purely from the recorded events. There is no LLM judgment, no
scoring model, and no subjective ranking. The categories are the fixed failure
taxonomy from Plan.md §5.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from pocket_harness.config import Config
from pocket_harness.state import StateStore

#: The fixed failure taxonomy (Plan.md §5). Keys are event names, values are the
#: human-readable category names.
FAILURE_TAXONOMY: dict[str, str] = {
    "request.rejected": "client_invalid_request",
    "request.context_overflow": "context_overflow",
    "upstream.error": "upstream_unavailable",
    "loop.detected": "loop_detected",
}


def build_report(config: Config, run_id: str | None = None) -> dict[str, Any]:
    """Build a report for a run (the newest run when ``run_id`` is ``None``)."""
    store = StateStore(config.state.resolved_root())
    runs = store.list_runs()
    if run_id is None:
        if not runs:
            return {"ok": False, "reason": "no runs were recorded", "runs": []}
        run_id = runs[0]["run_id"]

    events = list(store.iter_events(run_id))
    if not events:
        return {"ok": False, "reason": f"run {run_id} has no events", "run_id": run_id}

    event_counts = Counter(event.get("event", "") for event in events)
    failure_counts: Counter[str] = Counter()
    for event in events:
        name = event.get("event", "")
        if name in FAILURE_TAXONOMY:
            failure_counts[FAILURE_TAXONOMY[name]] += 1
        if name == "toolcall.validated":
            payload = event.get("payload") or {}
            failed = int(payload.get("failed", 0))
            if failed:
                failure_counts["tool_call_invalid"] += failed

    latencies = [
        float((event.get("payload") or {}).get("latency_seconds", 0.0))
        for event in events
        if event.get("event") == "response.completed"
    ]

    rules = Counter()
    token_saved = 0
    for event in events:
        if event.get("event") != "request.transformed":
            continue
        payload = event.get("payload") or {}
        for rule in payload.get("rules", []):
            rules[rule.get("id", "?")] += 1
        token_count = payload.get("token_count") or {}
        before = int(token_count.get("before", 0))
        after = int(token_count.get("after", 0))
        token_saved += max(0, before - after)

    repairs = {"checked": 0, "repaired": 0, "failed": 0}
    for event in events:
        if event.get("event") != "toolcall.validated":
            continue
        payload = event.get("payload") or {}
        repairs["checked"] += int(payload.get("checked", 0))
        repairs["repaired"] += int(payload.get("repaired", 0))
        repairs["failed"] += int(payload.get("failed", 0))

    return {
        "ok": True,
        "run_id": run_id,
        "event_counts": dict(sorted(event_counts.items())),
        "failure_taxonomy": dict(sorted(failure_counts.items())),
        "transformations": dict(sorted(rules.items())),
        "tokens_saved_estimate": token_saved,
        "tool_calls": repairs,
        "latency_seconds": {
            "count": len(latencies),
            "min": round(min(latencies), 4) if latencies else None,
            "max": round(max(latencies), 4) if latencies else None,
            "mean": round(sum(latencies) / len(latencies), 4) if latencies else None,
        },
        "capture": config.logging.capture,
    }
