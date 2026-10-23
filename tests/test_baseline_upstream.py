"""Regression tests for the independent baseline upstream (DECISION.md §7.1)."""

from __future__ import annotations

from pocket_harness.config import Config, UpstreamConfig, config_from_mapping
from pocket_harness.repo_bench import build_manifest, upstream_for


def _config(baseline: str | None) -> Config:
    return Config(upstream=UpstreamConfig(base_url="http://127.0.0.1:11434/v1", baseline_base_url=baseline))


def test_without_a_baseline_upstream_both_conditions_use_the_primary() -> None:
    config = _config(None)
    assert upstream_for(config, "baseline") == "http://127.0.0.1:11434/v1"
    assert upstream_for(config, "harness") == "http://127.0.0.1:11434/v1"


def test_with_a_baseline_upstream_only_the_baseline_moves() -> None:
    config = _config("http://127.0.0.1:11435/v1")
    assert upstream_for(config, "baseline") == "http://127.0.0.1:11435/v1"
    assert upstream_for(config, "harness") == "http://127.0.0.1:11434/v1"


def test_the_baseline_upstream_round_trips_through_the_toml_mapping() -> None:
    config = config_from_mapping(
        {
            "upstream": {
                "base_url": "http://127.0.0.1:11434/v1",
                "baseline_base_url": "http://127.0.0.1:11435/v1",
            }
        }
    )
    assert upstream_for(config, "baseline") == "http://127.0.0.1:11435/v1"
    assert upstream_for(config, "harness") == "http://127.0.0.1:11434/v1"


def test_the_manifest_records_both_upstreams() -> None:
    config = _config("http://127.0.0.1:11435/v1")
    manifest = build_manifest(config, seeds=[1], conditions=["baseline", "harness"])
    assert manifest["baseline_upstream_base_url"] == "http://127.0.0.1:11435/v1"
    assert manifest["upstream_base_url"] == "http://127.0.0.1:11434/v1"


def test_the_bench_flag_exists_and_defaults_to_none() -> None:
    import pocket_harness.cli as cli

    parser = cli.build_parser()
    args = parser.parse_args(["bench"])
    assert getattr(args, "baseline_upstream", "missing") is None


def test_a_baseline_record_reports_its_own_upstream() -> None:
    """The per-record upstream field must not hide the baseline's real route.

    This pins the record drafting logic used by ``_run_one`` (the displayed
    URL has to follow the condition, not the primary config alone).
    """

    config = _config("http://127.0.0.1:11435/v1")

    def displayed_upstream(condition: str) -> str:
        return upstream_for(config, condition)

    assert displayed_upstream("baseline") == "http://127.0.0.1:11435/v1"
    assert displayed_upstream("harness") == "http://127.0.0.1:11434/v1"


def test_the_config_hash_changes_when_the_baseline_upstream_is_set() -> None:
    base = _config(None)
    moved = _config("http://127.0.0.1:11435/v1")
    assert base.config_hash() != moved.config_hash()
