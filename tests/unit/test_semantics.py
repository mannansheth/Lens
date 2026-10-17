"""General concepts and literal source identities, deliberately using unfamiliar providers."""
from pathlib import Path
import pytest
from lens.repository.indexing import parse_repository
from lens.repository.knowledge import chunks
from lens.search.semantics import understand, relevance
from lens.core.project_context import select, save, active, state_path
from lens.core.models import stable_id


@pytest.mark.parametrize("query,concept", [
    ("What LLM is used?", "language_model"), ("What storage provider is used?", "storage"),
    ("Which service handles notifications?", "notifications"), ("How is audio converted to text?", "transcription"),
    ("Where is authentication implemented?", "authentication"), ("How does submission work?", "submission"),
])
def test_general_concepts(query, concept):
    result = understand(query)
    assert concept in result.concepts
    assert result.semantic
    assert len(result.expansions) <= 4
    assert result == understand(query)
    assert not any(vendor in " ".join(result.expansions).lower() for vendor in ("gemini", "cloudinary", "firebase"))


def test_identifier_structural_queries_preserve_phase3():
    result = understand("Which API receives Contact.submit's form request?")
    assert not result.semantic
    assert "contact.submit" in result.identifiers
    assert "REQUESTS" in result.relationship_kinds


def parse(tmp_path, source):
    (tmp_path / "service.js").write_text(source, encoding="utf-8")
    return parse_repository(tmp_path)


@pytest.mark.parametrize("binding", ["import client from 'novel-vendor';", "const client = require('novel-vendor');"])
def test_unknown_vendor_uses_are_source_bound(tmp_path, binding):
    knowledge = parse(tmp_path, binding + "\nexport function uploadAttachment(file) { return client.upload(file); }")
    provider = next(s for s in knowledge.symbols if s.kind == "integration")
    assert provider.name == "novel-vendor"
    edge = next(r for r in knowledge.relationships if r.kind == "USES_INTEGRATION" and r.evidence == "client.upload")
    assert edge.target_id == provider.id
    assert edge.metadata["static_only"] and edge.metadata["resolution"] == "resolved"
    docs = chunks(knowledge, "test")
    integration = next(d for d in docs if d.kind == "integration")
    assert "client.upload" in integration.embedding_text
    assert "uploadAttachment" in integration.embedding_text
    _, evidence = relevance(understand("What storage provider is used?"), next(d for d in docs if d.name.endswith("::uploadAttachment")))
    assert evidence["matched_concepts"] == ["storage"] and evidence["integration_evidence"]
    assert not evidence["external_host_evidence"]  # Package imports alone do not prove remote use.


@pytest.mark.parametrize("body", ["function send(client) { return client.send(); }",
    "function send() { const client = other; return client.send(); }",
    "function send() { client = other; return client.send(); }",
    "client = other; function send() { return client.send(); }",
    "function outer(client) { function send() { return client.send(); } }"])
def test_shadowed_or_mutated_provider_stays_unknown(tmp_path, body):
    k = parse(tmp_path, "import client from 'unfamiliar';\n" + body)
    owner = next(s for s in k.symbols if s.name == "send")
    assert not any(r.kind == "USES_INTEGRATION" and r.source_id == owner.id for r in k.relationships)


def test_url_host_template_and_reference_not_runtime(tmp_path):
    k = parse(tmp_path, "function x(id) { const url = `https://speech.vendor.test/v2/${id}?key=${process.env.KEY}`; return url; }")
    target = next(s for s in k.symbols if s.kind == "integration")
    assert target.name == "speech.vendor.test"
    edge = next(r for r in k.relationships if r.kind == "REFERENCES_INTEGRATION")
    assert "not proof" in edge.evidence
    assert edge.kind != "REQUESTS"
    assert not any("?key=" in s.source for s in k.symbols if s.kind == "integration")


def test_computed_require_and_unknown_host_not_guessed(tmp_path):
    k = parse(tmp_path, "const client = require(packageName); function f(host) { return fetch(`https://${host}/api`); }")
    assert not any(s.kind == "integration" for s in k.symbols)


def test_sdk_constructor_initialization_and_ambiguous_alias(tmp_path):
    k = parse(tmp_path, "import Client from 'previously-unknown-sdk'; const model = new Client({api_key:process.env.MODEL_KEY});")
    edge = next(r for r in k.relationships if r.kind == "USES_INTEGRATION" and r.evidence == "Client")
    assert edge.metadata["configuration_evidence"] and edge.target_id
    k = parse(tmp_path, "import client from 'one'; import client from 'two'; function use() { return client.upload(); }")
    assert not any(r.kind == "USES_INTEGRATION" for r in k.relationships)
    k = parse(tmp_path, "require = other; const client=require('not-proven'); function f(){ return client.upload(); }")
    assert not any(s.kind == "integration" for s in k.symbols)


