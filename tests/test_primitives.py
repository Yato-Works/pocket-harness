"""Tests for the configuration, hashing, redaction, and token primitives."""

from __future__ import annotations

import json
import os

from pocket_harness.config import Config, config_from_mapping, load_config
from pocket_harness.hashing import canonical_json, hash_obj
from pocket_harness.redaction import redact_headers, redact_obj, redact_text
from pocket_harness.tokens import TokenCounter, TokenSource, estimate_tokens

# ------------------------------------------------------------------ hashing


def test_canonical_json_is_order_independent() -> None:
    """Object key order does not change the encoding."""
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_has_no_padding() -> None:
    """The encoding uses no insignificant whitespace."""
    assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'


def test_hash_obj_is_stable() -> None:
    """The same value always hashes to the same digest."""
    assert hash_obj({"a": 1}) == hash_obj({"a": 1})
    assert hash_obj({"a": 1}) != hash_obj({"a": 2})


def test_hash_obj_handles_non_ascii() -> None:
    """Non-ASCII text hashes without an encoding error."""
    assert len(hash_obj({"text": "検索"})) == 64


# ---------------------------------------------------------------- redaction


def test_bearer_token_is_redacted() -> None:
    """A bearer token in a header is removed."""
    headers = redact_headers({"Authorization": "Bearer sk-abcdefghijklmnop"})
    assert headers["Authorization"] == "[REDACTED]"


def test_api_key_header_is_redacted() -> None:
    """An API key header is removed."""
    assert redact_headers({"x-api-key": "abcd1234"})["x-api-key"] == "[REDACTED]"


def test_secret_in_text_is_redacted() -> None:
    """A well-known secret shape in free text is removed."""
    text = "the key is sk-abcdefghijklmnopqrstuvwxyz"
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in redact_text(text)


def test_assignment_secret_is_redacted() -> None:
    """An ``api_key = ...`` assignment is removed."""
    text = 'config api_key = "supersecretvalue"'
    assert "supersecretvalue" not in redact_text(text)


def test_redact_obj_recurses() -> None:
    """Nested secrets are removed."""
    payload = {"a": {"b": {"authorization": "Bearer sk-abcdefghijklmnop"}}}
    assert "sk-abcdefghijklmnop" not in json.dumps(redact_obj(payload))


def test_redaction_is_best_effort_not_a_guarantee() -> None:
    """An unrecognized secret shape survives — redaction is documented as best effort."""
    assert redact_text("my_very_own_secret_token_42") == "my_very_own_secret_token_42"


# ------------------------------------------------------------------- tokens


def test_estimate_is_labelled_as_estimated() -> None:
    """A heuristic count is never presented as exact."""
    count = estimate_tokens("hello world")
    assert count.source is TokenSource.ESTIMATED
    assert not count.is_exact


def test_empty_text_is_zero_tokens() -> None:
    """Empty text costs nothing."""
    assert estimate_tokens("").value == 0


def test_estimate_is_monotonic() -> None:
    """More text never costs fewer tokens."""
    assert estimate_tokens("a" * 1000).value > estimate_tokens("a").value


def test_counter_prefers_the_exact_source() -> None:
    """A configured tokenizer makes the count exact."""

    class Fixed:
        def count(self, text: str) -> int:
            return 42

    count = TokenCounter(tokenizer=Fixed()).count_text("anything")
    assert count.is_exact
    assert count.value == 42


def test_counter_falls_back_to_the_estimate() -> None:
    """Without a tokenizer the count is an estimate."""
    assert not TokenCounter().count_text("hello").is_exact


def test_counter_labels_the_method() -> None:
    """The provenance method is always recorded."""
    count = TokenCounter().count_text("hello")
    assert count.method
    assert count.as_dict()["source"] == "estimated"


# ------------------------------------------------------------------- config


def test_config_defaults_are_safe() -> None:
    """The default configuration captures metadata only and has no telemetry."""
    config = Config()
    assert config.logging.capture == "metadata"
    assert config.logging.telemetry is False
    assert config.routing.allow_paid_fallback is False


def test_config_hash_excludes_the_api_key() -> None:
    """A credential never enters the configuration hash."""
    a = Config()
    b = Config()
    b.upstream.api_key = "sk-secret"
    assert a.config_hash() == b.config_hash()


def test_config_hash_changes_with_the_context_length() -> None:
    """A value that changes a transformation outcome is part of the hash."""
    a = Config()
    b = Config()
    b.upstream.context_length = 4096
    assert a.config_hash() != b.config_hash()


def test_config_can_be_loaded_from_a_mapping() -> None:
    """A raw mapping becomes a configuration."""
    config = config_from_mapping(
        {
            "upstream": {"base_url": "http://x/v1", "model": "m"},
            "context": {"keep_recent_turns": 9},
        }
    )
    assert config.upstream.base_url == "http://x/v1"
    assert config.context.keep_recent_turns == 9


def test_missing_config_file_yields_defaults(tmp_path) -> None:
    """A missing file produces the built-in defaults."""
    config = load_config(tmp_path / "does-not-exist.toml")
    assert config.source_path is None
    assert config.upstream.base_url == "http://127.0.0.1:11434/v1"


def test_routes_are_parsed() -> None:
    """Static routes are loaded from the configuration."""
    config = config_from_mapping(
        {
            "routing": {"enabled": True},
            "routes": {
                "fast": {
                    "requested_model": "gpt-4o-mini",
                    "upstream": "fast",
                    "upstream_model": "qwen2.5-coder:7b",
                    "base_url": "http://127.0.0.1:11434/v1",
                }
            },
        }
    )
    assert config.routing.enabled is True
    assert config.routes["fast"].upstream_model == "qwen2.5-coder:7b"


def test_api_key_prefers_the_environment_reference() -> None:
    """An environment reference wins over an inline key."""
    config = Config()
    config.upstream.api_key = "inline"
    config.upstream.api_key_env = "POCKET_TEST_KEY"
    os.environ["POCKET_TEST_KEY"] = "from-env"
    try:
        assert config.upstream.resolved_api_key() == "from-env"
    finally:
        del os.environ["POCKET_TEST_KEY"]

