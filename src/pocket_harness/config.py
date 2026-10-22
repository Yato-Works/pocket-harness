"""Configuration loading for Pocket Harness.

The config surface mirrors the illustrative ``pocket.toml`` sketch in Plan.md
Appendix A. Every value that changes a transformation outcome is part of the
*configuration hash*, which is recorded next to the request hash so that a replay
can prove it used the same settings (Plan.md §2 "Reproducibility").
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pocket_harness.hashing import hash_obj

DEFAULT_CONFIG_PATHS = (
    Path.home() / ".pocket" / "pocket.toml",
    Path("pocket.toml"),
)


class ConfigError(Exception):
    """Raised when a configuration file cannot be used as written."""


#: Matches a Windows drive path such as ``C:\Users\me`` inside a TOML file.
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\\\")


def _toml_error_hint(source: Path, exc: tomllib.TOMLDecodeError) -> str:
    """Return an actionable hint for a TOML parse error.

    The most common cause on Windows is a path written with backslashes inside a
    basic string (``"C:\\Users\\me"``), where ``\\U`` is an invalid escape sequence.
    A literal string (``'C:\\Users\\me'``) or forward slashes both work.
    """
    hint = f"the configuration file {source} is not valid TOML: {exc}"
    message = str(exc).lower()
    looks_like_escape = "\\" in str(exc) or any(
        marker in message
        for marker in ("escape", "invalid hex", "invalid value", "not a valid")
    )
    if not looks_like_escape:
        try:
            looks_like_escape = bool(_WINDOWS_PATH_RE.search(source.read_text(encoding="utf-8")))
        except OSError:  # pragma: no cover - unreadable file
            looks_like_escape = False
    if looks_like_escape:
        hint += (
            "\nhint: a Windows path with backslashes must use a literal string "
            "(single quotes) or forward slashes, e.g. root = 'C:/Users/me/.pocket'"
        )
    return hint


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    host: str = "127.0.0.1"
    port: int = 8787


class StateConfig(BaseModel):
    """Where the local state (events, blobs, the SQLite index) is stored."""

    model_config = ConfigDict(extra="allow")

    #: The state root. ``~`` is expanded. Defaults to ``~/.pocket``.
    root: str = "~/.pocket"

    def resolved_root(self) -> Path:
        """Return the expanded state root."""
        return Path(self.root).expanduser()


class UpstreamConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["openai-compatible"] = "openai-compatible"
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = ""
    context_length: int | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = 600.0
    #: Whether the upstream exposes a tokenize/count endpoint.
    supports_count_endpoint: bool = False
    extra_headers: dict[str, str] = Field(default_factory=dict)
    #: Fields the upstream is known to reject or ignore.
    unsupported_fields: list[str] = Field(default_factory=list)
    #: Fields the upstream accepts but does not implement.
    optional_fields: list[str] = Field(default_factory=list)

    def resolved_api_key(self) -> str | None:
        """Return the API key, preferring the environment reference over inline values."""
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return self.api_key


class ClientConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    profile: str = "generic"
    protocol: Literal["openai-chat-completions"] = "openai-chat-completions"
    #: Tool names that must always be exposed to the model.
    pinned_tools: list[str] = Field(default_factory=list)
    #: Tools whose call may only be repaired at the syntax level.
    destructive_tools: list[str] = Field(default_factory=list)


class ContextConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    start_at: float = 0.85
    target_at: float = 0.70
    keep_recent_turns: int = 4
    system_prompt_mutation: bool = False
    reserved_output_tokens: int = 1024
    safety_margin_tokens: int = 256
    #: Maximum size of a single tool result before it is clipped, in bytes.
    max_tool_output_bytes: int = 8192
    #: Absolute hard ceiling on the estimated request size, independent of the context window.
    max_request_tokens: int | None = None


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_non_pinned: int = 6
    sticky_unused_turns: int = 5
    fuzzy_tool_name_repair: bool = False
    max_description_chars: int = 200
    #: Explicit alias table used by C3 (alias name -> canonical name).
    tool_aliases: dict[str, str] = Field(default_factory=dict)


class RepairConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_corrective_retries_per_request: int = 1
    max_corrective_retries_per_task: int = 3
    allow_fuzzy_tool_name: bool = False
    allow_schema_defaults: bool = False


class LoopGuardConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    repeated_call_threshold: int = 3
    max_steps_per_task: int = 40
    max_wall_seconds: int = 1800
    detect_oscillation: bool = True


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    capture: Literal["metadata", "local-replay"] = "metadata"
    telemetry: bool = False


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    allow_paid_fallback: bool = False


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    requested_model: str
    upstream: str = "default"
    upstream_model: str = ""
    enabled: bool = True
    api_key_env: str | None = None
    base_url: str | None = None
    context_length: int | None = None
    paid: bool = False
class Config(BaseModel):
    """The complete Pocket Harness configuration."""

    model_config = ConfigDict(extra="allow")

    server: ServerConfig = Field(default_factory=ServerConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    client: ClientConfig = Field(default_factory=ClientConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    repair: RepairConfig = Field(default_factory=RepairConfig)
    loop_guard: LoopGuardConfig = Field(default_factory=LoopGuardConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    routes: dict[str, RouteConfig] = Field(default_factory=dict)
    #: Path the config was loaded from (``None`` for pure defaults).
    source_path: str | None = None

    def effective_context_length(self) -> int | None:
        """Return the configured context window, if known."""
        if self.upstream.context_length:
            return self.upstream.context_length
        route = self.routes.get("default")
        if route and route.context_length:
            return route.context_length
        return None

    def config_hash(self) -> str:
        """Return a hash over every configuration value that can change an output."""
        payload = self.model_dump(exclude={"source_path"}, mode="json")
        # Credentials never influence a transformation and must not enter a hash that
        # is written to disk alongside run records.
        payload.get("upstream", {}).pop("api_key", None)
        return hash_obj(payload)


def default_config_path() -> Path | None:
    """Return the first existing default config path, if any."""
    for path in DEFAULT_CONFIG_PATHS:
        if path.is_file():
            return path
    return None


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from ``path``, a default path, or built-in defaults."""
    if path is None:
        resolved = default_config_path()
    else:
        resolved = Path(path).expanduser()
    if resolved is None or not resolved.is_file():
        config = Config()
        config.source_path = None
        return config
    with resolved.open("rb") as handle:
        try:
            raw: dict[str, Any] = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(_toml_error_hint(resolved, exc)) from exc
    try:
        return config_from_mapping(raw, source_path=str(resolved))
    except ValidationError as exc:
        raise ConfigError(
            f"the configuration file {resolved} has invalid values:\n{exc}"
        ) from exc


def config_from_mapping(raw: dict[str, Any], source_path: str | None = None) -> Config:
    """Build a :class:`Config` from a raw mapping."""
    data = dict(raw)
    routes_raw = data.pop("routes", {}) or {}
    config = Config(**data)
    for name, route in routes_raw.items():
        config.routes[name] = RouteConfig(**route)
    config.source_path = source_path
    return config

