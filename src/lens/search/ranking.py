"""Explicit candidate union and rank-based fusion of lexical/vector branches."""

from lens.core.models import SearchHit
from lens.search.ranking_signals import signals


RRF_CONSTANT = 60


def rank(
    keyword: list[SearchHit], vector: list[SearchHit], alpha: float = 0.5, mode: str = "hybrid", query: str | None = None
) -> list[SearchHit]:
    if not 0 <= alpha <= 1:
        raise ValueError("alpha must be between 0 and 1")
    maximum = max((h.keyword_score for h in keyword), default=0.0)
    merged: dict[str, SearchHit] = {}
    keyword_ranks: dict[str, int] = {}
    vector_ranks: dict[str, int] = {}
    for position, hit in enumerate(keyword, 1):
        item = merged.setdefault(hit.chunk.id, SearchHit(hit.chunk))
        item.keyword_score = max(0.0, hit.keyword_score) / maximum if maximum > 0 else 0.0
        keyword_ranks.setdefault(hit.chunk.id, position)
    for position, hit in enumerate(vector, 1):
        item = merged.setdefault(hit.chunk.id, SearchHit(hit.chunk))
        item.vector_score = min(1.0, max(0.0, hit.vector_score))
        vector_ranks.setdefault(hit.chunk.id, position)
    available_weight = (alpha if keyword_ranks else 0.0) + (1.0 - alpha if vector_ranks else 0.0)
    for item in merged.values():
        if mode == "keyword":
            item.hybrid_score = item.keyword_score
        elif mode == "vector":
            item.hybrid_score = item.vector_score
        else:
            # Lexical rank scores and embedding cosine scores are different
            # quantities. Fuse their positions, rather than averaging the raw
            # values and allowing one branch's score scale to dominate.
            lexical = ((RRF_CONSTANT + 1) / (RRF_CONSTANT + keyword_ranks[item.chunk.id])
                       if item.chunk.id in keyword_ranks else 0.0)
            semantic = ((RRF_CONSTANT + 1) / (RRF_CONSTANT + vector_ranks[item.chunk.id])
                        if item.chunk.id in vector_ranks else 0.0)
            item.hybrid_score = ((alpha * lexical + (1.0 - alpha) * semantic) / available_weight
                                 if available_weight else 0.0)
        if query is not None and mode != "vector":
            item.ranking_signals = signals(query, item.chunk)
    result = sorted(merged.values(), key=lambda h: (
        -int(h.ranking_signals.get("relationship_match", False) or h.ranking_signals.get("api_path_match", False) or h.ranking_signals.get("ui_route_match", False)),
        -int(h.ranking_signals.get("exact_identifier", False)),
        -int(h.ranking_signals.get("source_scope_match", False)),
        -int(h.ranking_signals.get("entity_kind_match", False)),
        -h.hybrid_score, h.chunk.id))
    for position, item in enumerate(result, 1):
        item.rank = position
    return result
