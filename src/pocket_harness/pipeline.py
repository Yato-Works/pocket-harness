"""The deterministic transformation pipeline (C0 → C1 → C2 → C6).

Everything in this module is a pure function of (input, configuration, version). No
network call, no LLM, and no wall-clock-dependent decision influences the transformed
request — which is what makes ``pocket replay`` able to reproduce a request hash.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pocket_harness.canonical import CanonicalRequest
from pocket_harness.components.context_budgeter import BudgetResult, ContextBudgeter
from pocket_harness.components.loop_guard import LoopGuard, detect_from_history
from pocket_harness.components.router import ResolvedRoute, Router
from pocket_harness.components.tool_selector import ToolSelection, ToolSelector
from pocket_harness.config import Config
from pocket_harness.hashing import hash_obj
from pocket_harness.tokens import TokenCount, TokenCounter


class UnsupportedFieldError(Exception):
    """Raised when a request contains a field the upstream cannot accept."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


@dataclass(slots=True)
class TransformResult:
    """The result of one pipeline run."""

    request: CanonicalRequest
    original: CanonicalRequest
    input_hash: str
    output_hash: str
    token_before: TokenCount
    token_after: TokenCount
    tool_selection: ToolSelection | None = None
    budget: BudgetResult | None = None
    route: ResolvedRoute | None = None
    rules: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fields_dropped: list[dict[str, Any]] = field(default_factory=list)

    def as_event_dict(self) -> dict[str, Any]:
        return {
            "input_sha256": self.input_hash,
            "output_sha256": self.output_hash,
            "token_count": {
                "before": self.token_before.value,
                "after": self.token_after.value,
                "source": self.token_after.source.value,
                "method": self.token_after.method,
            },
            "rules": list(self.rules),
            "warnings": list(self.warnings),
            "dropped_fields": list(self.fields_dropped),
        }



