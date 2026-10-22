"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pocket_harness.config import Config  # noqa: E402
from pocket_harness.state import StateStore  # noqa: E402


@pytest.fixture
def config() -> Config:
    """Return a configuration with a small, known context window."""
    cfg = Config()
    cfg.upstream.base_url = "http://127.0.0.1:11434/v1"
    cfg.upstream.model = "test-model"
    cfg.upstream.context_length = 2048
    cfg.context.reserved_output_tokens = 128
    cfg.context.safety_margin_tokens = 64
    cfg.context.max_tool_output_bytes = 512
    return cfg


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    """Return a state store rooted in a temporary directory."""
    return StateStore(tmp_path / "pocket")
