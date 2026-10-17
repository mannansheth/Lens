import pytest
from lens.core.models import DocumentChunk, SearchHit
from lens.search.ranking import rank
from lens.search.retrieval import search, select_with_document_evidence
from lens.evaluation import evaluate
from lens.search.embeddings import validate_vectors


def hit(name, keyword=0, vector=0):
    return SearchHit(
        DocumentChunk(name, name, name, "method", "x.cs", "csharp", "text"), keyword, vector
    )


def test_fusion_normalizes_and_unions():
    keyword = [hit("a", 20), hit("b", 10)]
    vector = [hit("b", vector=0.9), hit("c", vector=-0.2)]
    result = rank(keyword, vector)
    assert [h.chunk.name for h in result] == ["b", "a", "c"]
    assert result[0].hybrid_score == pytest.approx(0.7)
    assert result[0].keyword_score == 0.5
    assert result[2].vector_score == 0
    assert keyword[0].keyword_score == 20  # Trace retains raw evidence.
    assert [h.rank for h in result] == [1, 2, 3]


def test_alpha_extremes_and_ties():
    assert rank([hit("a", 2)], [hit("b", vector=1)], 1)[0].chunk.name == "a"
    assert rank([hit("a", 2)], [hit("b", vector=1)], 0)[0].chunk.name == "b"
    assert [h.chunk.name for h in rank([], [hit("b", vector=1), hit("a", vector=1)])] == ["a", "b"]
    assert rank([], []) == []
    with pytest.raises(ValueError):
        rank([], [], 2)


class Store:
    def keyword_search(self, query, limit, project):
        assert query == "pending amount"
        return [hit("a", 2)]

    def vector_search(self, vector, model, limit, project):
        return [hit("a", vector=0.8), hit("b", vector=0.9)]


class Provider:
    model = "test"
    dimension = 3

    def embed(self, texts):
        return [[1, 0, 0] for text in texts]


def test_debug_trace_and_keyword_without_model():
    response = search("  pending  amount  ", Store(), Provider(), top_k=1)
    assert response.results[0].hybrid_score == 0.9
    assert response.debug["normalized_query"] == "pending amount"
    assert response.debug["candidate_merging"]["unique"] == 2
    assert response.debug["keyword_results_raw"][0]["keyword_score"] == 2
    assert search("pending amount", Store(), None, mode="keyword").results[0].hybrid_score == 1


@pytest.mark.parametrize("query,k", [("", 5), ("ok", 0), ("ok", 101)])
def test_invalid_search(query, k):
    with pytest.raises(ValueError):
        search(query, Store(), Provider(), top_k=k)


def test_recall_precision_use_labeled_sets():
    result = evaluate(
        [{"question": "test", "expected": ["a", "b"]}], lambda q: [hit("a"), hit("c"), hit("d")], 3
    )
    assert result["mean_recall_at_k"] == 0.5
    assert result["mean_precision_at_k"] == pytest.approx(1 / 3)


@pytest.mark.parametrize(
    "vectors,count,dimension",
    [([], 1, 3), ([[0, 0, 0]], 1, 3), ([[float("nan"), 1, 0]], 1, 3), ([[1, 0]], 1, 3)],
)
def test_embedding_contract(vectors, count, dimension):
    with pytest.raises(ValueError):
        validate_vectors(vectors, count, dimension)


def test_exact_identifier_and_relationship_priority_explain_scores():
    definition = hit("InvoiceTable", keyword=2, vector=0.9)
    definition.chunk.metadata["entity_name"] = "InvoiceTable"
    caller = hit("InvoicePage", keyword=1, vector=0.5)
    caller.chunk.metadata["relation_evidence"] = [{"direction":"outgoing", "kind":"RENDERS",
        "target_name":"InvoiceTable", "target_id":"known", "confidence":"high"}]
    assert rank([definition, caller], [], query="Where is InvoiceTable defined?")[0].chunk.name == "InvoiceTable"
    results = rank([definition, caller], [], query="What renders InvoiceTable?")
    assert results[0].chunk.name == "InvoicePage"
    assert results[0].ranking_signals["relationship_match"]
    assert results[0].hybrid_score < results[1].hybrid_score  # categorical priority is visible
    assert rank([], [definition, caller], mode="vector", query="What renders InvoiceTable?")[0].chunk.name == "InvoiceTable"


