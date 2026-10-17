"""Concept candidates, bounded real-edge neighbors, deterministic score/diversity."""
from time import perf_counter
import re
from dataclasses import asdict
from lens.core.models import SearchHit
from lens.core.models import stable_id
from lens.search.semantics import relevance, words
from lens.search.ranking_signals import signals


def priority(hit):
    s = hit.ranking_signals
    return (int(s.get("relationship_match", False) or s.get("api_path_match", False) or s.get("ui_route_match", False)),
            int(s.get("exact_identifier", False)), int(s.get("source_scope_match", False)), int(s.get("entity_kind_match", False)))


def retrieve(query, understanding, ranked, store, project, top_k, provider=None):
    begun = perf_counter()
    topical = words(query) - {"what", "which", "how", "where", "when", "does", "do", "is", "are", "the", "a", "an", "and", "or", "to", "of", "for", "from", "in", "use", "uses", "used", "using", "implemented", "implementation", "provider", "service", "integration", "model", "models", "llm", "ai", "language", "generate", "content", "converted", "convert", "score", "1", "10", "through", "frontend", "backend", "python", "trace", "happens", "user", "submits", "form", "response", "database", "persistence", "calculation"}
    candidates = store.semantic_candidates(list(understanding.concepts), project, 60,
        integrations=any(i in understanding.intents for i in ("provider", "technology", "dependency", "integration")))
    expansions = list(understanding.expansions)
    expanded = []
    for expansion in expansions:
        expanded.extend(store.keyword_search(expansion, 30, project))
    semantic_vector = []
    if provider and expansions:
        vectors = provider.embed(expansions[:2])
        for vector in vectors:
            semantic_vector.extend(store.vector_search(vector, provider.model, 30, project, include_integrations=True))
    merged = {h.chunk.id: h for h in ranked}
    sources = {h.chunk.id: ["original"] for h in ranked}
    for branch, hits in (("concept_metadata", candidates), ("expanded_lexical", expanded), ("semantic_vector", semantic_vector)):
        for hit in hits:
            merged.setdefault(hit.chunk.id, SearchHit(hit.chunk, vector_score=hit.vector_score))
            sources.setdefault(hit.chunk.id, []).append(branch)
    # Expand only actual source relationships, capped at two hops, 64 nodes, five seeds.
    graph = store.evidence_graph([project])
    provisional = sorted(merged.values(), key=lambda h: (-min(2, len(topical & words(h.chunk.file + " " + h.chunk.name + " " + h.chunk.text))), -relevance(understanding, h.chunk)[0], h.chunk.id))
    vocabulary = list(dict.fromkeys(h.chunk.metadata.get("entity_name", h.chunk.name) for h in provisional[:3]))
    for term in vocabulary:
        for hit in store.keyword_search(term, 20, project):
            merged.setdefault(hit.chunk.id, SearchHit(hit.chunk))
            sources.setdefault(hit.chunk.id, []).append("repository_vocabulary")
    expansions.extend(vocabulary)
    seeds = [h.chunk.entity_id for h in provisional if h.chunk.entity_id in graph.nodes][:5]
    visited, frontier = set(seeds), seeds
    edges = []
    trails = {seed: ([graph.nodes[seed]], []) for seed in seeds}
    unresolved = []
    traversal_started = perf_counter()
    edge_inspections = 0
    allowed = {"USES_INTEGRATION", "REFERENCES_INTEGRATION", "DECLARES_INTEGRATION", "IMPORTS_MODULE", "USES_CONFIGURATION", "IMPORTS", "CALLS", "REFERENCES"}
    for depth in range(2):
        next_frontier = []
        for node in frontier:
            for direction, choices in (("forward", graph.outgoing.get(node, [])), ("reverse", graph.incoming.get(node, []))):
                for edge in choices:
                    if edge_inspections >= 256:
                        break
                    edge_inspections += 1
                    target = edge.target_id if direction == "forward" else edge.source_id
                    if edge.kind not in allowed:
                        continue
                    if not edge.target_id:
                        if len(unresolved) < 16:
                            unresolved.append({**asdict(edge), "direction": direction, "termination": "unresolved"})
                        continue
                    if target in visited:
                        continue
                    if len(visited) >= 64:
                        break
                    visited.add(target)
                    next_frontier.append(target)
                    edges.append({"source_id": edge.source_id, "target_id": edge.target_id, "kind": edge.kind,
                                  "direction": direction, "depth": depth + 1, "evidence": edge.evidence,
                                  "metadata": edge.metadata})
                    record = {**asdict(edge), "direction": direction,
                              "edge_id": stable_id(*(str(p) for p in graph.edge_key(edge)))}
                    previous_nodes, previous_edges = trails[node]
                    trails[target] = ([*previous_nodes, graph.nodes[target]], [*previous_edges, record])
        frontier = next_frontier
        if edge_inspections >= 256:
            break
    reconstruction_seconds = perf_counter() - traversal_started
    neighbors = store.chunks_for_entities(sorted(visited - set(seeds)), [project])
    for hit in neighbors:
        merged.setdefault(hit.chunk.id, hit)
        sources.setdefault(hit.chunk.id, []).append("resolved_neighbor")
    # Repository vocabulary is discovered from the best evidence, never a vendor dictionary.
    for hit in merged.values():
        score, evidence = relevance(understanding, hit.chunk)
        matched_subject = topical & words(hit.chunk.file + " " + hit.chunk.name + " " + hit.chunk.text)
        subject_factor = .1 + .9 * min(1, len(matched_subject) / min(2, len(topical))) if topical else 1
        score *= subject_factor
        evidence.update(semantic_score=score, matched_subject_terms=sorted(matched_subject))
        hit.ranking_signals = {**signals(query, hit.chunk), **evidence,
                               "candidate_sources": sorted(set(sources[hit.chunk.id]))}
        # Bounded equal fusion of structural relevance, semantic facts and original score.
        hit.hybrid_score = (score * 4 + hit.hybrid_score) / 5
        document_boost = 0.20 if hit.ranking_signals.get("document_topic_match") else 0.0
        hit.hybrid_score += document_boost
        hit.ranking_signals["document_topic_boost"] = document_boost
        # Large module aggregates are evidence context, rather than precise implementations.
        if hit.chunk.kind in {"module", "python_module"}:
            hit.hybrid_score /= 2
    ordered = sorted(merged.values(), key=lambda h: (*(-p for p in priority(h)), -h.hybrid_score, h.chunk.id))
    selected = []
    seen_roles = set()
    best_priority = priority(ordered[0]) if ordered else ()
    # Reserve one source-supported configuration, integration and implementation when relevant.
    def role(hit):
        if hit.chunk.metadata.get("source_type") == "documentation" or hit.chunk.language == "documentation":
            return "documentation"
        if hit.chunk.kind == "integration":
            return "integration"
        if hit.chunk.kind in {"constant", "variable"} and hit.chunk.metadata.get("semantic", {}).get("environment_names"):
            return "configuration"
        return "implementation"
    for wanted in ("documentation", "implementation", "configuration", "integration"):
        item = next((h for h in ordered if role(h) == wanted and
                     ((wanted == "documentation" and h.ranking_signals.get("document_topic_match")) or
                      (priority(h) == best_priority and
                       h.ranking_signals["semantic_score"] >= (ordered[0].ranking_signals["semantic_score"] / 2) and
                       (h.ranking_signals["matched_concepts"] or "dependency" in understanding.intents)))), None)
        if item is not None and len(selected) < top_k:
            selected.append(item)
            seen_roles.add((item.chunk.file, role(item)))
    # Model-selection questions need the actual declaration values, not merely
    # nearby inference code. This applies to any language and keeps exact scopes.
    if "model" in words(query):
        declarations = [h for h in ordered if h.chunk.kind in {"constant", "variable", "configuration"}
                        and "model" in words(h.chunk.name)
                        and re.search(r"[=:]\s*[\"']", h.chunk.text)
                        and h.ranking_signals.get("matched_concepts")
                        and priority(h) == best_priority]
        for item in declarations[:2]:
            if item not in selected and len(selected) < top_k:
                selected.append(item)
                seen_roles.add((item.chunk.file, role(item)))
    # Within identical deterministic priority and nearby semantic relevance, reserve source diversity.
    for hit in ordered:
        evidence_role = (hit.chunk.file, role(hit))
        if hit in selected or evidence_role in seen_roles:
            continue
        if hit.chunk.kind == "integration" and any(h.chunk.kind == "integration" and h.chunk.metadata.get("entity_name") == hit.chunk.metadata.get("entity_name") for h in selected):
            continue
        if selected and (priority(hit) != priority(ordered[0]) or hit.ranking_signals["semantic_score"] < ordered[0].ranking_signals["semantic_score"] / 2):
            continue
        selected.append(hit)
        seen_roles.add(evidence_role)
        if len(selected) == top_k:
            break
    for hit in ordered:
        if len(selected) == top_k:
            break
        if hit not in selected:
            selected.append(hit)
    for position, hit in enumerate(selected, 1):
        hit.rank = position
    selected_paths = [{"nodes": [asdict(n) for n in trails[h.chunk.entity_id][0]],
                       "relationships": trails[h.chunk.entity_id][1], "termination": "semantic_context",
                       "static_only": True}
                      for h in selected if h.chunk.entity_id in trails and trails[h.chunk.entity_id][1]][:8]
    return selected, ordered, {"understanding": understanding.describe(), "expansions": expansions,
        "repository_vocabulary": vocabulary[:5], "query_expansion_count": len(expansions),
        "candidate_count": len(merged), "relationships": edges, "neighbor_count": len(neighbors),
        "edge_inspections": edge_inspections, "truncated": edge_inspections >= 256 or len(visited) >= 64,
        "bounds": {"depth": 2, "nodes": 64, "edge_inspections": 256, "seeds": 5, "metadata_candidates": 60, "expanded_branch": 30, "lexical_queries": 7, "vector_queries": 2},
        "reconstruction_seconds": reconstruction_seconds, "semantic_stage_seconds": perf_counter() - begun,
        "selected_paths": selected_paths, "unresolved_endings": unresolved,
        "selected_evidence": [h.chunk.name for h in selected]}
