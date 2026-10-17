"""Opt-in real persistence tests. Each test owns a temporary project only."""

import os
from pathlib import Path
import pytest
from lens.database.store import PostgresStore
from lens.repository.indexing import index
from lens.core.models import stable_id

pytestmark = pytest.mark.integration


class Provider:
    model = "test-only-three-dimensional"
    dimension = 3
    calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[1.0, 0.0, 0.0] for _ in texts]


@pytest.fixture
def store(tmp_path):
    url = os.getenv("LENS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set LENS_TEST_DATABASE_URL to enable PostgreSQL integration tests")
    store = PostgresStore(url)
    store.initialize()
    yield store
    with store.connection() as conn:
        conn.execute("DELETE FROM projects WHERE id=%s", (stable_id(str(tmp_path.resolve())),))


def test_index_duplicate_updates_deletes_and_search(store, tmp_path):
    path = tmp_path / "code.cs"
    path.write_text("class BillingMaster { public int CalculatePendingAmount() { return 5; } }")
    provider = Provider()
    assert index(tmp_path, store, provider)["status"] == "indexed"
    first = store.inspect(stable_id(str(tmp_path.resolve())))
    assert index(tmp_path, store, provider)["status"] == "unchanged"
    assert provider.calls == 1
    project = stable_id(str(tmp_path.resolve()))
    assert store.keyword_search("CalculatePendingAmount", 5, project)[0].chunk.name.endswith(
        "CalculatePendingAmount"
    )
    assert store.vector_search([1, 0, 0], provider.model, 5, project)[
        0
    ].vector_score == pytest.approx(1)
    assert first["counts"]["chunks"] == 2
    path.write_text("class Changed { public void NewMethod() {} }")
    assert index(tmp_path, store, provider)["status"] == "indexed"
    assert not store.keyword_search("CalculatePendingAmount", 5, project)
    path.unlink()
    with pytest.raises(ValueError, match="existing index preserved"):
        index(tmp_path, store, provider)
    assert store.inspect(project)["counts"]["chunks"] == 2


def test_semantic_incremental_invalidation_isolation_and_revision(store, tmp_path, monkeypatch):
    from lens.search.retrieval import search
    from lens.core.project_context import select, save
    from lens.database import store as storage
    import lens.repository.indexing as indexing
    project = stable_id(str(tmp_path.resolve()))
    provider = Provider()
    source = tmp_path / "integration.js"
    source.write_text("import client from 'unseen-service'; export function uploadFile(x) { return client.upload(x); }")
    first = index(tmp_path, store, provider)
    assert first["embeddings_generated"] > 0
    assert index(tmp_path, store, provider)["embeddings_generated"] == 0
    initial = store.semantic_candidates(["storage"], project)
    assert any(h.chunk.kind == "integration" for h in initial)
    monkeypatch.setenv("LENS_STATE_DIR", str(tmp_path.parent / (tmp_path.name + "-context")))
    projects = store.projects()
    save(select(projects, tmp_path), projects)
    assert select(projects, cwd=tmp_path / "src")["id"] == project
    with pytest.raises(ValueError, match="exactly one"):
        store.keyword_search("upload", 5)
    with pytest.raises(ValueError, match="exactly one"):
        store.vector_search([1, 0, 0], provider.model, 5)
    response = search("What storage provider is used?", store, None, project=project, mode="keyword")
    assert any(h.chunk.name.endswith("::uploadFile") for h in response.results)
    assert response.debug["semantic"]["neighbor_count"] <= 64
    assert response.debug["semantic"]["query_expansion_count"] <= 7
    assert not store.semantic_candidates(["storage"], "unindexed-project")
    old_hashes = {h.chunk.name: h.chunk.metadata["embedding_hash"] for h in initial}
    source.write_text("import client from 'replacement-service'; export function uploadFile(x) { return client.upload(x); }")
    changed = index(tmp_path, store, provider)
    assert changed["changed_files"] == 1 and changed["embeddings_generated"] > 0
    updated = store.semantic_candidates(["storage"], project)
    assert not any("unseen-service" in h.chunk.text for h in updated)
    assert any(h.chunk.name not in old_hashes or old_hashes[h.chunk.name] != h.chunk.metadata["embedding_hash"] for h in updated)
    added = tmp_path / "more.js"
    added.write_text("import client from 'other-service'; export function transcribeAudio(x) { return client.transcribe(x); }")
    assert index(tmp_path, store, provider)["added_files"] == 1
    assert store.semantic_candidates(["transcription"], project)
    added.unlink()
    deleted = index(tmp_path, store, provider)
    assert deleted["deleted_files"] == 1
    assert not store.semantic_candidates(["transcription"], project)
    graph = store.evidence_graph([project])
    assert not any("other-service" in n.name for n in graph.nodes.values())
    monkeypatch.setattr(storage, "PIPELINE_REVISION", "phase4-test-revision")
    monkeypatch.setattr(indexing, "PIPELINE_REVISION", "phase4-test-revision")
    assert index(tmp_path, store, provider)["embeddings_generated"] > 0


