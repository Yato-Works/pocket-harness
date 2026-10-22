"""Local state storage: SQLite index, JSONL run events, content-addressed blobs.

Layout (Plan.md §8.1)::

    ~/.pocket/state.sqlite
    ~/.pocket/runs/<run-id>/events.jsonl
    ~/.pocket/blobs/<sha256>

The store is deliberately file-based and service-free: the proxy must be usable with
no extra long-running daemon, and every artifact must be inspectable with ordinary
tools.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pocket_harness.hashing import canonical_json, sha256_bytes
from pocket_harness.redaction import redact_obj

#: Schema version of the evidence event records (Plan.md §8.3).
EVENT_SCHEMA_VERSION = 1


class StateStore:
    """Filesystem-backed state for runs, events, and blobs.

    Thread-safe for concurrent requests: the SQLite connection is guarded by a lock
    and each writer appends a complete line to its own JSONL file handle.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            root = Path.home() / ".pocket"
        self.root = Path(root).expanduser()
        self.runs_dir = self.root / "runs"
        self.blobs_dir = self.root / "blobs"
        self.db_path = self.root / "state.sqlite"
        self._lock = threading.Lock()
        self._ensure_dirs()
        self._init_db()

    # ------------------------------------------------------------------ setup

    def _ensure_dirs(self) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)

    def _init_db(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY,
                        started_at REAL NOT NULL,
                        label TEXT,
                        manifest TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        run_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        request_id TEXT NOT NULL,
                        event TEXT NOT NULL,
                        ts REAL NOT NULL,
                        payload TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq)"
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------- runs

    def create_run(
        self, run_id: str, label: str | None = None, manifest: dict[str, Any] | None = None
    ) -> None:
        """Register a new run with its manifest."""
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO runs (run_id, started_at, label, manifest) VALUES (?, ?, ?, ?)",
                    (run_id, time.time(), label, canonical_json(manifest or {})),
                )
                conn.commit()
            finally:
                conn.close()
        (self.runs_dir / run_id).mkdir(parents=True, exist_ok=True)

    def list_runs(self) -> list[dict[str, Any]]:
        """Return all known runs, newest first."""
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            try:
                rows = conn.execute(
                    "SELECT run_id, started_at, label, manifest FROM runs ORDER BY started_at DESC"
                ).fetchall()
            finally:
                conn.close()
        out = []
        for run_id, started_at, label, manifest in rows:
            out.append(
                {
                    "run_id": run_id,
                    "started_at": started_at,
                    "label": label,
                    "manifest": json.loads(manifest) if manifest else {},
                }
            )
        return out


    # ----------------------------------------------------------------- events

    def events_path(self, run_id: str) -> Path:
        """Return the JSONL path for a run."""
        return self.runs_dir / run_id / "events.jsonl"

    def append_event(
        self, run_id: str, request_id: str, event: str, payload: dict[str, Any]
    ) -> int:
        """Append one evidence event and return its sequence number."""
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = ?", (run_id,)
                ).fetchone()
                seq = int(row[0]) + 1
                record = {
                    "schema": EVENT_SCHEMA_VERSION,
                    "ts": time.time(),
                    "run_id": run_id,
                    "seq": seq,
                    "request_id": request_id,
                    "event": event,
                    "payload": payload,
                }
                conn.execute(
                    "INSERT INTO events (run_id, seq, request_id, event, ts, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, seq, request_id, event, record["ts"], canonical_json(payload)),
                )
                conn.commit()
            finally:
                conn.close()
        path = self.events_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
        return seq

    def iter_events(self, run_id: str) -> Iterator[dict[str, Any]]:
        """Yield the events of a run in order."""
        path = self.events_path(run_id)
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    # ------------------------------------------------------------------ blobs

    def put_blob(self, data: bytes) -> str:
        """Store ``data`` content-addressed and return ``sha256:<digest>``."""
        digest = sha256_bytes(data)
        path = self.blobs_dir / digest
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(digest + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return f"sha256:{digest}"

    def put_json_blob(self, value: Any) -> str:
        """Store a JSON value content-addressed and return its reference."""
        return self.put_blob(canonical_json(value).encode("utf-8"))

    def get_blob(self, ref: str) -> bytes | None:
        """Return the bytes of a blob reference, or ``None`` if unknown."""
        digest = ref.split(":", 1)[-1]
        path = self.blobs_dir / digest
        if not path.is_file():
            return None
        return path.read_bytes()

    def has_blob(self, ref: str) -> bool:
        """Return whether a blob reference exists locally."""
        digest = ref.split(":", 1)[-1]
        return (self.blobs_dir / digest).is_file()

    # --------------------------------------------------------------- purging

    def purge_run(self, run_id: str) -> None:
        """Delete all stored data for a run."""
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            try:
                conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
                conn.commit()
            finally:
                conn.close()
        shutil.rmtree(self.runs_dir / run_id, ignore_errors=True)

    def purge_all(self) -> None:
        """Delete every stored run and blob."""
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            try:
                conn.execute("DELETE FROM events")
                conn.execute("DELETE FROM runs")
                conn.commit()
            finally:
                conn.close()
        shutil.rmtree(self.runs_dir, ignore_errors=True)
        shutil.rmtree(self.blobs_dir, ignore_errors=True)
        self._ensure_dirs()


def redact_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an event payload with secrets redacted."""
    return redact_obj(payload)
