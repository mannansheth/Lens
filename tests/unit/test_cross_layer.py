"""Static evidence contracts, traversal bounds, and complementary retrieval."""

import pytest
from lens.search.evidence import EvidenceGraph, EvidenceNode
from lens.core.models import Relationship, SearchHit
from lens.repository.indexing import parse_repository
from lens.repository.knowledge import chunks
from lens.search.multihop import intent, expand


def parsed(tmp_path, files):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return parse_repository(tmp_path)


def node(name, kind="function", source_layer="backend"):
    return EvidenceNode(name, name, kind, "code.js", 1, 2, source_layer)


def graph(edges, names="abcd"):
    return EvidenceGraph({name: node(name) for name in names}, edges)


def test_order_locations_unresolved_and_determinism():
    edge = Relationship("a", "CALLS", "b", "b", "literal", "high", {"source_line_start": 3})
    value = graph([edge, Relationship("b", "REQUESTS", "GET /missing")])
    report = value.reconstruct("a")
    path = report["paths"][0]
    assert [n["name"] for n in path["nodes"]] == ["a", "b", "GET /missing"]
    assert path["termination"] == "unresolved"
    assert path["relationships"][0]["metadata"]["source_line_start"] == 3
    assert path["nodes"][-1]["id"] is None
    assert report["paths"] == value.reconstruct("a")["paths"]


def test_cycle_depth_multiple_paths_dedup_and_budget():
    ab = Relationship("a", "CALLS", "b", "b")
    value = graph([ab, ab, Relationship("b", "CALLS", "a", "a"),
                   Relationship("b", "CALLS", "c", "c"), Relationship("a", "CALLS", "d", "d")])
    paths = value.reconstruct("a")["paths"]
    assert len(paths) == 2
    assert all(len({n["id"] for n in p["nodes"]}) == len(p["nodes"]) for p in paths)
    assert value.reconstruct("a", max_depth=1)["paths"][0]["termination"] == "depth_limit"
    assert value.reconstruct("a", max_paths=1)["truncated"]
    assert value.reconstruct("a", max_expansions=1)["truncated"]


@pytest.mark.parametrize("bounds", [{"max_depth": 0}, {"max_depth": 17}, {"max_paths": 0}, {"max_expansions": 0}])
def test_invalid_bounds(bounds):
    with pytest.raises(ValueError):
        graph([]).reconstruct("a", **bounds)


def test_missing_start_and_outside_snapshot():
    with pytest.raises(ValueError):
        graph([]).reconstruct("missing")
    with pytest.raises(ValueError):
        graph([Relationship("a", "CALLS", "outside", "outside")])


def test_reverse_edges_preserve_source_direction():
    value = graph([Relationship("a", "RENDERS", "b", "b"), Relationship("a", "REQUESTS", "c", "c")])
    path = value.reconstruct("b", upstream=True)["paths"][0]
    assert [n["name"] for n in path["nodes"]] == ["b", "a", "c"]
    assert path["relationships"][0]["direction"] == "reverse"
    assert path["relationships"][0]["source_id"] == "a"


def test_handler_service_sql_and_procedure_chain(tmp_path):
    knowledge = parsed(tmp_path, {
        "frontend/Page.jsx": "export function Page(){function submit(){fetch('/api/save',{method:'POST'});}return <form onSubmit={submit}/>;}",
        "backend/app.js": "import express from 'express';import {save} from './service';const app=express();app.post('/api/save',save);",
        "backend/service.js": "export function save(){return query('CALL SaveItem(?)',[1]);}",
        "schema.sql": "CREATE TABLE items(id INT); CREATE PROCEDURE SaveItem(IN x INT) BEGIN INSERT INTO items(id) VALUES(x); END;",
    })
    value = EvidenceGraph.from_knowledge(knowledge)
    page = next(s for s in knowledge.symbols if s.name == "Page")
    paths = value.reconstruct(page.id)["paths"]
    chain = next(path for path in paths if path["nodes"][-1]["name"] == "items")
    assert [edge["kind"] for edge in chain["relationships"]] == ["REFERENCES", "REQUESTS", "HANDLES", "CALLS_PROCEDURE", "WRITES"]
    assert all(edge["metadata"]["source_file"] for edge in chain["relationships"])
    assert all(edge["metadata"]["static_only"] for edge in chain["relationships"])


