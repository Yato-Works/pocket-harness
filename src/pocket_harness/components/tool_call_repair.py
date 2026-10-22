"""C3 — ToolCallRepair.

Validates a completed tool call against the tool schema supplied by the **client**
and repairs only structural defects that have a clearly defined, deterministic
correction (Plan.md §6 C3).

Repair order:

1. Parse JSON with narrowly permitted tolerance (code fences, trailing commas).
   Never ``eval`` and never execute model output.
2. Resolve the tool name by exact match, then case-insensitive match, then an explicit
   alias table. Fuzzy matching is off by default.
3. Apply only lossless, unambiguous schema conversions. Never guess missing paths,
   commands, enum values, or business meaning.
4. Validate against the original JSON Schema; record the original and repaired hashes
   plus a structured diff.
5. For destructive tools, permit syntax-level repair only — no fuzzy name matching
   and no argument value changes.
6. If local repair fails, the caller may issue one explicit corrective inference
   retry (bounded by the retry budget). That is a normal model request, counted in
   the accounting — not a hidden judge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from pocket_harness.hashing import hash_obj
from pocket_harness.redaction import redact_text

_FENCE_RE = re.compile(r"^\s*```(?:json|javascript|js)?\s*\n(?P<body>.*?)\n?\s*```\s*$", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
#: A single-quoted string literal without an embedded double quote.
_SINGLE_QUOTED_RE = re.compile(r"'([^'\"\\]*(?:\\.[^'\"\\]*)*)'")


def _normalize_single_quotes(text: str) -> str:
    """Convert Python-style single-quoted strings to JSON double-quoted strings.

    This is a *syntactic* transformation only: the literal content is preserved
    verbatim, and it is only applied when the text contains no double-quoted string
    (so there is no risk of mixing the two conventions inside one document).
    """
    if "'" not in text or '"' in text:
        return text
    return _SINGLE_QUOTED_RE.sub(lambda m: json.dumps(m.group(1)), text)


class RepairFailure(Exception):
    """Raised when a tool call cannot be repaired locally."""


@dataclass(slots=True)
class RepairOutcome:
    """The result of validating/repairing one tool call.

    ``original_hash`` and ``repaired_hash`` are computed over the *same* namespace
    (the canonical tool name plus the parsed arguments), so that an unmodified call
    has two equal hashes. ``original_text_hash`` identifies the raw text exactly as
    the model emitted it, which is what the audit trail needs when a parse failed.
    """

    ok: bool
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    original_arguments_text: str = ""
    repaired_arguments_text: str = ""
    original_hash: str = ""
    repaired_hash: str = ""
    original_text_hash: str = ""
    changed: bool = False
    #: True when the *text* of the arguments changed, even if the parsed
    #: arguments are the same (a re-serialisation after tolerant parsing).
    #: This is the flag that decides whether the repaired text is applied to
    #: the tool call the client receives (ADR 0001).
    text_changed: bool = False
    reasons: list[str] = field(default_factory=list)
    diff: dict[str, Any] = field(default_factory=dict)
    error_class: str | None = None

    def as_event_dict(self) -> dict[str, Any]:
        """Return a redacted, structured description for the evidence log."""
        return {
            "ok": self.ok,
            "tool": self.tool_name,
            "changed": self.changed,
            "text_changed": self.text_changed,
            "original_sha256": self.original_hash,
            "repaired_sha256": self.repaired_hash,
            "original_text_sha256": self.original_text_hash,
            "reasons": list(self.reasons),
            "diff": self.diff,
            "error_class": self.error_class,
        }


def parse_arguments(text: str) -> dict[str, Any]:
    """Parse a tool-call argument string into a mapping.

    Raises :class:`RepairFailure` when the text is not valid JSON. The tolerance is
    limited to code fences and trailing commas; nothing is ever evaluated.
    """
    if text is None:
        return {}
    stripped = text.strip()
    if not stripped:
        return {}
    match = _FENCE_RE.match(stripped)
    if match:
        stripped = match.group("body").strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        repaired = _TRAILING_COMMA_RE.sub(r"\1", _normalize_single_quotes(stripped))
        try:
            value = json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise RepairFailure(f"invalid JSON arguments: {exc.msg} (line {exc.lineno})") from exc
    if not isinstance(value, dict):
        raise RepairFailure(f"tool arguments must be a JSON object, got {type(value).__name__}")
    return value


def _string_values(schema: dict[str, Any]) -> list[str]:
    """Return the allowed string values of an enum schema, if any."""
    if not isinstance(schema, dict):
        return []
    values = schema.get("enum")
    if isinstance(values, list) and all(isinstance(v, str) for v in values):
        return list(values)
    return []


def _similar(a: str, b: str) -> bool:
    """Return whether two names are close enough for an experimental fuzzy match."""
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.85


class ToolCallRepairer:
    """Validates and conservatively repairs tool calls."""

    def __init__(
        self,
        tool_schemas: dict[str, dict[str, Any]],
        aliases: dict[str, str] | None = None,
        allow_fuzzy_tool_name: bool = False,
        allow_schema_defaults: bool = False,
        destructive_tools: set[str] | None = None,
    ) -> None:
        self.tool_schemas = dict(tool_schemas)
        self.aliases = dict(aliases or {})
        self.allow_fuzzy_tool_name = allow_fuzzy_tool_name
        self.allow_schema_defaults = allow_schema_defaults
        self.destructive_tools = set(destructive_tools or ())
        self._validators: dict[str, Draft202012Validator] = {
            name: Draft202012Validator(schema or {})
            for name, schema in self.tool_schemas.items()
        }

    # ------------------------------------------------------- name resolution

    def resolve_tool_name(self, name: str) -> tuple[str | None, str]:
        """Resolve a model-provided tool name to a canonical client tool name.

        Returns ``(canonical_name_or_None, reason)``.
        """
        if not name:
            return None, "empty tool name"
        if name in self.tool_schemas:
            return name, "exact match"

        alias = self.aliases.get(name)
        if alias and alias in self.tool_schemas:
            return alias, f"alias table ({name} -> {alias})"

        lowered = {k.lower(): k for k in self.tool_schemas}
        if name.lower() in lowered:
            return lowered[name.lower()], f"case-insensitive match ({name} -> {lowered[name.lower()]})"

        if self.allow_fuzzy_tool_name and name.lower() not in self.destructive_tools:
            candidates = [k for k in self.tool_schemas if _similar(name, k)]
            if len(candidates) == 1:
                candidate = candidates[0]
                if candidate in self.destructive_tools:
                    return None, "fuzzy match rejected for destructive tool"
                return candidate, f"fuzzy match (experimental) ({name} -> {candidate})"

        return None, f"no tool named {name!r} in the client tool list"

    # ------------------------------------------------------------- validation

    def _validate(self, tool_name: str, arguments: dict[str, Any]) -> list[str]:
        validator = self._validators.get(tool_name)
        if validator is None:
            return [f"no schema for tool {tool_name!r}"]
        errors = []
        for error in sorted(validator.iter_errors(arguments), key=lambda e: list(e.path)):
            location = ".".join(str(p) for p in error.path) or "<root>"
            errors.append(f"{location}: {error.message}")
        return errors

    # ---------------------------------------------------------------- repair

    def repair(self, tool_name: str, arguments_text: str) -> RepairOutcome:
        """Validate and, if possible, repair one tool call."""
        original_text = arguments_text or ""
        outcome = RepairOutcome(
            ok=False,
            tool_name=tool_name,
            original_arguments_text=original_text,
            original_text_hash=hash_obj(original_text),
        )

        # 1) parse
        # The strict parse tells us whether the text the model produced was
        # already usable. Only a call that was *not* usable is a repair: a
        # re-serialisation of already valid JSON changes nothing for the client.
        try:
            strict_arguments = json.loads(original_text.strip()) if original_text.strip() else {}
        except json.JSONDecodeError:
            strict_arguments = None
        try:
            arguments = parse_arguments(original_text)
        except RepairFailure as exc:
            outcome.error_class = "M.parse"
            outcome.reasons.append(str(exc))
            outcome.original_hash = hash_obj({"name": tool_name, "arguments": original_text})
            outcome.repaired_hash = outcome.original_hash
            return outcome
        if original_text.strip() != json.dumps(arguments, ensure_ascii=False, sort_keys=True):
            outcome.reasons.append("arguments were re-serialized after tolerant JSON parsing")

        # 2) resolve the tool name
        canonical, reason = self.resolve_tool_name(tool_name)
        if canonical is None:
            outcome.error_class = "M.unknown_tool"
            outcome.reasons.append(reason)
            outcome.original_hash = hash_obj({"name": tool_name, "arguments": arguments})
            outcome.repaired_hash = outcome.original_hash
            return outcome
        if canonical != tool_name:
            outcome.reasons.append(f"tool name resolved: {reason}")
        outcome.tool_name = canonical

        is_destructive = canonical in self.destructive_tools

        # The original hash is always computed over the *original* arguments, so that
        # the audit trail can compare it with the repaired hash.
        outcome.original_hash = hash_obj({"name": canonical, "arguments": arguments})

        # 3) lossless schema conversions
        if is_destructive:
            # A destructive tool is repaired at the *syntax* level only: the JSON is
            # parsed, but no argument value is changed in any way. The call is then
            # validated against the schema exactly as the model wrote it.
            outcome.reasons.append(
                "destructive tool: only syntax-level repair was applied"
            )
        else:
            arguments = self._apply_lossless_conversions(
                canonical, arguments, outcome, allow_defaults=True
            )

        # 4) validate against the original schema
        errors = self._validate(canonical, arguments)
        if errors:
            outcome.error_class = "M.schema_invalid"
            outcome.reasons.extend(errors)
            outcome.repaired_hash = hash_obj({"name": canonical, "arguments": arguments})
            outcome.diff = self._build_diff(arguments, original_text)
            return outcome

        outcome.ok = True
        outcome.arguments = arguments
        outcome.repaired_arguments_text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        outcome.repaired_hash = hash_obj({"name": canonical, "arguments": arguments})
        outcome.changed = outcome.repaired_hash != outcome.original_hash
        # A repair is only *applied* to the client's tool call when the original
        # was not usable as it stood (ADR 0001). A pure re-serialisation of
        # already valid JSON is not a repair and must not be counted as one.
        original_was_usable = (
            isinstance(strict_arguments, dict)
            and not self._validate(canonical, strict_arguments)
        )
        outcome.text_changed = (
            outcome.repaired_arguments_text != outcome.original_arguments_text
            and not original_was_usable
        )
        if not original_was_usable and not outcome.text_changed:
            outcome.reasons.append("the original arguments were already usable")
        outcome.diff = self._build_diff(arguments, original_text)
        return outcome

    def _apply_lossless_conversions(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        outcome: RepairOutcome,
        allow_defaults: bool,
    ) -> dict[str, Any]:
        """Apply only lossless, unambiguous conversions."""
        schema = self.tool_schemas.get(tool_name) or {}
        properties = schema.get("properties") or {}
        converted = dict(arguments)

        for key, value in list(converted.items()):
            prop = properties.get(key)
            if not isinstance(prop, dict):
                continue
            expected = prop.get("type")
            if expected == "integer" and isinstance(value, str):
                try:
                    converted[key] = int(value.strip())
                    outcome.reasons.append(f"argument {key!r}: string -> integer (lossless)")
                    continue
                except ValueError:
                    pass
            if expected == "number" and isinstance(value, str):
                text = value.strip()
                try:
                    number = float(text)
                except ValueError:
                    number = None
                if number is not None and text == str(number):
                    converted[key] = number
                    outcome.reasons.append(f"argument {key!r}: string -> number (lossless)")
                    continue
            if expected == "boolean" and isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"true", "false"}:
                    converted[key] = lowered == "true"
                    outcome.reasons.append(f"argument {key!r}: string -> boolean (lossless)")
                    continue
            if expected == "array" and isinstance(value, str):
                stripped = value.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    try:
                        parsed = json.loads(stripped)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, list):
                        converted[key] = parsed
                        outcome.reasons.append(f"argument {key!r}: JSON string -> array (lossless)")
                        continue
            allowed = _string_values(prop)
            if allowed and isinstance(value, str) and value not in allowed:
                match = next((a for a in allowed if a.lower() == value.lower()), None)
                if match is not None:
                    converted[key] = match
                    outcome.reasons.append(f"argument {key!r}: enum case normalized (lossless)")
                    continue

        if allow_defaults and self.allow_schema_defaults:
            for key, prop in properties.items():
                if key in converted or not isinstance(prop, dict):
                    continue
                if "default" in prop:
                    converted[key] = prop["default"]
                    outcome.reasons.append(f"argument {key!r}: inserted explicit schema default")

        return converted

    def _build_diff(self, arguments: dict[str, Any], original_text: str) -> dict[str, Any]:
        """Return a redacted structured diff between the original and repaired call."""
        try:
            original = parse_arguments(original_text)
        except RepairFailure:
            original = {}
        changed = {}
        for key in sorted(set(original) | set(arguments)):
            before = original.get(key)
            after = arguments.get(key)
            if before != after:
                changed[key] = {
                    "before": redact_text(str(before)),
                    "after": redact_text(str(after)),
                }
        return {"changed_keys": changed}
