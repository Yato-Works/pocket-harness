"""Canonical internal request/response representation (C0).

The proxy normalizes every supported client request into :class:`CanonicalRequest`
before any transformation runs, and translates back to the client protocol only at
the edges (Plan.md §4.1).

The canonical model deliberately keeps *both* the client-supplied tool definitions
(the "authority") and the tool subset that is exposed to the model, because C3 must
validate a repaired call against the **original** schema even when C1 hid the tool.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Client-facing protocols supported in v0.1.
Protocol = Literal["openai-chat-completions"]

#: Upstream endpoint kinds supported in v0.1.
UpstreamKind = Literal["openai-compatible"]


class FunctionDef(BaseModel):
    """An OpenAI-style function/tool definition."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolDef(BaseModel):
    """A client-supplied tool entry."""

    model_config = ConfigDict(extra="allow")

    type: str = "function"
    function: FunctionDef


class ToolCall(BaseModel):
    """A tool call emitted by the model."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    type: str = "function"
    index: int | None = None
    function: dict[str, Any] = Field(default_factory=dict)

    @property
    def name(self) -> str:
        return str(self.function.get("name", ""))

    @property
    def arguments_text(self) -> str:
        return str(self.function.get("arguments", ""))


class Message(BaseModel):
    """A chat message in the canonical representation."""

    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class CanonicalRequest(BaseModel):
    """The single internal representation every transformation operates on."""

    model_config = ConfigDict(extra="allow")

    protocol: Protocol = "openai-chat-completions"
    model: str = ""
    messages: list[Message] = Field(default_factory=list)
    tools: list[ToolDef] = Field(default_factory=list)
    tool_choice: Any = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    seed: int | None = None
    response_format: Any = None
    parallel_tool_calls: bool | None = None
    user: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: Fields the client sent that the canonical model does not model explicitly.
    extra_fields: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CanonicalRequest:
        """Build a canonical request from a raw client payload."""
        known = {
            "model",
            "messages",
            "tools",
            "tool_choice",
            "temperature",
            "top_p",
            "max_tokens",
            "stream",
            "stream_options",
            "seed",
            "response_format",
            "parallel_tool_calls",
            "user",
            "metadata",
        }
        data: dict[str, Any] = {k: v for k, v in payload.items() if k in known}
        extra = {k: v for k, v in payload.items() if k not in known}
        request = cls(**data)
        request.extra_fields = extra
        return request

    def to_payload(self) -> dict[str, Any]:
        """Render the canonical request back into an OpenAI-compatible payload."""
        payload: dict[str, Any] = {}
        payload["model"] = self.model
        payload["messages"] = [m.to_dict() for m in self.messages]
        if self.tools:
            payload["tools"] = [t.model_dump(exclude_none=True) for t in self.tools]
        if self.tool_choice is not None:
            payload["tool_choice"] = self.tool_choice
        for name in ("temperature", "top_p", "max_tokens", "seed", "response_format", "user"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        if self.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = self.parallel_tool_calls
        if self.stream:
            payload["stream"] = True
            if self.stream_options is not None:
                payload["stream_options"] = self.stream_options
        payload.update(self.extra_fields)
        return payload

    def tool_schema_map(self) -> dict[str, dict[str, Any]]:
        """Return ``{tool_name: json_schema}`` for every client-supplied tool."""
        out: dict[str, dict[str, Any]] = {}
        for tool in self.tools:
            schema = tool.function.parameters or {}
            out[tool.function.name] = schema
        return out


class CanonicalResponse(BaseModel):
    """A normalized upstream response."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    model: str = ""
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