def test_inline_handler_identity_and_missing_named_handler(tmp_path):
    knowledge = parsed(tmp_path, {"backend/app.js": "import express from 'express';const app=express();app.get('/a',(req,res)=>query('SELECT id FROM items'));app.get('/b',missing);",
                                   "schema.sql": "CREATE TABLE items(id INT);"})
    value = EvidenceGraph.from_knowledge(knowledge)
    endpoint = next(s for s in knowledge.symbols if s.name == "GET /a")
    chain = next(p for p in value.reconstruct(endpoint.id)["paths"] if p["nodes"][-1]["name"] == "items")
    assert [r["kind"] for r in chain["relationships"]] == ["HANDLES", "READS"]
    missing = next(r for r in knowledge.relationships if r.kind == "HANDLES" and r.target_name == "missing")
    assert missing.target_id is None


def test_finite_sql_templates_unknown_sql_and_dml_semantics(tmp_path):
    knowledge = parsed(tmp_path, {"backend/service.js": "export function list(publicOnly){query(`SELECT id FROM items ${publicOnly ? 'WHERE visible=1' : ''}`);query(`SELECT * FROM ${table}`);query('INSERT INTO items(id) SELECT id FROM drafts');}",
                                   "schema.sql": "CREATE TABLE items(id INT,visible INT);CREATE TABLE drafts(id INT);"})
    named = {s.id: s.qualified_name for s in knowledge.entities}
    edges = {(r.kind, named.get(r.target_id)) for r in knowledge.relationships}
    assert {("READS", "items"), ("READS", "drafts"), ("WRITES", "items")} <= edges
    assert any(r.kind == "QUERIES" and r.target_id is None for r in knowledge.relationships)
    variants = [r for r in knowledge.relationships if r.metadata.get("conditional")]
    assert variants and all(r.metadata["rule"] == "finite_sql_alternatives" for r in variants)
    assert not any(r.target_name == "table" for r in knowledge.relationships)


def test_methods_ambiguity_external_host_and_unknown_options(tmp_path):
    knowledge = parsed(tmp_path, {
        "frontend/api.js": "export function api(path,opts={}){return fetch(`/api${path}`,opts);}",
        "frontend/page.js": "import {api} from './api';export function run(opts){api('/items');api('/items',{method:'POST'});api('/items',opts);fetch('https://other/api/items');}",
        "backend/app.js": "import express from 'express';const app=express();app.get('/api/items',()=>{});app.post('/api/items',()=>{});",
    })
    requests = [r for r in knowledge.relationships if r.kind == "REQUESTS" and r.source_id == next(s for s in knowledge.symbols if s.name == "run").id]
    assert any(r.target_name == "GET /api/items" and r.target_id for r in requests)
    assert any(r.target_name == "POST /api/items" and r.target_id for r in requests)
    assert any(r.target_name == "? /api/items" and r.target_id is None for r in requests)
    assert any("https:" in r.target_name and r.target_id is None for r in requests)


def test_shadowed_fetch_and_duplicate_routes_remain_unresolved(tmp_path):
    knowledge = parsed(tmp_path, {"page.js": "export function run(fetch){fetch('/api/items');}export function real(){fetch('/api/duplicate');}",
        "app.js": "import express from 'express';const app=express();app.get('/api/items',()=>{});app.get('/api/duplicate',()=>{});app.get('/api/duplicate',()=>{});"})
    assert all(edge.target_id is None for edge in knowledge.relationships if edge.kind == "REQUESTS")


def test_sql_column_locations_are_actual_declaration_lines(tmp_path):
    knowledge = parsed(tmp_path, {"schema.sql": "CREATE TABLE items (\n id INT,\n name VARCHAR(100)\n);"})
    assert {entity.name: entity.line_start for entity in knowledge.database_objects if entity.kind == "column"} == {"id": 2, "name": 3}