def test_model_mismatch_and_atomic_rollback(store, tmp_path):
    (tmp_path / "a.cs").write_text("class Stable {}")
    index(tmp_path, store, Provider())
    project = stable_id(str(tmp_path.resolve()))
    with pytest.raises(ValueError, match="differs"):
        store.check_model("wrong", 3, project)
    from lens.core.models import Knowledge, CodeFile

    bad = Knowledge(
        files=[
            CodeFile("duplicate", "a.cs", "csharp", "h", 1),
            CodeFile("duplicate", "b.cs", "csharp", "h", 1),
        ]
    )
    with pytest.raises(RuntimeError):
        store.replace(Path(tmp_path), bad, [], [], "broken", 3)
    assert store.inspect(project)["items"][0]["qualified_name"] == "Stable"


def test_incremental_reuses_unchanged_inputs_and_resolves_changed_targets(store, tmp_path):
    (tmp_path / "stable.js").write_text("export function stable(){return 1;}")
    (tmp_path / "service.js").write_text("export function load(){return 2;}")
    (tmp_path / "page.jsx").write_text("import {load} from './service'; export function Page(){load();return <div/>;}")
    provider = Provider()
    initial = index(tmp_path, store, provider)
    assert initial["embeddings_reused"] == 0
    (tmp_path / "service.js").write_text("export function load(){return 3;}")
    changed = index(tmp_path, store, provider)
    assert changed["embeddings_reused"] >= 2  # stable.js module and function
    assert changed["embeddings_generated"] < initial["embeddings_generated"]
    (tmp_path / "service.js").unlink()
    deleted = index(tmp_path, store, provider)
    assert deleted["files"] == 2
    report = store.inspect(stable_id(str(tmp_path.resolve())), relations=True)
    load_edges = [r for r in report["relationships"] if r["kind"] == "CALLS" and r["target_name"] == "load"]
    assert len(load_edges) == 1 and load_edges[0]["target_id"] is None
    (tmp_path / "service.js").write_text("export function load(){return 4;}")
    added = index(tmp_path, store, provider)
    assert added["files"] == 3
    report = store.inspect(stable_id(str(tmp_path.resolve())), relations=True)
    assert any(r["kind"] == "CALLS" and r["target_name"] == "load" and r["target_id"] for r in report["relationships"])


def test_project_isolation_and_model_change_invalidates_cache(store, tmp_path):
    (tmp_path / "a.js").write_text("export function stable(){return true;}")
    provider = Provider()
    index(tmp_path, store, provider)
    root_b = tmp_path / "other"
    root_b.mkdir()
    (root_b / "b.js").write_text("export function second(){return false;}")
    try:
        index(root_b, store, provider)
        assert store.inspect(stable_id(str(tmp_path.resolve())))["counts"]["files"] == 1
        provider.model = "another-three-dimensional-model"
        rebuilt = index(root_b, store, provider)
        assert rebuilt["embeddings_reused"] == 0
        assert rebuilt["embeddings_generated"] > 0
        store.check_model("test-only-three-dimensional", 3, stable_id(str(tmp_path.resolve())))
    finally:
        with store.connection() as conn:
            conn.execute("DELETE FROM projects WHERE id=%s", (stable_id(str(root_b.resolve())),))


def test_exact_frontend_identifiers_and_directional_ranking(store, tmp_path):
    from lens.search.retrieval import search
    (tmp_path / "Table.jsx").write_text("export default function InvoiceTable(){return <div/>;}")
    (tmp_path / "Page.jsx").write_text("import InvoiceTable from './Table'; export function InvoicePage(){return <InvoiceTable/>;}")
    index(tmp_path, store, Provider())
    project = stable_id(str(tmp_path.resolve()))
    exact = search("Where is InvoiceTable defined?", store, None, mode="keyword", project=project)
    assert exact.results[0].chunk.name == "Table.jsx::InvoiceTable"
    related = search("What renders InvoiceTable?", store, None, mode="keyword", project=project)
    assert related.results[0].chunk.name == "Page.jsx::InvoicePage"
    assert related.debug["final_ranking"][0]["ranking_signals"]["relationship_match"]


