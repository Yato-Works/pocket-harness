"""The upstream client: a thin, transparent OpenAI-compatible HTTP client.

Responsibilities:

* forward the (already transformed) request to the configured upstream,
* stream the response back without buffering when the client asked for streaming,
* collect tool calls from a streamed response so that C3 can validate them,
* translate upstream errors into a small, stable error taxonomy.

The client never rewrites the model output. It does not add a system prompt, does not
change sampling parameters, and does not inject fallback behavior.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx


class UpstreamError(Exception):
    """An error returned by the upstream endpoint."""

    def __init__(self, message: str, status_code: int = 502, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass(slots=True)
class ToolCallDelta:
    """An accumulated tool call from a streamed response."""

    id: str = ""
    name: str = ""
    arguments: str = ""
    index: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


class StreamCollector:
    """Accumulates streamed chunks into a complete response.

    Used both to forward the stream to the client unchanged and to obtain the
    complete tool calls for C3 validation.
    """

    def __init__(self) -> None:
        self.content: list[str] = []
        self.tool_calls: dict[int, ToolCallDelta] = {}
        self.finish_reason: str | None = None
        self.model: str = ""
        self.id: str = ""
        self.usage: dict[str, Any] = {}
        self.raw_chunks: list[dict[str, Any]] = []

    def feed(self, chunk: dict[str, Any]) -> None:
        """Accumulate one SSE ``data`` payload."""
        self.raw_chunks.append(chunk)
        if not chunk:
            return
        self.id = chunk.get("id") or self.id
        self.model = chunk.get("model") or self.model
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        for choice in choices:
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str):
                self.content.append(text)
            for call in delta.get("tool_calls") or []:
                index = int(call.get("index", 0))
                target = self.tool_calls.setdefault(index, ToolCallDelta(index=index))
                if call.get("id"):
                    target.id = call["id"]
                function = call.get("function") or {}
                if function.get("name"):
                    target.name += function["name"]
                if function.get("arguments"):
                    target.arguments += function["arguments"]

    @property
    def text(self) -> str:
        """The accumulated assistant text."""
        return "".join(self.content)

    def tool_call_list(self) -> list[ToolCallDelta]:
        """The accumulated tool calls, ordered by index."""
        return [self.tool_calls[key] for key in sorted(self.tool_calls)]


@dataclass(slots=True)
class UpstreamTarget:
    """Where a request should be sent."""

    base_url: str
    model: str = ""
    api_key: str | None = None
    api_key_env: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    def headers(self) -> dict[str, str]:
        """Return the outbound headers, including authorization when configured."""
        out = {"Content-Type": "application/json"}
        out.update(self.extra_headers)
        if self.api_key:
            out["Authorization"] = f"Bearer {self.api_key}"
        return out

    def chat_url(self) -> str:
        """Return the chat-completions URL."""
        return self.base_url.rstrip("/") + "/chat/completions"

    def models_url(self) -> str:
        """Return the models URL."""
        return self.base_url.rstrip("/") + "/models"

    def count_url(self) -> str:
        """Return the tokenize/count URL used by ``doctor`` when available."""
        return self.base_url.rstrip("/") + "/tokenize"


class UpstreamClient:
    """A minimal async client for an OpenAI-compatible upstream.

    The ``transport`` argument exists so that the proxy can be tested without a
    running inference server. It has no effect in production.
    """

    def __init__(
        self,
        target: UpstreamTarget,
        timeout: float = 600.0,
        transport: Any = None,
    ) -> None:
        self.target = target
        self.timeout = timeout
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport)

    async def list_models(self) -> list[str]:
        """Return the model IDs advertised by the upstream."""
        async with self._client() as client:
            response = await client.get(self.target.models_url(), headers=self.target.headers())
        if response.status_code >= 400:
            raise UpstreamError(
                "upstream rejected the models request",
                status_code=response.status_code,
                body=response.text[:500],
            )
        payload = response.json()
        return [str(item.get("id")) for item in payload.get("data", []) if item.get("id")]

    async def count_tokens(self, text: str) -> int | None:
        """Return an exact token count from the upstream, if it supports one."""
        async with self._client() as client:
            response = await client.post(
                self.target.count_url(),
                headers=self.target.headers(),
                json={"model": self.target.model, "prompt": text},
            )
        if response.status_code >= 400:
            return None
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return None
        count = payload.get("count")
        if isinstance(count, int):
            return count
        tokens = payload.get("tokens")
        if isinstance(tokens, list):
            return len(tokens)
        return None

    # --------------------------------------------------------------- requests

    async def create_chat(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], StreamCollector]:
        """Perform a non-streaming chat completion and collect the result."""
        body = dict(payload)
        body["stream"] = False
        async with self._client() as client:
            response = await client.post(
                self.target.chat_url(), headers=self.target.headers(), json=body
            )
        if response.status_code >= 400:
            raise UpstreamError(
                f"upstream returned HTTP {response.status_code}",
                status_code=response.status_code,
                body=response.text[:1000],
            )
        data = response.json()
        collector = StreamCollector()
        collector.id = str(data.get("id", ""))
        collector.model = str(data.get("model", ""))
        collector.usage = data.get("usage") or {}
        for choice in data.get("choices", []):
            message = choice.get("message") or {}
            text = message.get("content")
            if isinstance(text, str):
                collector.content.append(text)
            for index, call in enumerate(message.get("tool_calls") or []):
                function = call.get("function") or {}
                collector.tool_calls[index] = ToolCallDelta(
                    id=str(call.get("id", "")),
                    name=str(function.get("name", "")),
                    arguments=str(function.get("arguments", "")),
                    index=index,
                )
            if choice.get("finish_reason"):
                collector.finish_reason = choice["finish_reason"]
        return data, collector

    async def stream_chat(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[str]:
        """Yield raw SSE lines from a streaming chat completion.

        The raw lines are forwarded to the client unchanged. The collector is updated
        in parallel so that the proxy can validate the tool calls after the stream
        ends.
        """
        body = dict(payload)
        body["stream"] = True
        async with self._client() as client:
            async with client.stream(
                "POST", self.target.chat_url(), headers=self.target.headers(), json=body
            ) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")
                    raise UpstreamError(
                        f"upstream returned HTTP {response.status_code}",
                        status_code=response.status_code,
                        body=text[:1000],
                    )
                async for line in response.aiter_lines():
                    yield line