def test_fixed_method_wrapper_does_not_inherit_callers_default_get(tmp_path):
    knowledge = parsed(tmp_path, {"api.js": "export function send(path){return fetch(`/api${path}`,{method:'POST'});}",
        "page.js": "import {send} from './api';export function run(){send('/items');}",
        "app.js": "import express from 'express';const app=express();app.get('/api/items',()=>{});app.post('/api/items',()=>{});"})
    run = next(symbol for symbol in knowledge.symbols if symbol.name == "run")
    assert any(edge.source_id == run.id and edge.kind == "REQUESTS" and edge.target_name == "POST /api/items" and edge.target_id for edge in knowledge.relationships)
    assert not any(edge.source_id == run.id and edge.kind == "REQUESTS" and edge.target_name.startswith("GET") for edge in knowledge.relationships)


def test_wrapper_default_options_and_computed_method_are_conservative(tmp_path):
    knowledge = parsed(tmp_path, {"api.js": "export function api(path,options={method:'POST'}){return fetch(`/api${path}`,options);}",
        "page.js": "import {api} from './api';export function run(){api('/items');fetch('/api/items',{[method]:'POST'});}",
        "app.js": "import express from 'express';const app=express();app.get('/api/items',()=>{});app.post('/api/items',()=>{});"})
    run = next(symbol for symbol in knowledge.symbols if symbol.name == "run")
    assert any(edge.source_id == run.id and edge.kind == "REQUESTS" and edge.target_name == "POST /api/items" and edge.target_id for edge in knowledge.relationships)
    assert any(edge.source_id == run.id and edge.kind == "REQUESTS" and edge.target_name == "? /api/items" and edge.target_id is None for edge in knowledge.relationships)


def test_mutated_wrapper_options_are_not_proven(tmp_path):
    knowledge = parsed(tmp_path, {"api.js": "export function api(path,options={}){options.method=choice;return fetch(`/api${path}`,options);}",
        "page.js": "import {api} from './api';export function run(){api('/items');}",
        "app.js": "import express from 'express';const app=express();app.get('/api/items',()=>{});"})
    run = next(symbol for symbol in knowledge.symbols if symbol.name == "run")
    assert not any(edge.source_id == run.id and edge.kind == "REQUESTS" and edge.target_id for edge in knowledge.relationships)


def test_missing_endpoint_and_dynamic_url_are_explicit(tmp_path):
    knowledge = parsed(tmp_path, {"page.js": "export function run(url){fetch('/missing');fetch(url);}"})
    requests = [edge for edge in knowledge.relationships if edge.kind == "REQUESTS"]
    assert {edge.target_name for edge in requests} == {"GET /missing", "GET [dynamic URL]"}
    assert all(edge.target_id is None for edge in requests)


def test_query_preference_reaches_relevant_branch_with_fixed_path_budget():
    value = EvidenceGraph({name: node(name) for name in ("root", "unrelated", "login", "sessions")}, [
        Relationship("root", "CALLS", "unrelated", "unrelated"),
        Relationship("root", "CALLS", "login", "login"),
        Relationship("unrelated", "REQUESTS", "GET /missing"),
        Relationship("login", "WRITES", "sessions", "sessions")])
    result = value.reconstruct("root", max_paths=1, preferred_terms=frozenset({"login"}))
    assert result["paths"][0]["nodes"][-1]["name"] == "sessions"
    assert result["candidate_paths"] == 1


@pytest.mark.parametrize("query,expected", [
    ("Where is Header defined?", False), ("Which component renders Header?", False),
    ("Where is the projects table defined?", False),
    ("Trace the chain from a component to storage", True),
    ("Which API does this component call?", True),
    ("What happens when a form is submitted?", True),
    ("Which database table does this endpoint read?", True),
])
def test_general_query_intent(query, expected):
    assert intent(query)["expand"] == expected


