"""Client profiles.

A profile describes how a specific client agent talks to the proxy: which tools it
registers, which of them are destructive, and which fields the upstream cannot accept.

v0.1 ships profiles for Cline and OpenCode. Both speak the OpenAI-compatible chat
completions protocol, so the profile only needs to describe the tool surface and the
upstream quirks — not a separate protocol implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pocket_harness.config import Config


@dataclass(slots=True)
class ClientProfile:
    """The description of one client agent."""

    name: str
    protocol: str = "openai-chat-completions"
    pinned_tools: list[str] = field(default_factory=list)
    destructive_tools: list[str] = field(default_factory=list)
    #: Fields the upstream is known to reject for this client.
    unsupported_fields: list[str] = field(default_factory=list)
    optional_fields: list[str] = field(default_factory=list)
    notes: str = ""


#: Tools whose call can change the state of the machine. C3 restricts these to
#: syntax-level repair only.
DEFAULT_DESTRUCTIVE_TOOLS = [
    "execute_command",
    "run_command",
    "run_terminal_cmd",
    "execute",
    "shell",
    "bash",
    "write_to_file",
    "apply_patch",
    "delete_file",
    "remove_file",
    "move_file",
    "edit_file",
]

PROFILES: dict[str, ClientProfile] = {
    "cline": ClientProfile(
        name="cline",
        pinned_tools=["execute_command", "read_file", "list_files"],
        destructive_tools=DEFAULT_DESTRUCTIVE_TOOLS,
        notes=(
            "Cline sends one large tool set per request and streams the response. "
            "C1 reduces the exposed set; the client tool list stays the C3 authority."
        ),
    ),
    "opencode": ClientProfile(
        name="opencode",
        pinned_tools=["bash", "read", "edit"],
        destructive_tools=DEFAULT_DESTRUCTIVE_TOOLS,
        notes="OpenCode uses compact tool names and sends a system prompt per request.",
    ),
    "generic": ClientProfile(
        name="generic",
        pinned_tools=[],
        destructive_tools=DEFAULT_DESTRUCTIVE_TOOLS,
        notes="A minimal profile for any OpenAI-compatible client.",
    ),
}


def get_profile(name: str) -> ClientProfile:
    """Return a profile by name, falling back to the generic profile."""
    return PROFILES.get(name, PROFILES["generic"])


def apply_profile(config: Config, name: str) -> Config:
    """Apply a client profile to a configuration.

    Values that the user set explicitly in the config file are not overwritten.
    """
    profile = get_profile(name)
    config.client.profile = profile.name
    if not config.client.pinned_tools:
        config.client.pinned_tools = list(profile.pinned_tools)
    if not config.client.destructive_tools:
        config.client.destructive_tools = list(profile.destructive_tools)
    for field_name in profile.unsupported_fields:
        if field_name not in config.upstream.unsupported_fields:
            config.upstream.unsupported_fields.append(field_name)
    for field_name in profile.optional_fields:
        if field_name not in config.upstream.optional_fields:
            config.upstream.optional_fields.append(field_name)
    return config
