"""Tests for the C6 static router and the deterministic pipeline."""

from __future__ import annotations

from pocket_harness.canonical import CanonicalRequest
from pocket_harness.components.router import Router
from pocket_harness.config import Config, config_from_mapping
from pocket_harness.pipeline import Pipeline, UnsupportedFieldError


def routed_config() -> Config:
    """Return a configuration with one static route."""
    return config_from_mapping(
        {
            "upstream": {
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "default-model",
                "context_length": 8192,
            },
            "routing": {"enabled": True, "allow_paid_fallback": False},
            "routes": {
                "fast": {
                    "requested_model": "gpt-4o-mini",
                    "upstream": "fast",
                    "upstream_model": "qwen2.5-coder:7b",
                    "base_url": "http://127.0.0.1:11435/v1",
                    "context_length": 4096,
                }
            },
        }
    )


def test_disabled_routing_always_uses_the_default() -> None:
    """With routing off, the requested model never changes the upstream."""
    config = routed_config()
    config.routing.enabled = False
    route = Router(config).resolve("gpt-4o-mini")
    assert route.source == "default"
    assert route.base_url == "http://127.0.0.1:11434/v1"


def test_enabled_routing_matches_by_exact_name() -> None:
    """An exact requested-model name selects the configured route."""
    route = Router(routed_config()).resolve("gpt-4o-mini")
    assert route.source == "route"
    assert route.model == "qwen2.5-coder:7b"
    assert route.base_url == "http://127.0.0.1:11435/v1"


def test_unknown_model_falls_back_to_the_default() -> None:
    """An unknown model name uses the default upstream, not a guess."""
    route = Router(routed_config()).resolve("some-other-model")
    assert route.source == "default"
    assert route.base_url == "http://127.0.0.1:11434/v1"


def test_routing_is_case_sensitive() -> None:
    """The match is exact; a differently-cased name is not routed."""
    route = Router(routed_config()).resolve("GPT-4O-MINI")
    assert route.source == "default"


# ------------------------------------------------------------------ pipeline


def test_pipeline_is_deterministic() -> None:
    """The same input always produces the same output hash."""
    config = routed_config()
    pipeline = Pipeline(config)
    request = CanonicalRequest(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hello"}],
    )
    first = pipeline.transform(request)
    second = pipeline.transform(request)
    assert first.output_hash == second.output_hash
    assert first.input_hash == second.input_hash


def test_pipeline_reports_the_token_source() -> None:
    """The token count always carries its provenance."""
    pipeline = Pipeline(routed_config())
    result = pipeline.transform(
        CanonicalRequest(model="m", messages=[{"role": "user", "content": "hello"}])
    )
    assert result.token_after.source.value in {"exact", "estimated"}
    assert result.token_after.method


def test_pipeline_records_the_rules_it_applied() -> None:
    """Every applied rule is reported with an id."""
    config = routed_config()
    pipeline = Pipeline(config)
    request = CanonicalRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": f"t{i}",
                    "description": "tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for i in range(20)
        ],
    )
    result = pipeline.transform(request)
    rule_ids = [rule["id"] for rule in result.rules]
    assert "C1.select" in rule_ids
    assert all(isinstance(rule_id, str) for rule_id in rule_ids)


def test_unsupported_field_is_rejected_by_default() -> None:
    """A field the upstream cannot accept is an error, not a silent drop."""
    config = Config()
    config.upstream.unsupported_fields = ["some_field"]
    pipeline = Pipeline(config)
    try:
        pipeline.normalize({"model": "m", "messages": [], "some_field": 1})
    except UnsupportedFieldError as exc:
        assert exc.field == "some_field"
    else:
        raise AssertionError("expected an UnsupportedFieldError")


def test_optional_unsupported_field_is_dropped_with_a_warning() -> None:
    """An optional unsupported field is dropped and reported."""
    config = Config()
    config.upstream.unsupported_fields = ["some_field"]
    config.upstream.optional_fields = ["some_field"]
    pipeline = Pipeline(config)
    request, dropped = pipeline.normalize(
        {"model": "m", "messages": [], "some_field": 1}
    )
    assert "some_field" not in request.extra_fields
    assert dropped[0]["field"] == "some_field"