def test_chain_diversity_keeps_k_and_recovers_missing_chunks(tmp_path):
    knowledge = parsed(tmp_path, {
        "frontend/Page.jsx": "export function Page(){function submit(){fetch('/api/save',{method:'POST'});}return <form onSubmit={submit}/>;}",
        "backend/app.js": "import express from 'express';const app=express();app.post('/api/save',(req,res)=>query('INSERT INTO items(id) VALUES(1)'));",
        "schema.sql": "CREATE TABLE items(id INT);",
    })
    documents = chunks(knowledge, "test")
    page = next(c for c in documents if c.name == "frontend/Page.jsx::Page")
    hits = [SearchHit(page, 1, 0.8, 0.9)]
    chosen, trace = expand("Show the Page chain from frontend to database", hits,
        EvidenceGraph.from_knowledge(knowledge), lambda ids: [SearchHit(c) for c in documents if c.entity_id in ids], 5)
    assert len(chosen) == 5
    assert {"frontend", "api", "backend", "database"} <= set(trace["selected_layers"])
    assert any(hit.chunk.name == "items" for hit in chosen)
    assert trace["selected_paths"][0]["nodes"][0]["name"] == page.name


def test_photo_flow_keeps_handler_over_unrelated_resolved_graph(tmp_path):
    knowledge = parsed(tmp_path, {
        "frontend/CameraScreen.jsx": "export function CameraScreen(){async function takePicture(){const photo=await cameraRef.current.takePictureAsync({base64:true});setImage(photo);setShowImagePreview(true);}return <CameraUI takePicture={takePicture}/>;}",
        "frontend/CameraUI.jsx": "export function CameraUI({takePicture}){return <button onClick={takePicture}>Photo</button>;}",
        "frontend/App.jsx": "export function App(){function notify(){sendNotification();}return <button onClick={notify}>Click</button>;}",
    })
    documents = chunks(knowledge, "test")
    def candidate(suffix, score):
        chunk = next(c for c in documents if c.name.endswith(suffix))
        return SearchHit(chunk, hybrid_score=score)
    hits = [candidate("::App", .99), candidate("::CameraUI", .3),
            candidate("::CameraScreen.takePicture", .2), candidate("::CameraScreen", .1)]
    chosen, trace = expand("What happens when user clicks photo?", hits,
        EvidenceGraph.from_knowledge(knowledge), lambda ids: [SearchHit(c) for c in documents if c.entity_id in ids], 3)
    assert any(h.chunk.name.endswith("::CameraScreen.takePicture") for h in chosen)
    assert not any(name.endswith("::App") for name in trace["anchors"])
    assert any(h.chunk.name.endswith("::CameraUI") for h in chosen)


def test_submit_entry_point_survives_neighboring_read_path():
    from lens.search.evidence import EvidenceNode
    from lens.core.models import DocumentChunk, Relationship
    definitions = [("h", "handleReportSubmit", "function", "frontend"),
                   ("b", "SubmitButton", "component", "frontend"),
                   ("f", "ReportForm", "component", "frontend"),
                   ("l", "getNearbyReports", "function", "frontend"),
                   ("t", "reports", "table", "database")]
    nodes = {cid:EvidenceNode(cid, name, kind, "frontend/report.jsx", 1, 3, source_layer)
             for cid, name, kind, source_layer in definitions}
    graph = EvidenceGraph(nodes, [Relationship("h","CALLS","submitReport"),
                                  Relationship("b","RENDERS","ReportForm","f"),
                                  Relationship("f","CALLS","getNearbyReports","l"),
                                  Relationship("l","READS","reports","t")])
    hits = [SearchHit(DocumentChunk(cid,cid,name,kind,"frontend/report.jsx","jsx","source",{"entity_name":name}),
                      hybrid_score=.2 if cid == "h" else .9)
            for cid, name, kind, _ in definitions]
    chosen, _ = expand("What happens when user submits a report?", hits, graph,
                       lambda ids:[h for h in hits if h.chunk.entity_id in ids], 3)
    assert chosen[0].chunk.entity_id == "h"
    assert len(chosen) == 3
