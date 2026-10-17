from lens.evaluation.chain_evaluation import evaluate_chains
from lens.search.evidence import EvidenceGraph, EvidenceNode
from lens.core.models import DocumentChunk, Relationship, SearchHit
from lens.search.retrieval import SearchResponse


def fixture():
    graph = EvidenceGraph({
        "a": EvidenceNode("a", "Page", "component", "frontend/Page.jsx", 1, 2, "frontend"),
        "b": EvidenceNode("b", "GET /api/items", "endpoint", "backend/app.js", 1, 2, "api"),
        "c": EvidenceNode("c", "items", "table", "schema.sql", 1, 2, "database"),
    }, [Relationship("a", "REQUESTS", "GET /api/items", "b"),
        Relationship("b", "READS", "items", "c"), Relationship("a", "REQUESTS", "GET /missing")])
    paths = graph.reconstruct("a")["paths"]
    hits = [SearchHit(DocumentChunk("a", "a", "Page", "component", "frontend/Page.jsx", "jsx", "")),
            SearchHit(DocumentChunk("c", "c", "items", "table", "schema.sql", "mysql", ""))]
    response = SearchResponse(hits, {"retrieval_seconds": 0.01,
        "cross_layer": {"selected_paths": paths, "candidate_paths": 2, "reconstruction_seconds": 0.001}})
    labels = [{"question": "show chain", "expected": ["Page", "GET /api/items", "items"],
        "expected_paths": [[{"source": "Page", "kind": "REQUESTS", "target": "GET /api/items"},
                            {"source": "GET /api/items", "kind": "READS", "target": "items"}]],
        "expected_unresolved": [{"source": "Page", "kind": "REQUESTS", "target_name": "GET /missing"}]}]
    return graph, response, labels


def test_metrics_known_values_and_no_optimistic_unknown_judgments():
    graph, response, labels = fixture()
    report = evaluate_chains(labels, lambda _: response, 5, graph)
    assert report["entity_recall_at_k"] == 2/3
    assert report["chain_recall"] == 1
    assert report["hop_coverage"] == 1
    assert report["evidence_diversity"] == 2/3
    assert report["unresolved_edge_accuracy"] == 1
    assert report["path_precision"] == 1
    assert report["false_relationship_rate"] == 0
    assert report["performance"]["candidate_paths"] == 2


def test_partial_chain_and_incorrect_resolution_are_penalized():
    graph, response, labels = fixture()
    response.debug["cross_layer"]["selected_paths"] = [next(path for path in graph.reconstruct("a", max_depth=1)["paths"] if path["nodes"][-1]["id"] == "b")]
    graph.edges[-1].target_id = "b"
    report = evaluate_chains(labels, lambda _: response, 5, graph)
    assert report["chain_recall"] == 0
    assert report["hop_coverage"] == 0.5
    assert report["unresolved_edge_accuracy"] == 0


def test_vector_baseline_has_no_chains_and_metrics_are_not_fabricated():
    graph, response, labels = fixture()
    response.debug["cross_layer"] = {}
    report = evaluate_chains(labels, lambda _: response, 5, graph)
    assert report["chain_recall"] == 0 and report["hop_coverage"] == 0
    assert report["path_precision"] is None
    assert report["path_judgment_coverage"] is None


def test_false_relationship_rate_counts_distinct_judged_relationships():
    graph, response, labels = fixture()
    graph.edges[-1].target_id = "b"
    response.debug["cross_layer"]["selected_paths"] = graph.reconstruct("a")["paths"]
    report = evaluate_chains(labels, lambda _: response, 5, graph)
    assert report["judged_relationships"] == 3
    assert report["false_relationship_rate"] == 1/3
    assert report["path_precision"] < 1
