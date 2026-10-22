"""Deterministic hashing helpers.

All hashes produced by Pocket Harness are SHA-256 over a canonical JSON encoding
(sorted keys, no insignificant whitespace, UTF-8). The same canonical encoder is
used for request bodies, tool-call arguments, and content-addressed blobs so that
a hash is reproducible across processes and platforms.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Encode ``value`` to canonical JSON.

    Canonical form: sorted object keys, no whitespace padding, ``ensure_ascii=False``
    so that non-ASCII text hashes identically to the original bytes.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_fallback,
    )


def _fallback(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def sha256_bytes(data: bytes) -> str:
    """Return the raw hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """Return the raw hex digest of the UTF-8 encoding of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_obj(value: Any) -> str:
    """Return the raw hex digest of the canonical JSON encoding of ``value``."""
    return sha256_text(canonical_json(value))


def content_ref(digest: str) -> str:
    """Return a blob reference string such as ``sha256:ab12…``."""
    return f"sha256:{digest}"