def test_cross_layer_incremental_route_handler_sql_cleanup_and_revision(store, tmp_path, monkeypatch):
    import lens.repository.indexing as indexing
    import lens.database.store as persistence
    from lens.search.retrieval import search
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend").mkdir()
    page = tmp_path / "frontend/Page.jsx"
    page.write_text("export function Page(){fetch('/api/items');return <div/>;}")
    route = tmp_path / "backend/app.js"
    route.write_text("import express from 'express';import {load} from './service';const app=express();app.get('/api/items',load);")
    service = tmp_path / "backend/service.js"
    service.write_text("export function load(){return query('SELECT id FROM items');}")
    (tmp_path / "schema.sql").write_text("CREATE TABLE items(id INT);CREATE TABLE drafts(id INT);")
    provider = Provider()
    first = index(tmp_path, store, provider)
    project = stable_id(str(tmp_path.resolve()))
    initial = store.evidence_graph([project])
    assert all(edge.metadata.get("static_only") for edge in initial.edges)
    assert index(tmp_path, store, provider)["embeddings_generated"] == 0
    response = search("Show the Page chain from frontend to database", store, None, mode="keyword", project=project)
    assert response.debug["cross_layer"]["enabled"]
    assert any(hit.chunk.name == "items" for hit in response.results)
    service.write_text("export function load(){return query('SELECT id FROM drafts');}")
    changed = index(tmp_path, store, provider)
    assert changed["changed_files"] == 1 and changed["embeddings_reused"] > 0
    current = store.evidence_graph([project])
    assert not any(edge.kind == "READS" and edge.target_name == "items" for edge in current.edges)
    assert any(edge.kind == "READS" and edge.target_name == "drafts" and edge.target_id for edge in current.edges)
    route.write_text("import express from 'express';import {load} from './service';const app=express();app.get('/api/new',load);")
    index(tmp_path, store, provider)
    current = store.evidence_graph([project])
    assert not any(n.kind == "endpoint" and "/api/items" in n.name for n in current.nodes.values())
    assert any(edge.kind == "REQUESTS" and edge.target_name == "GET /api/items" and edge.target_id is None for edge in current.edges)
    service.unlink()
    assert index(tmp_path, store, provider)["deleted_files"] == 1
    current = store.evidence_graph([project])
    assert not any(edge.kind == "READS" for edge in current.edges)
    assert any(edge.kind == "HANDLES" and edge.target_name == "load" and edge.target_id is None for edge in current.edges)
    monkeypatch.setattr(indexing, "PIPELINE_REVISION", "test-phase3-revision")
    monkeypatch.setattr(persistence, "PIPELINE_REVISION", "test-phase3-revision")
    rebuilt = index(tmp_path, store, provider)
    assert rebuilt["embeddings_reused"] == 0 and rebuilt["embeddings_generated"] > 0
    assert rebuilt["embeddings_generated"] < first["embeddings_generated"]
    assert store.snapshot(project)["project"]["pipeline_revision"] == "test-phase3-revision"


def test_evidence_graph_project_scope_and_repeatable_migrations(store, tmp_path):
    (tmp_path / "a.js").write_text("export function local(){fetch('/missing');}")
    index(tmp_path, store, Provider())
    store.initialize()
    store.initialize()
    current = store.evidence_graph([stable_id(str(tmp_path.resolve()))])
    assert current.nodes
    assert not store.evidence_graph(["unindexed-project"]).nodes
    assert not store.chunks_for_entities(list(current.nodes), ["unindexed-project"])
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM lens_schema_version WHERE version=3").fetchone()["n"] == 1


def test_definition_lookup_is_project_scoped_and_preserves_ambiguity(store, tmp_path):
    (tmp_path / "one.js").write_text("export function upload() {}")
    (tmp_path / "two.js").write_text("export function upload() {}")
    index(tmp_path, store, Provider())
    project = stable_id(str(tmp_path.resolve()))
    assert len(store.definition_hits("upload", project)) == 2
    assert not store.definition_hits("upload", "unindexed-project")
    assert not store.definition_hits("notDeclared", project)