class Pipeline:
    """Applies C1, C2, and C6 to a canonical request."""

    def __init__(
        self,
        config: Config,
        counter: TokenCounter | None = None,
        blob_store: Callable[[Any], str] | None = None,
    ) -> None:
        self.config = config
        self.counter = counter or TokenCounter()
        self.blob_store = blob_store

        self.selector = ToolSelector(
            max_non_pinned=config.tools.max_non_pinned,
            sticky_unused_turns=config.tools.sticky_unused_turns,
            max_description_chars=config.tools.max_description_chars,
            pinned_tools=config.client.pinned_tools,
            aliases=config.tools.tool_aliases,
        )
        self.router = Router(config)
        self._loop_guard = LoopGuard(
            repeated_call_threshold=config.loop_guard.repeated_call_threshold,
            max_steps_per_task=config.loop_guard.max_steps_per_task,
            max_wall_seconds=config.loop_guard.max_wall_seconds,
            detect_oscillation=config.loop_guard.detect_oscillation,
        )
        self._budgeter = ContextBudgeter(
            context_length=config.effective_context_length(),
            start_at=config.context.start_at,
            target_at=config.context.target_at,
            keep_recent_turns=config.context.keep_recent_turns,
            reserved_output_tokens=config.context.reserved_output_tokens,
            safety_margin_tokens=config.context.safety_margin_tokens,
            max_tool_output_bytes=config.context.max_tool_output_bytes,
            max_request_tokens=config.context.max_request_tokens,
            counter=self.counter,
            blob_store=blob_store,
        )

    # ------------------------------------------------------------------ C0

    def normalize(self, payload: dict[str, Any]) -> tuple[CanonicalRequest, list[dict[str, Any]]]:
        """Normalize a client payload, reporting any dropped/translated fields."""
        request = CanonicalRequest.from_payload(payload)
        dropped: list[dict[str, Any]] = []

        unsupported = set(self.config.upstream.unsupported_fields)
        optional = set(self.config.upstream.optional_fields)
        policy = "omit-and-warn" if unsupported and optional <= unsupported else "error"

        for name in list(request.extra_fields):
            if name in unsupported:
                if name in optional:
                    dropped.append({"field": name, "reason": "unsupported but optional", "policy": policy})
                    del request.extra_fields[name]
                else:
                    raise UnsupportedFieldError(
                        f"field {name!r} is not supported by the configured upstream",
                        field=name,
                    )

        for name in ("parallel_tool_calls", "response_format"):
            value = getattr(request, name, None)
            if value is not None and name in unsupported and name in optional:
                setattr(request, name, None)
                dropped.append({"field": name, "reason": "unsupported but optional", "policy": policy})

        return request, dropped

    # ----------------------------------------------------------------- run

    def transform(
        self,
        request: CanonicalRequest,
        recent_tool_uses: list[str] | None = None,
        recent_context: str = "",
        sticky_tools: list[str] | None = None,
    ) -> TransformResult:
        """Run the full transformation pipeline over a canonical request."""
        input_hash = hash_obj(request.to_payload())
        rules: list[dict[str, Any]] = []
        warnings: list[str] = []

        current = request

        # --- C1: tool selection ---------------------------------------------
        selection = self.selector.select(
            current,
            recent_tool_uses=recent_tool_uses or [],
            recent_context=recent_context,
            sticky_tools=sticky_tools or [],
        )
        if selection.hidden:
            current = _with_tools(current, selection.exposed)
            rules.append(
                {
                    "id": "C1.select",
                    "reason": "reduced the exposed tool set",
                    "exposed": selection.exposed_names,
                    "hidden": selection.hidden,
                }
            )
        warnings.extend(selection.warnings)

        # --- C4: loop guard --------------------------------------------------
        # The detection is derived from the request history, so the escalation is
        # a deterministic part of the transformation and a replay reproduces it
        # (ADR 0002). It runs before C2 so that the context budgeter accounts
        # for the message the guard adds.
        loop_events = detect_from_history(
            current.messages,
            repeated_call_threshold=self.config.loop_guard.repeated_call_threshold,
            detect_oscillation=self.config.loop_guard.detect_oscillation,
        )
        if loop_events:
            warning = self._loop_guard.warning_text(loop_events)
            current = _with_appended_message(current, warning)
            rules.append(
                {
                    "id": "C4.repeat_warning",
                    "reason": "the request history shows a repeated tool call",
                    "signals": [event.signal.value for event in loop_events],
                }
            )

        # --- C2: context budget ---------------------------------------------
        budget = self._budgeter.apply(current)
        current = budget.request
        rules.extend(budget.as_rule_dicts())
        warnings.extend(budget.warnings)

        # --- C6: route resolution -------------------------------------------
        route = self.router.resolve(current.model)

        output_hash = hash_obj(current.to_payload())
        return TransformResult(
            request=current,
            original=request,
            input_hash=input_hash,
            output_hash=output_hash,
            token_before=budget.token_before,
            token_after=budget.token_after,
            tool_selection=selection,
            budget=budget,
            route=route,
            rules=rules,
            warnings=warnings,
        )


def _with_tools(request: CanonicalRequest, tools: list[Any]) -> CanonicalRequest:
    """Return a copy of ``request`` with a different tool list."""
    data = request.model_dump()
    data["tools"] = tools
    return CanonicalRequest(**data)


def _with_appended_message(request: CanonicalRequest, content: str) -> CanonicalRequest:
    """Return a copy of ``request`` with one deterministic message appended.

    The message is a ``user`` message: the system prompt stays untouched
    (Plan.md §6 C2 invariant) and the tool-call/result pairing of the
    conversation is not disturbed.
    """
    data = request.model_dump()
    messages = list(data.get("messages") or [])
    messages.append({"role": "user", "content": content})
    data["messages"] = messages
    return CanonicalRequest(**data)


def recent_tool_uses(request: CanonicalRequest) -> list[str]:
    """Return the tool names used in the request's messages, oldest first.

    This is a pure function of the request, so ``pocket replay`` can recompute it
    from a captured payload and obtain the same C1 input as the live proxy.
    """
    names: list[str] = []
    for message in request.messages:
        for call in message.tool_calls or []:
            names.append(call.name)
    return names


def recent_context(request: CanonicalRequest, window: int = 6) -> str:
    """Return the recent conversation text used for tool relevance scoring.

    Like :func:`recent_tool_uses`, this is a pure function of the request so that a
    replay reproduces the live C1 selection exactly.
    """
    parts: list[str] = []
    for message in request.messages[-window:]:
        content = message.content
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)