def test_api_signal_matching_method_and_dynamic_route_shape():
    from lens.search.ranking_signals import signals
    item = hit("save").chunk
    item.metadata["requests"] = [{"resolved_path":"/api/projects/{dynamic}", "method":"PUT"}]
    assert signals("What frontend function calls PUT /api/projects/:id?", item)["api_path_match"]
    assert not signals("What frontend function calls DELETE /api/projects/:id?", item)["api_path_match"]


def test_category_evaluation_and_invalid_labels():
    result = evaluate([{"question":"q", "category":"component", "expected":["a"]}], lambda q: [hit("b")], 5)
    assert result["zero_recall_questions"] == 1
    assert result["categories"]["component"]["count"] == 1
    with pytest.raises(ValueError):
        evaluate([{"question":"q", "expected":"not a list"}], lambda q: [], 5)


def test_explicit_kind_scope_and_ui_route_are_visible():
    from lens.search.ranking_signals import signals
    component = hit("App").chunk
    component.kind = "component"
    component.file = "frontend/App.jsx"
    component.metadata["routes"] = [{"path":"/work/:slug", "renders":["Project"]}]
    evidence = signals("Which frontend component handles /work/:slug?", component)
    assert evidence["ui_route_match"]
    assert evidence["entity_kind_match"] and evidence["source_scope_match"]
    assert not signals("Which backend function handles /work/:slug?", component)["source_scope_match"]


def test_best_topic_document_replaces_a_weaker_document_reservation():
    def document(name, score, hybrid):
        value = hit(name, keyword=hybrid)
        value.chunk.language = "documentation"
        value.chunk.metadata.update(source_type="documentation", document_topic_match=True,
                                    document_topic_score=score)
        value.hybrid_score = hybrid
        value.ranking_signals.update(document_topic_match=True, document_topic_score=score)
        return value

    code = hit("unrelated code", keyword=10)
    weak = document("Frontend dependency audit", 1, 0.5)
    exact = document("Dependencies and maintenance", 3, 0.2)
    selected = select_with_document_evidence([code, weak, hit("other", keyword=1)], 3,
                                              [code, weak, exact])
    assert exact in selected
    assert weak not in selected


def test_feature_questions_rank_routed_react_pages_using_visible_text():
    from lens.search.ranking_signals import signals
    profile = hit("frontend/src/pages/ProfileSettings.jsx::ProfileSettings").chunk
    profile.kind = "component"
    profile.metadata.update(navigation_candidate=True, incoming_routes=[{"path": "/profile"}],
                            ui_text=["Profile settings", "Current password", "Confirm password change"])
    result = signals("How do I change my password?", profile)
    assert result["navigation_query"]
    assert result["navigation_evidence"]
    assert result["navigation_route_feature_score"] > 0


def test_client_navigation_matches_semantic_feature_terms_to_api_paths():
    from lens.search.ranking_signals import signals
    endpoint = hit("POST /book").chunk
    endpoint.kind = "endpoint"
    endpoint.metadata.update(navigation_candidate=True,
                             endpoint={"method": "POST", "path": "/book"})
    result = signals("How do users book an appointment with a therapist?", endpoint)
    assert result["navigation_query"]
    assert result["navigation_route_feature_score"] > 0


def test_whole_application_overview_does_not_expand_arbitrary_code_paths():
    from lens.search.multihop import intent
    result = intent("Describe the components and data flow across the whole application")
    assert result["broad_overview"]
    assert not result["expand"]


def test_api_query_punctuation_is_not_part_of_url():
    from lens.search.ranking_signals import signals
    endpoint = hit("GET /api/items").chunk
    endpoint.metadata["endpoint"] = {"path": "/api/items", "method": "GET"}
    assert signals("Which handler handles GET /api/items.", endpoint)["api_path_match"]


def test_disabled_expansion_and_invalid_depth_preserve_contract():
    response = search("pending amount", Store(), Provider(), multi_hop=False)
    assert response.debug["cross_layer"]["enabled"] is False
    with pytest.raises(ValueError, match="max-depth"):
        search("pending amount", Store(), Provider(), max_depth=0)
