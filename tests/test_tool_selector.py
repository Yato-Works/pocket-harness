"""Tests for the C1 ToolSelector."""

from __future__ import annotations

from pocket_harness.canonical import CanonicalRequest, ToolDef
from pocket_harness.components.tool_selector import BM25, ToolSelector, tokenize


def make_tools(count: int) -> list[ToolDef]:
    """Return ``count`` simple tool definitions."""
    return [
        ToolDef(
            type="function",
            function={
                "name": f"tool_{i}",
                "description": f"Tool {i} that searches the codebase for a symbol",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        )
        for i in range(count)
    ]


def test_bm25_is_deterministic() -> None:
    """The same corpus and query must always produce the same scores."""
    corpus = [["read", "file"], ["write", "file"], ["run", "command"]]
    bm25 = BM25(corpus)
    first = bm25.scores(["file"])
    second = BM25(corpus).scores(["file"])
    assert first == second
    assert first[0] == first[1]  # both mention "file"
    assert first[2] == 0.0


def test_tokenize_handles_cjk() -> None:
    """CJK text is tokenized per character."""
    assert tokenize("検索") == ["検", "索"]
    assert tokenize("Read_File") == ["read_file"]
    assert tokenize("read file") == ["read", "file"]


def test_selection_respects_the_cap() -> None:
    """No more than ``max_non_pinned`` tools are exposed."""
    request = CanonicalRequest(model="m", tools=make_tools(20))
    selector = ToolSelector(max_non_pinned=6)
    selection = selector.select(request)
    assert len(selection.exposed) == 6
    assert len(selection.hidden) == 14


def test_pinned_tools_are_always_exposed() -> None:
    """Pinned tools survive selection even when the cap is tight."""
    request = CanonicalRequest(model="m", tools=make_tools(20))
    selector = ToolSelector(max_non_pinned=3, pinned_tools=["tool_17", "tool_18"])
    selection = selector.select(request)
    assert "tool_17" in selection.exposed_names
    assert "tool_18" in selection.exposed_names
    assert len(selection.exposed) == 3


def test_pinned_overflow_is_warned_not_dropped() -> None:
    """Too many pinned tools are preserved, and a warning is emitted."""
    request = CanonicalRequest(model="m", tools=make_tools(10))
    selector = ToolSelector(max_non_pinned=2, pinned_tools=["tool_0", "tool_1", "tool_2"])
    selection = selector.select(request)
    assert len(selection.exposed) == 3
    assert selection.warnings


def test_selection_is_deterministic() -> None:
    """Repeated selection produces the same exposed set."""
    request = CanonicalRequest(model="m", tools=make_tools(12))
    selector = ToolSelector(max_non_pinned=5)
    first = selector.select(request, recent_context="search the code")
    second = selector.select(request, recent_context="search the code")
    assert first.exposed_names == second.exposed_names


def test_sticky_tools_are_kept() -> None:
    """A sticky tool is retained across turns."""
    request = CanonicalRequest(model="m", tools=make_tools(20))
    selector = ToolSelector(max_non_pinned=3)
    selection = selector.select(request, sticky_tools=["tool_15"])
    assert "tool_15" in selection.exposed_names


def test_recently_used_tools_are_preferred() -> None:
    """A recently used tool outranks an unused one."""
    request = CanonicalRequest(model="m", tools=make_tools(10))
    selector = ToolSelector(max_non_pinned=1)
    selection = selector.select(request, recent_tool_uses=["tool_9"])
    assert selection.exposed_names == ["tool_9"]


def test_schema_is_never_rewritten() -> None:
    """The tool parameters are passed through untouched."""
    tools = make_tools(1)
    original = tools[0].function.parameters
    request = CanonicalRequest(model="m", tools=tools)
    selection = ToolSelector().select(request)
    assert selection.exposed[0].function.parameters == original


def test_client_order_is_preserved() -> None:
    """The exposed tools keep the client's original order."""
    request = CanonicalRequest(model="m", tools=make_tools(10))
    selection = ToolSelector(max_non_pinned=4).select(request)
    original_order = [t.function.name for t in request.tools]
    positions = [original_order.index(name) for name in selection.exposed_names]
    assert positions == sorted(positions)
