"""Budgeted knowledge context; raw retrieval results remain unchanged.

The budget counts Unicode code points in snippet bodies only (Python ``len``),
not tokens, metadata, or serialized JSON. Prefix truncation is a final fallback;
this module does not summarize, extract evidence, re-rank, or paginate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.services.search import SearchResultItem


@dataclass
class ContextSnippet(SearchResultItem):
    """A copy of one retrieved chunk, optionally shortened at its end."""

    truncated: bool = False


@dataclass
class KnowledgeContext:
    snippets: list[ContextSnippet]
    # True for a partial body OR entirely omitted chunks.
    truncated: bool
    # Counts only whole omitted chunks, after identity deduplication.
    omittedChunks: int


def build_context(results: list[SearchResultItem], budget: int) -> KnowledgeContext:
    """Fill a body-character budget in retrieval order, retaining a final prefix.

    Deduplicate by document/version/chunk identity, keeping the first occurrence.
    Do not mutate or re-rank ``results``. Even when a body ends exactly at the
    budget, later omitted chunks make the overall context incomplete.
    """
    if type(budget) is not int or budget < 1:
        raise ValueError("context character budget must be a positive integer")

    candidates: list[SearchResultItem] = []
    seen: set[tuple[str, int, str]] = set()
    for item in results:
        key = (item.documentId, item.documentVersion, item.chunkId)
        if key not in seen:
            if not item.content:
                raise ValueError("context candidate body must not be empty")
            seen.add(key)
            candidates.append(item)

    snippets: list[ContextSnippet] = []
    remaining = budget
    for item in candidates:
        if remaining == 0:
            break
        content = item.content[:remaining]
        partial = len(content) < len(item.content)
        fields = asdict(item)
        fields["content"] = content
        snippets.append(ContextSnippet(**fields, truncated=partial))
        remaining -= len(content)

    omitted = len(candidates) - len(snippets)
    return KnowledgeContext(
        snippets=snippets,
        truncated=omitted > 0 or any(item.truncated for item in snippets),
        omittedChunks=omitted,
    )
