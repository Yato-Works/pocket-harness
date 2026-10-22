"""The Pocket Harness proxy server.

Endpoints:

* ``POST /v1/chat/completions`` — the OpenAI-compatible endpoint a client agent uses.
* ``GET  /v1/models`` — lists the configured upstream model.
* ``GET  /healthz`` — a liveness probe.
* ``GET  /pocket/status`` — a deterministic status report for the current run.
* ``POST /pocket/flush`` — force-flush the current run's events to disk.

The proxy is a *proxy*, not an agent: it transforms the request, forwards it, and
reports what it did. It never fabricates a model response, never silently falls back
to another model, and never hides an upstream failure.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from pocket_harness.canonical import CanonicalRequest
from pocket_harness.components.context_budgeter import ContextOverflowError
from pocket_harness.components.loop_guard import LoopGuard, LoopState
from pocket_harness.components.tool_call_repair import ToolCallRepairer
from pocket_harness.config import Config
from pocket_harness.hashing import canonical_json
from pocket_harness.ledger import ExecutionLedger
from pocket_harness.pipeline import Pipeline, UnsupportedFieldError, recent_context, recent_tool_uses
from pocket_harness.redaction import redact_headers
from pocket_harness.state import StateStore
from pocket_harness.tokens import TokenCounter
from pocket_harness.toolcall_text import describe_text_tool_call
from pocket_harness.upstream import StreamCollector, UpstreamClient, UpstreamError, UpstreamTarget
from pocket_harness.version import PIPELINE_VERSION


class ProxyService:
    """Holds the state shared by all proxy endpoints."""

    def __init__(self, config: Config, store: StateStore | None = None) -> None:
        self.config = config
        self.store = store or StateStore(config.state.resolved_root())
        self.counter = TokenCounter()
        self.pipeline = Pipeline(config, counter=self.counter, blob_store=self.store.put_json_blob)
        self.loop_guard = LoopGuard(
            repeated_call_threshold=config.loop_guard.repeated_call_threshold,
            max_steps_per_task=config.loop_guard.max_steps_per_task,
            max_wall_seconds=config.loop_guard.max_wall_seconds,
            detect_oscillation=config.loop_guard.detect_oscillation,
        )
        self.run_id = f"run-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self.store.create_run(
            self.run_id,
            label="proxy",
            manifest={
                "pipeline_version": PIPELINE_VERSION,
                "config_hash": config.config_hash(),
                "upstream": config.upstream.base_url,
                "model": config.upstream.model,
                "capture": config.logging.capture,
            },
        )
        self.loop_state = LoopState()
        self.ledger = ExecutionLedger()
        self.request_counter = 0
        self.sticky_tools: list[str] = []
        self.recent_tool_uses: list[str] = []
        self.retry_budget_used = 0
        #: Optional transport override, used by the tests to avoid a real upstream.
        self.transport: Any = None

    # ---------------------------------------------------------------- events

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        """Append an evidence event to the run log."""
        self.store.append_event(self.run_id, self.current_request_id, event, payload)

    current_request_id: str = "-"

    def next_request_id(self) -> str:
        """Return a new request identifier."""
        self.request_counter += 1
        return f"req-{self.request_counter:06d}"

    # ------------------------------------------------------------- upstream

    def target_for(self, model: str) -> UpstreamTarget:
        """Return the upstream target for a (possibly routed) model."""
        route = self.pipeline.router.resolve(model)
        return UpstreamTarget(
            base_url=route.base_url,
            model=route.model or self.config.upstream.model,
            api_key=self.config.upstream.resolved_api_key(),
            api_key_env=route.api_key_env,
            extra_headers=self.config.upstream.extra_headers,
        )

    # ---------------------------------------------------------- tool schemas

    def repairer_for(self, request: CanonicalRequest) -> ToolCallRepairer:
        """Return a C3 repairer bound to the **client's original** tool schemas.

        The client tool list is the authority, even when C1 hid some tools from the
        model — otherwise a valid call to a hidden tool would be rejected.
        """
        return ToolCallRepairer(
            tool_schemas=request.tool_schema_map(),
            aliases=self.config.tools.tool_aliases,
            allow_fuzzy_tool_name=self.config.repair.allow_fuzzy_tool_name,
            allow_schema_defaults=self.config.repair.allow_schema_defaults,
            destructive_tools=set(self.config.client.destructive_tools),
        )

    # -------------------------------------------------------------- helpers

    def _recent_context(self, request: CanonicalRequest) -> str:
        """Return the recent conversation text used for tool relevance scoring."""
        return recent_context(request)

    def _tool_uses_in(self, request: CanonicalRequest) -> list[str]:
        """Return the tool names used in the recent messages, newest last."""
        return recent_tool_uses(request)

    def _capture_body(self, request: CanonicalRequest) -> str | None:
        """Store the request body locally when capture mode allows it.

        Returns the blob reference, or ``None`` when nothing was stored. The body is
        stored in its *canonical* form (the exact input C1/C2/C6 received) so that a
        replay re-applies the pipeline to the same bytes.
        """
        if self.config.logging.capture != "local-replay":
            return None
        try:
            return self.store.put_json_blob(request.to_payload())
        except Exception:  # noqa: BLE001 - evidence storage must never break a request
            return None

    # ------------------------------------------------------------- handlers

    async def chat_completions(self, request: Request) -> Response:
        """Handle a chat-completions request from the client agent."""
        request_id = self.next_request_id()
        self.current_request_id = request_id
        started = time.monotonic()

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return self._error("invalid JSON body", 400, "client_invalid_request")

        if not isinstance(payload, dict):
            return self._error("request body must be a JSON object", 400, "client_invalid_request")

        self.emit(
            "request.received",
            {
                "model": payload.get("model"),
                "stream": bool(payload.get("stream")),
                "message_count": len(payload.get("messages") or []),
                "tool_count": len(payload.get("tools") or []),
                "client_headers": redact_headers(dict(request.headers)),
            },
        )

        # --- C0 ------------------------------------------------------------
        try:
            canonical, dropped = self.pipeline.normalize(payload)
        except UnsupportedFieldError as exc:
            self.emit("request.rejected", {"reason": str(exc), "field": exc.field})
            return self._error(str(exc), 400, "client_unsupported_field")

        if dropped:
            self.emit("request.fields_dropped", {"fields": dropped})

        # --- C1 / C2 / C6 --------------------------------------------------
        transform_started = time.perf_counter()
        try:
            result = self.pipeline.transform(
                canonical,
                recent_tool_uses=self._tool_uses_in(canonical),
                recent_context=self._recent_context(canonical),
                sticky_tools=self.sticky_tools,
            )
        except ContextOverflowError as exc:
            self.emit("request.context_overflow", exc.details)
            return self._error(
                "the request does not fit the configured context window; "
                "reduce the task size or raise the context length",
                413,
                "context_overflow",
            )
        transform_ms = (time.perf_counter() - transform_started) * 1000.0

        self.sticky_tools = list(result.tool_selection.exposed_names) if result.tool_selection else []
        event = result.as_event_dict()
        # The proxy-only cost of the transformation (Plan.md §2 "proxy
        # overhead"). It excludes the upstream call and the network wait.
        event["transform_ms"] = round(transform_ms, 3)
        self.emit("request.transformed", event)
        if result.route:
            self.emit("request.routed", result.route.as_event_dict())

        # C0 body capture. With the default ``metadata`` capture nothing is stored
        # beyond hashes and rule ids. With ``local-replay`` the canonical request
        # body is written to the content-addressed local blob store so that
        # ``pocket replay`` can reconstruct the exact request later.
        capture_ref = self._capture_body(canonical)
        if capture_ref:
            self.emit("request.captured", {"blob_ref": capture_ref})

        target = self.target_for(result.request.model)
        client = UpstreamClient(
            target,
            timeout=self.config.upstream.timeout_seconds,
            transport=self.transport,
        )
        out_payload = result.request.to_payload()
        if target.model:
            out_payload["model"] = target.model

        # --- forward --------------------------------------------------------
        if result.request.stream:
            return await self._stream_response(client, out_payload, result, request_id, started)
        return await self._unary_response(client, out_payload, result, request_id, started)

    # ------------------------------------------------------------- responses

    async def _unary_response(
        self,
        client: UpstreamClient,
        payload: dict[str, Any],
        result: Any,
        request_id: str,
        started: float,
    ) -> Response:
        """Forward a non-streaming completion and validate its tool calls."""
        try:
            data, collector = await client.create_chat(payload)
        except UpstreamError as exc:
            self.emit("upstream.error", {"status_code": exc.status_code, "message": str(exc)})
            return self._error(
                "the upstream endpoint failed; no fallback was attempted",
                502,
                "upstream_unavailable",
            )
        except Exception as exc:  # noqa: BLE001
            self.emit("upstream.error", {"message": f"{type(exc).__name__}: {exc}"})
            return self._error("the upstream request failed", 502, "upstream_unavailable")

        repair, repairs = self._validate_tool_calls(collector, result)
        elapsed = time.monotonic() - started
        self.emit(
            "response.completed",
            {
                "latency_seconds": round(elapsed, 4),
                "usage": collector.usage,
                "finish_reason": collector.finish_reason,
                "content_length": len(collector.text),
                "tool_call_count": len(collector.tool_call_list()),
                "repair": repair,
            },
        )
        # ADR 0001: a repaired tool call is what the client receives.
        data = self._apply_repairs(data, repairs)
        return JSONResponse(data)

    async def _stream_response(
        self,
        client: UpstreamClient,
        payload: dict[str, Any],
        result: Any,
        request_id: str,
        started: float,
    ) -> Response:
        """Forward a streaming completion verbatim and validate the tool calls."""
        collector = StreamCollector()

        async def event_stream():
            try:
                async for line in client.stream_chat(payload):
                    yield line + "\n"
                    stripped = line.strip()
                    if stripped.startswith("data:") and not stripped.startswith("data: [DONE]"):
                        try:
                            collector.feed(json.loads(stripped[5:].strip()))
                        except json.JSONDecodeError:
                            pass
            except UpstreamError as exc:
                self.emit("upstream.error", {"status_code": exc.status_code, "message": str(exc)})
                err = {"error": {"message": "the upstream endpoint failed", "type": "upstream_error"}}
                yield "data: " + canonical_json(err) + "\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:  # noqa: BLE001
                self.emit("upstream.error", {"message": f"{type(exc).__name__}: {exc}"})
                err = {"error": {"message": "the upstream request failed", "type": "upstream_error"}}
                yield "data: " + canonical_json(err) + "\n\n"
                yield "data: [DONE]\n\n"

        async def finalize() -> None:
            repair, _repairs = self._validate_tool_calls(collector, result)
            elapsed = time.monotonic() - started
            self.emit(
                "response.completed",
                {
                    "latency_seconds": round(elapsed, 4),
                    "usage": collector.usage,
                    "finish_reason": collector.finish_reason,
                    "content_length": len(collector.text),
                    "tool_call_count": len(collector.tool_call_list()),
                    "repair": repair,
                },
            )

        response = StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        response.background = finalize
        return response

    # ------------------------------------------------------------ C3 / C4

    def _validate_tool_calls(
        self, collector: StreamCollector, result: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Validate the tool calls of a response with C3 and record the outcome.

        Returns the evidence summary and the list of repairs that may be applied
        to the tool calls the client receives.

        A repair that C3 produced, that validates against the client's original
        schema and that only changed the *structure* of the call is applied to
        the response (ADR 0001). The original text, the repaired text, both
        hashes and the structured diff are recorded, so the change is auditable.
        A call that could not be repaired is passed through unchanged.

        When the upstream delivered a tool call as *text* instead of as a
        structured call, that is recorded as well. The proxy does not turn it
        into an executable call — that would guess the model's intent — but the
        evidence must show it, because such a response makes the client make no
        progress and would otherwise be indistinguishable from a model failure.
        """
        calls = collector.tool_call_list()
        text_call = describe_text_tool_call(collector.text)
        if not calls:
            summary = {"checked": 0, "repaired": 0, "failed": 0, "outcomes": []}
            if text_call is not None:
                summary["delivered_as_text"] = text_call
            self.emit("toolcall.validated", summary)
            return summary, []

        original_request = result.original
        repairer = self.repairer_for(original_request)
        outcomes: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        repaired = 0
        failed = 0

        for index, call in enumerate(calls):
            outcome = repairer.repair(call.name, call.arguments)
            if outcome.ok:
                if outcome.text_changed:
                    repaired += 1
                    repairs.append(
                        {
                            "index": index,
                            "id": call.id,
                            "name": outcome.tool_name,
                            "arguments": outcome.repaired_arguments_text,
                        }
                    )
            else:
                failed += 1
                self.ledger.record_error(
                    self.loop_state.step,
                    f"tool call {call.name!r} failed validation: {outcome.error_class}",
                )
            outcomes.append(outcome.as_event_dict())
            self.recent_tool_uses.append(outcome.tool_name or call.name)

            events = self.loop_guard.observe_call(self.loop_state, call.name, call.arguments)
            if events:
                self.emit("loop.detected", {"events": [e.as_dict() for e in events]})
                self.ledger.record_warning(self.loop_state.step, events[0].detail)

        summary = {
            "checked": len(calls),
            "repaired": repaired,
            "failed": failed,
            "outcomes": outcomes,
        }
        self.emit("toolcall.validated", summary)
        return summary, repairs

    @staticmethod
    def _apply_repairs(data: dict[str, Any], repairs: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the response body with the repaired tool calls applied."""
        if not repairs:
            return data
        choices = data.get("choices") or []
        for choice in choices:
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []
            for repair in repairs:
                index = repair.get("index")
                if not isinstance(index, int) or index >= len(tool_calls):
                    continue
                call = tool_calls[index]
                if call.get("id") and repair.get("id") and call["id"] != repair["id"]:
                    continue
                function = call.get("function") or {}
                function["arguments"] = repair.get("arguments", "")
                call["function"] = function
            message["tool_calls"] = tool_calls
            choice["message"] = message
        data["choices"] = choices
        return data

    # ------------------------------------------------------------- endpoints

    def _error(self, message: str, status_code: int, error_type: str) -> JSONResponse:
        """Return a stable error body."""
        return JSONResponse(
            {"error": {"message": message, "type": error_type, "code": error_type}},
            status_code=status_code,
        )

    async def models(self, request: Request) -> Response:
        """List the configured upstream model."""
        model = self.config.upstream.model
        if not model:
            return JSONResponse({"object": "list", "data": []})
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": 0,
                        "owned_by": "pocket-harness-upstream",
                    }
                ],
            }
        )

    async def health(self, request: Request) -> Response:
        """Return a liveness probe."""
        return JSONResponse({"status": "ok", "run_id": self.run_id})

    async def status(self, request: Request) -> Response:
        """Return a deterministic status report for the current run."""
        return JSONResponse(
            {
                "run_id": self.run_id,
                "pipeline_version": PIPELINE_VERSION,
                "config_hash": self.config.config_hash(),
                "requests": self.request_counter,
                "ledger": self.ledger.summary(),
                "loop": {
                    "step": self.loop_state.step,
                    "events": [e.as_dict() for e in self.loop_state.events],
                },
                "exposed_tools": self.sticky_tools,
            }
        )

    async def flush(self, request: Request) -> Response:
        """Return the run's evidence events."""
        events = list(self.store.iter_events(self.run_id))
        return JSONResponse({"run_id": self.run_id, "events": events})


def create_app(config: Config, store: StateStore | None = None) -> Starlette:
    """Create the Starlette application for the proxy."""
    service = ProxyService(config, store=store)

    routes = [
        Route("/v1/chat/completions", service.chat_completions, methods=["POST"]),
        Route("/v1/models", service.models, methods=["GET"]),
        Route("/healthz", service.health, methods=["GET"]),
        Route("/pocket/status", service.status, methods=["GET"]),
        Route("/pocket/flush", service.flush, methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    app.state.service = service
    return app

