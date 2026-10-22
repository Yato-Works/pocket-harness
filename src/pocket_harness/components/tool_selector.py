"""C1 — ToolSelector.

Chooses a smaller subset of the client-supplied tools to expose to the model, while
keeping the *original* tool definitions as the authority for validation.

Design rules enforced here (Plan.md §6 C1):

* ``pinned`` tools are always exposed.
* Remaining tools are ranked with deterministic signals only (recent use + lexical
  relevance via a self-contained BM25 scorer).
* The argument schema is **never** rewritten — only the description may be truncated.
* Selection is *sticky*: a selected tool stays available until it has been unused for
  ``sticky_unused_turns`` turns, to avoid needless prompt churn / cache invalidation.
* If the pinned set already exceeds the cap, the pinned tools are preserved and a
  warning is emitted rather than silently dropping them.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from pocket_harness.canonical import CanonicalRequest, ToolDef

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿぀-ヿ]+")
_CJK_RE = re.compile(r"[一-鿿぀-ヿ]+")


def tokenize(text: str) -> list[str]:
    """Split ``text`` into lowercase lexical tokens (CJK handled per character)."""
    if not text:
        return []
    tokens: list[str] = []
    for match in _TOKEN_RE.findall(text):
        lowered = match.lower()
        if _CJK_RE.fullmatch(lowered):
            tokens.extend(list(lowered))
        else:
            tokens.append(lowered)
    return tokens


class BM25:
    """A small, deterministic BM25 scorer.

    Implemented locally rather than pulled from a dependency so that the ranking is
    reproducible across environments and free of any hidden model or network call.
    """

    __slots__ = ("_docs", "_df", "_avg_len", "_k1", "_b")

    def __init__(self, corpus: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self._docs = [list(doc) for doc in corpus]
        self._df: Counter[str] = Counter()
        for doc in self._docs:
            for term in set(doc):
                self._df[term] += 1
        total = sum(len(doc) for doc in self._docs)
        self._avg_len = (total / len(self._docs)) if self._docs else 0.0
        self._k1 = k1
        self._b = b

    def score(self, query: Sequence[str], index: int) -> float:
        """Return the BM25 score of document ``index`` against ``query``."""
        if not self._docs or not query:
            return 0.0
        doc = self._docs[index]
        if not doc:
            return 0.0
        n_docs = len(self._docs)
        counts = Counter(doc)
        length = len(doc)
        score = 0.0
        for term in query:
            freq = counts.get(term, 0)
            if freq == 0:
                continue
            df = self._df.get(term, 0)
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            denom = freq + self._k1 * (1 - self._b + self._b * length / (self._avg_len or 1.0))
            score += idf * (freq * (self._k1 + 1)) / (denom or 1.0)
        return score

    def scores(self, query: Sequence[str]) -> list[float]:
        """Return the score of every document against ``query``."""
        return [self.score(query, i) for i in range(len(self._docs))]


@dataclass(slots=True)
class ToolSelection:
    """The outcome of one C1 selection."""

    exposed: list[ToolDef] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
    pinned: list[str] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def exposed_names(self) -> list[str]:
        return [tool.function.name for tool in self.exposed]


class ToolSelector:
    """Deterministic tool-subset selector."""

    def __init__(
        self,
        max_non_pinned: int = 6,
        sticky_unused_turns: int = 5,
        max_description_chars: int = 200,
        pinned_tools: Sequence[str] = (),
        aliases: dict[str, str] | None = None,
    ) -> None:
        self.max_non_pinned = max_non_pinned
        self.sticky_unused_turns = sticky_unused_turns
        self.max_description_chars = max_description_chars
        self.pinned_tools = list(pinned_tools)
        self.aliases = dict(aliases or {})

    # ------------------------------------------------------------------ utils

    def _canonical_name(self, name: str) -> str:
        return self.aliases.get(name, name)

    def _truncate_description(self, description: str | None) -> str | None:
        """Return a deterministically truncated description (schema is never touched)."""
        if not description:
            return description
        if len(description) <= self.max_description_chars:
            return description
        first_line = next((line for line in description.splitlines() if line.strip()), description)
        clipped = first_line[: self.max_description_chars]
        return clipped.rstrip() + "…"

    # --------------------------------------------------------------- selection

    def select(
        self,
        request: CanonicalRequest,
        recent_tool_uses: Sequence[str] = (),
        recent_context: str = "",
        sticky_tools: Sequence[str] = (),
    ) -> ToolSelection:
        """Select the tools to expose for ``request``.

        ``recent_tool_uses`` are the tool names used in recent turns, newest last.
        ``sticky_tools`` are tools that were exposed in the previous turn and have not
        yet aged out of the sticky window.
        """
        tools = list(request.tools)
        names = [tool.function.name for tool in tools]
        result = ToolSelection()

        if not tools:
            return result

        pinned: list[ToolDef] = []
        rest: list[ToolDef] = []
        pinned_names = set(self.pinned_tools)
        for tool in tools:
            if tool.function.name in pinned_names:
                pinned.append(tool)
            else:
                rest.append(tool)
        result.pinned = [tool.function.name for tool in pinned]

        if len(pinned) > self.max_non_pinned:
            result.warnings.append(
                f"pinned tool count ({len(pinned)}) exceeds max_non_pinned "
                f"({self.max_non_pinned}); pinned tools preserved"
            )

        # --- relevance signal: BM25 over tool name + description -------------
        corpus = [
            tokenize(f"{tool.function.name} {tool.function.description or ''}")
            for tool in rest
        ]
        query = tokenize(recent_context)
        bm25 = BM25(corpus)
        scores = bm25.scores(query) if query else [0.0] * len(rest)

        # --- recency signal ---------------------------------------------------
        use_counts = Counter(self._canonical_name(name) for name in recent_tool_uses)

        # --- sticky signal ----------------------------------------------------
        sticky_set = {self._canonical_name(name) for name in sticky_tools}

        ranked: list[tuple[float, int, str, ToolDef]] = []
        for index, tool in enumerate(rest):
            name = tool.function.name
            canonical = self._canonical_name(name)
            recency = float(use_counts.get(canonical, 0))
            score = scores[index] + recency * 2.0
            if canonical in sticky_set:
                score += 100.0  # sticky tools dominate unless clearly irrelevant
            ranked.append((score, -index, name, tool))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)

        budget = max(0, self.max_non_pinned - len(pinned))
        chosen: list[ToolDef] = []
        chosen_names: list[str] = []
        for score, _, name, tool in ranked:
            if len(chosen) >= budget:
                break
            chosen.append(tool)
            chosen_names.append(name)
            result.scores[name] = score

        # Preserve the client's original tool order in the exposed list so that the
        # request hash stays comparable and the model sees a stable ordering.
        chosen_set = set(chosen_names)
        exposed = list(pinned) + [tool for tool in rest if tool.function.name in chosen_set]

        result.exposed = exposed
        result.selected = [tool.function.name for tool in exposed]
        result.hidden = [name for name in names if name not in set(result.selected)]
        return result

