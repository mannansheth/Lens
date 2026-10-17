"""Explicit candidate union, normalization, and weighted score fusion."""

from lens.core.models import SearchHit
from lens.search.ranking_signals import signals


def rank(
    keyword: list[SearchHit], vector: list[SearchHit], alpha: float = 0.5, mode: str = "hybrid", query: str | None = None
) -> list[SearchHit]:
    if not 0 <= alpha <= 1:
        raise ValueError("alpha must be between 0 and 1")
    maximum = max((h.keyword_score for h in keyword), default=0.0)
    merged: dict[str, SearchHit] = {}
    for hit in keyword:
        item = merged.setdefault(hit.chunk.id, SearchHit(hit.chunk))
        item.keyword_score = max(0.0, hit.keyword_score) / maximum if maximum > 0 else 0.0
    for hit in vector:
        item = merged.setdefault(hit.chunk.id, SearchHit(hit.chunk))
        item.vector_score = min(1.0, max(0.0, hit.vector_score))
    for item in merged.values():
        item.hybrid_score = (
            item.keyword_score
            if mode == "keyword"
            else item.vector_score
            if mode == "vector"
            else alpha * item.keyword_score + (1 - alpha) * item.vector_score
        )
        if query is not None and mode != "vector":
            item.ranking_signals = signals(query, item.chunk)
            item.ranking_signals["document_topic_boost"] = (
                0.20 if item.ranking_signals.get("document_topic_match") else 0.0
            )
            item.ranking_signals["navigation_match_boost"] = min(
                0.48, item.ranking_signals.get("navigation_match_score", 0.0) * 0.12)
            item.ranking_signals["navigation_route_feature_boost"] = min(
                0.90, item.ranking_signals.get("navigation_route_feature_score", 0.0) * 0.22)
    result = sorted(merged.values(), key=lambda h: (
        -int(h.ranking_signals.get("relationship_match", False) or h.ranking_signals.get("api_path_match", False) or h.ranking_signals.get("ui_route_match", False) or
             (h.ranking_signals.get("navigation_evidence", False) and
              (h.ranking_signals.get("navigation_route_feature_score", 0) > 0 or
               h.ranking_signals.get("navigation_match", False)))),
        -int(h.ranking_signals.get("exact_identifier", False)),
        -int(h.ranking_signals.get("source_scope_match", False)),
        -int(h.ranking_signals.get("entity_kind_match", False)),
        -(h.hybrid_score + h.ranking_signals.get("document_topic_boost", 0.0) +
          h.ranking_signals.get("navigation_match_boost", 0.0) +
          h.ranking_signals.get("navigation_route_feature_boost", 0.0)), h.chunk.id))
    for position, item in enumerate(result, 1):
        item.rank = position
    return result
