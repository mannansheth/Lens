"""Deterministic regressions for complementary evidence and useful partial answers."""
import json

import pytest

from lens.search.evidence import EvidenceGraph, EvidenceNode
from lens.llm.answering import synthesize
from lens.llm.context import build_context
from lens.llm.provider import Generation
from lens.core.models import DocumentChunk, Relationship, SearchHit
from lens.search.multihop import expand
from lens.search.retrieval import SearchResponse


def hit(identifier, file, language, kind="function"):
    return SearchHit(DocumentChunk(identifier, identifier, identifier, kind, file, language,
                                 f"def {identifier}():\n    return data\n",
                                 {"line_start": 1, "line_end": 2}), hybrid_score=.8)


def test_connected_python_branch_survives_frontend_candidates():
    definitions = [hit("submitJournal", "frontend/form.tsx", "typescript"),
                   hit("receiveJournal", "backend/routes.cs", "csharp"),
                   hit("calculateMood", "analysis/service.py", "python"),
                   hit("journal", "schema.sql", "sql", "table")]
    nodes = {h.chunk.id: EvidenceNode(h.chunk.id, h.chunk.name, h.chunk.kind,
             h.chunk.file, 1, 2, source_layer) for h, source_layer in
             zip(definitions, ["frontend", "backend", "code", "database"])}
    graph = EvidenceGraph(nodes, [
        Relationship("submitJournal", "CALLS", "receiveJournal", "receiveJournal"),
        Relationship("receiveJournal", "CALLS", "calculateMood", "calculateMood"),
        Relationship("receiveJournal", "WRITES", "journal", "journal")])
    selected, trace = expand("Trace submitJournal flow through analysis and database",
                            definitions[:1], graph,
                            lambda ids: [h for h in definitions if h.chunk.id in ids], 8)
    assert {h.chunk.language for h in selected} == {"typescript", "csharp", "python", "sql"}
    assert any(path["relationships"] for path in trace["selected_paths"])


def test_context_retains_more_than_five_selected_implementations():
    hits = [hit(f"step{i}", f"src/{i}.py", "python") for i in range(9)]
    context = build_context(SearchResponse(hits, {}), "project")
    assert len(context.items) == 9
    assert len(context.render().encode("utf-8")) <= 12000
    assert context.retrieved_count == 9


def test_partial_answer_keeps_verified_statement_and_citations():
    selected = hit("submitJournal", "frontend/form.tsx", "typescript")
    class Provider:
        def generate(self, messages, schema):
            assert "partial" in schema["properties"]["evidence_status"]["enum"]
            return Generation(json.dumps({"evidence_status": "partial", "answer": [
                {"text": "The source defines submitJournal; no downstream persistence is established in this excerpt.",
                 "basis": "static_fact", "citations": ["E-submitJournal"]}]}))
    result = synthesize("Explain the journal workflow", SearchResponse([selected], {}), Provider(), "project")
    assert result.output["evidence_status"] == "partial"
    assert result.output["answer"] and result.output["abstention"] is None
    assert result.citations[0]["file"] == "frontend/form.tsx"


def test_partial_answer_still_rejects_invented_citation():
    from lens.llm.schemas import parse_answer
    with pytest.raises(ValueError, match="citation"):
        parse_answer(json.dumps({"evidence_status": "partial", "answer": [
            {"text": "Known step", "basis": "static_fact", "citations": ["invented"]}]}), {"real": {}})


def test_live_schema_context_excludes_connection_values_and_records():
    schema = {"dialect": "sqlite", "password": "never-forward-this", "tables": [
        {"schema": "main", "name": "journal", "columns": [
            {"name": "id", "type": "INTEGER", "primary_key": True, "default": "private-value"}],
         "rows": [["private-record"]], "indexes": []}]}
    context = build_context(SearchResponse([], {}), "project", database_schema=schema)
    rendered = context.render()
    assert all(value not in rendered for value in ("never-forward-this", "private-value", "private-record"))
    assert context.items[0]["id"] == "D-schema"
    assert context.items[0]["static_only"] is False
    assert '"static_only": false' in rendered
    assert "journal" in rendered and "INTEGER" in rendered
