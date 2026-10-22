"""Best-effort redaction of secrets and authorization headers.

Plan.md §8.2: "Redaction is best-effort and must not be presented as a guarantee that
arbitrary secrets can always be detected." The helpers here therefore *never* claim to be
exhaustive; they only ensure that the well-known credential carriers (authorization
headers, API keys, provider-specific key shapes) are not written to logs by accident.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

#: Header names whose values are always replaced before logging.
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "openai-api-key",
        "anthropic-api-key",
        "cookie",
        "set-cookie",
        "x-auth-token",
    }
)

#: Header *names* that hint that the value is a bearer token even if the header is
#: not in :data:`SENSITIVE_HEADERS`.
_BEARERISH = re.compile(r"(authorization|api[-_]?key|secret|token|password)", re.IGNORECASE)

_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), REDACTED),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
)

#: JSON-ish or query-ish key/value pairs inside free text.
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[-_]?key|apikey|access[-_]?token|auth[-_]?token|bearer|password|secret)\b"
    r"(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[A-Za-z0-9_\-\.]{8,})"
)


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Return a copy of ``headers`` with sensitive values replaced."""
    if not headers:
        return {}
    out: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in SENSITIVE_HEADERS:
            out[name] = REDACTED
        elif _BEARERISH.search(name) and value.lower().startswith(("bearer ", "basic ")):
            out[name] = REDACTED
        else:
            out[name] = redact_text(value)
    return out


def redact_text(text: str) -> str:
    """Return ``text`` with well-known secret shapes replaced by ``[REDACTED]``."""
    if not text:
        return text
    result = text
    for pattern, replacement in _VALUE_PATTERNS:
        result = pattern.sub(replacement, result)
    result = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", result)
    return result


def redact_obj(value: Any) -> Any:
    """Recursively redact secrets inside a JSON-compatible structure."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_HEADERS:
                out[key] = REDACTED
            elif isinstance(key, str) and _BEARERISH.search(key):
                out[key] = redact_text(item) if isinstance(item, str) else redact_obj(item)
            else:
                out[key] = redact_obj(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_obj(item) for item in value]
    return value