def test_relative_configuration_link_and_semantic_invalidation(tmp_path):
    (tmp_path / "config.js").write_text("const client = require('new-provider'); client.config({}); module.exports=client;")
    source = "const configured = require('./config'); function uploadFile(x) { return configured.upload(x); }"
    k = parse(tmp_path, source)
    edge = next(r for r in k.relationships if r.kind == "USES_CONFIGURATION")
    assert edge.target_id
    original = next(d for d in chunks(k, "test") if d.kind == "integration").embedding_text
    (tmp_path / "config.js").write_text("const client = require('replacement-provider'); client.config({}); module.exports=client;")
    k2 = parse_repository(tmp_path)
    changed = next(d for d in chunks(k2, "test") if d.kind == "integration").embedding_text
    assert original != changed
    assert "new-provider" not in changed


def project(path):
    return {"id": stable_id(str(path.resolve())), "root": str(path.resolve())}


def test_context_explicit_cwd_active_and_longest_root(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_STATE_DIR", str(tmp_path / "state"))
    roots = [project(tmp_path / "one"), project(tmp_path / "one" / "nested"), project(tmp_path / "two")]
    save(project(tmp_path / "inactive"), roots)
    assert active() not in {root["id"] for root in roots}
    assert select(roots, cwd=tmp_path / "one" / "nested" / "src") == roots[1]
    save(roots[2], roots)
    assert active() == roots[2]["id"]
    assert select(roots, cwd=tmp_path / "one") == roots[2]
    assert select(roots, explicit=tmp_path / "two", cwd=tmp_path / "one") == roots[2]
    assert select(roots, cwd=tmp_path / "elsewhere") == roots[2]
    assert state_path().exists()
    save(roots[0], roots)
    assert active() == roots[0]["id"]


def test_context_errors_and_no_state_inside_source(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_STATE_DIR", str(tmp_path / "state"))
    roots = [project(tmp_path / "source")]
    with pytest.raises(ValueError, match="not indexed"):
        select(roots, explicit=tmp_path / "missing")
    with pytest.raises(ValueError, match="Current directory"):
        select(roots, cwd=tmp_path)
    monkeypatch.setenv("LENS_STATE_DIR", str(tmp_path / "source" / ".lens"))
    with pytest.raises(ValueError, match="outside"):
        save(roots[0], roots)
    assert not (tmp_path / "source").exists()


def test_corrupt_context_is_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_STATE_DIR", str(tmp_path))
    state_path().write_text("invalid")
    with pytest.raises(ValueError, match="Invalid local"):
        active()


def test_runtime_contains_no_repository_provider_dictionary():
    root = Path(__file__).resolve().parents[2] / "src/lens"
    files = [root / name for name in ("search/semantics.py", "search/semantic_retrieval.py", "repository/integrations.py")]
    assert not any(term in path.read_text().lower() for path in files for term in ("sample_repository", "cloudinary", "gemini", "firebase"))


def test_semantic_neighbors_cycles_unresolved_and_source_priority():
    from lens.search.evidence import EvidenceGraph, EvidenceNode
    from lens.core.models import DocumentChunk, SearchHit, Relationship
    from lens.search.semantic_retrieval import retrieve
    nodes = {str(i): EvidenceNode(str(i), f"upload{i}", "function", "backend/service.js", 1, 2, "backend") for i in range(80)}
    edges = [Relationship("0", "CALLS", f"upload{i}", str(i)) for i in range(1, 80)]
    edges += [Relationship("1", "CALLS", "upload0", "0"), Relationship("0", "CALLS", "unknown")]
    graph = EvidenceGraph(nodes, edges)
    def hit(identifier, file="backend/service.js"):
        return SearchHit(DocumentChunk(identifier, identifier, f"upload{identifier}", "function", file, "javascript", "source",
            {"entity_name": f"upload{identifier}", "semantic": {"concepts": ["storage"], "actions": ["client.upload"], "integrations": ["literal-package"]}}))
    calls = []
    class Store:
        def semantic_candidates(self, concepts, project, limit, integrations):
            assert project == "selected" and limit <= 60
            return [hit("0")]
        def keyword_search(self, query, limit, project):
            calls.append(query)
            assert project == "selected" and limit <= 30
            return []
        def evidence_graph(self, projects):
            assert projects == ["selected"]
            return graph
        def chunks_for_entities(self, identifiers, projects):
            assert len(identifiers) <= 64 and projects == ["selected"]
            return [hit(i) for i in identifiers]
    query = "Which frontend function uploads files to a storage provider?"
    preferred = hit("2", "frontend/service.js")
    result, ordered, debug = retrieve(query, understand(query), [preferred], Store(), "selected", 5)
    assert result[0].chunk.file.startswith("frontend")
    assert len(calls) <= 7 and debug["neighbor_count"] <= 64
    assert debug["edge_inspections"] <= 256
    assert all(r["depth"] <= 2 and r["source_id"] != r["target_id"] for r in debug["relationships"])
    assert any(e["target_name"] == "unknown" and e["target_id"] is None for e in debug["unresolved_endings"])
    assert all(len({n["id"] for n in p["nodes"]}) == len(p["nodes"]) for p in debug["selected_paths"])
    assert len(ordered) <= 64
