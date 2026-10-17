"""Independent retrieval branches plus inspectable ranking trace."""

from dataclasses import dataclass
from time import perf_counter
import re
from lens.database.store import RetrievalStore
from lens.search.embeddings import EmbeddingProvider
from lens.core.models import SearchHit
from lens.search.ranking import rank
from lens.core.logging import event


def describe(hit: SearchHit) -> dict:
    return {
        "rank": hit.rank,
        "id": hit.chunk.id,
        "item": hit.chunk.name,
        "item_type": hit.chunk.kind,
        "source": hit.chunk.file,
        "line": hit.chunk.metadata.get("line_start"),
        "keyword_score": hit.keyword_score,
        "vector_score": hit.vector_score,
        "hybrid_score": hit.hybrid_score,
        "ranking_signals": hit.ranking_signals,
    }


def select_with_document_evidence(ranked: list[SearchHit], top_k: int,
                                  candidates: list[SearchHit] | None = None) -> list[SearchHit]:
    """Reserve one directly topic-matched project document without dropping code context."""
    selected = list(ranked[:top_k])
    if top_k > 1 and not any(hit.ranking_signals.get("document_topic_match") for hit in selected):
        document = next((hit for hit in (candidates or ranked)
                         if hit.ranking_signals.get("document_topic_match")), None)
        if document is not None:
            selected = [*selected[:1], document, *selected[1:top_k - 1]]
    for position, hit in enumerate(selected, 1):
        hit.rank = position
    return selected


@dataclass
class SearchResponse:
    results: list[SearchHit]
    debug: dict


def search(
    query: str,
    store: RetrievalStore,
    provider: EmbeddingProvider | None,
    top_k: int = 5,
    alpha: float = 0.5,
    mode: str = "hybrid",
    project: str | None = None,
    multi_hop: bool = True,
    max_depth: int = 8,
    semantic: bool = True,
) -> SearchResponse:
    begun = perf_counter()
    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("Search query cannot be empty")
    if not 1 <= top_k <= 100:
        raise ValueError("top-k must be between 1 and 100")
    if mode not in {"keyword", "vector", "hybrid"}:
        raise ValueError("Unknown retrieval mode")
    if not 1 <= max_depth <= 16:
        raise ValueError("max-depth must be between 1 and 16")
    # Navigation questions often match their destination weakly in lexical
    # search (the evidence lives in a Razor button/script), while the top
    # semantic candidates contain generic pages sharing a broad feature word.
    # Retrieve a wider candidate pool only for these questions so route-aware
    # ranking can surface the actual UI/action evidence.
    navigation_query = bool(re.search(
        r"\b(page|screen|menu|navigate|navigation|access|open|find|where|edit|view|export|invoice|journal|employee|payroll|billing)\b",
        normalized, re.I))
    limit = max(150, top_k * 8) if navigation_query else max(60, top_k * 5)
    keyword = store.keyword_search(normalized, limit, project) if mode != "vector" else []
    vector = []
    if mode != "keyword":
        if provider is None:
            raise ValueError("Vector retrieval requires an embedding provider")
        query_vector = provider.embed([normalized])[0]
        vector = store.vector_search(query_vector, provider.model, limit, project)
    ranked = rank(keyword, vector, alpha, mode, normalized)
    original = [describe(hit) for hit in ranked]
    final = select_with_document_evidence(ranked, top_k)
    from lens.search.semantics import understand
    understanding = understand(normalized)
    # Capitalized natural-language subjects are not necessarily indexed symbols.
    # Keep exact identifier queries on their structural path; permit concept search
    # when none of the original candidates actually matches an identifier.
    if understanding.concepts and not understanding.semantic and not any(
            hit.ranking_signals.get("exact_identifier") for hit in ranked):
        from dataclasses import replace
        understanding = replace(understanding, semantic=True)
    semantic_trace = {"understanding": understanding.describe(), "enabled": False,
                      "query_expansion_count": 0, "candidate_count": len(ranked),
                      "disabled_reason": "disabled_by_option" if not semantic else
                      "vector_baseline" if mode == "vector" else
                      "store_unsupported" if not hasattr(store, "semantic_candidates") else
                      "exact_or_ordinary_query"}
    if semantic and understanding.semantic and mode != "vector" and hasattr(store, "semantic_candidates"):
        from lens.search.semantic_retrieval import retrieve
        final, ranked, semantic_trace = retrieve(normalized, understanding, ranked, store, project, top_k,
                                               provider if mode == "hybrid" else None)
        final = select_with_document_evidence(final, top_k, ranked)
        semantic_trace["enabled"] = True
        semantic_trace["disabled_reason"] = None
    from lens.search.multihop import expand, intent
    query_intent = intent(normalized)
    expansion = {"intent": query_intent, "enabled": False,
                 "disabled_reason": "disabled_by_option" if not multi_hop else
                 "vector_baseline" if mode == "vector" else
                 "ordinary_query" if not query_intent["expand"] else
                 "store_unsupported" if not hasattr(store, "evidence_graph") else "no_candidates"}
    # Vector-only remains a pure cosine baseline. Stores without graph support still work.
    semantic_flow = not semantic_trace.get("enabled") or "data_flow" in understanding.intents or len(query_intent.get("requested_layers", [])) > 1
    if multi_hop and mode != "vector" and query_intent["expand"] and ranked and semantic_flow and hasattr(store, "evidence_graph"):
        projects = [project] if project else store.project_ids_for_entities([hit.chunk.entity_id for hit in ranked])
        graph = store.evidence_graph(projects)
        final, expansion = expand(normalized, ranked, graph,
                                 lambda ids: store.chunks_for_entities(ids, projects), top_k, max_depth)
        expansion["enabled"] = True
        expansion["disabled_reason"] = None
    trace = {
        "original_query": query,
        "normalized_query": normalized,
        "selected_languages": sorted({h.chunk.language for h in final}),
        "selected_files": sorted({h.chunk.file for h in final}),
        "mode": mode,
        "alpha": alpha,
        "ranking_order": ["relationship_or_api_or_ui_route_match", "exact_identifier", "explicit_source_scope", "explicit_entity_kind", "hybrid_score_plus_document_topic_boost", "stable_id"] if mode != "vector" else ["vector_score", "stable_id"],
        "candidate_limit_per_branch": limit,
        "keyword_results_raw": [describe(h) for h in keyword],
        "vector_results_raw": [describe(h) for h in vector],
        "candidate_merging": {
            "keyword": len(keyword),
            "vector": len(vector),
            "unique": len(ranked),
        },
        "normalized_and_hybrid_scores": original,
        "considered_candidates": [describe(hit) for hit in ranked],
        "cross_layer": expansion,
        "semantic": semantic_trace,
        "final_ranking": [describe(h) for h in final],
        "retrieval_seconds": perf_counter() - begun,
    }
    event("search_ranked", mode=mode, candidates=len(ranked), returned=min(top_k, len(ranked)))
    return SearchResponse(final, trace)
