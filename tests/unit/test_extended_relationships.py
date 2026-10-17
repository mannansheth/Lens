"""Cross-language links require explicit structural evidence and retain provenance."""
from lens.repository.indexing import parse_repository
from lens.repository.knowledge import chunks


def parsed(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return parse_repository(tmp_path)


def test_frontend_python_sql_and_import_flow(tmp_path):
    knowledge = parsed(tmp_path, {
        "frontend.js": 'export function submit() { return fetch("/score", {method:"POST"}); }',
        "service.py": 'from analysis import score\nfrom flask import Flask\napp = Flask(__name__)\n@app.post("/score")\ndef predict():\n    return score()\n',
        "analysis.py": 'import os\ndef score():\n    key = os.getenv("MODEL_NAME")\n    db.execute("INSERT INTO Scores (Value) VALUES (1)")\n    return 1\n',
        "schema.sql": 'CREATE TABLE Scores (Value INT);',
        ".env.example": 'MODEL_NAME=private-model-name\n',
    })
    by_id = {s.id: s for s in knowledge.entities}
    requests = [e for e in knowledge.relationships if e.kind == "REQUESTS" and e.target_id]
    assert any(by_id[e.target_id].kind == "endpoint" for e in requests)
    assert any(e.kind == "CALLS" and e.target_id and by_id[e.target_id].qualified_name == "analysis.score" for e in knowledge.relationships)
    assert any(e.kind == "WRITES" and e.target_id and by_id[e.target_id].name == "Scores" for e in knowledge.relationships)
    config = next(e for e in knowledge.relationships if e.kind == "USES_CONFIGURATION" and e.target_name == "MODEL_NAME")
    assert config.target_id and config.metadata["rule"] == "environment_key_reference"
    documents = chunks(knowledge, "test")
    assert "private-model-name" not in "\n".join(c.text for c in documents)
    assert any(r["metadata"].get("rule") == "python_lexical_import" for c in documents for r in c.metadata["relation_evidence"])


def test_razor_controller_model_and_http_service(tmp_path):
    knowledge = parsed(tmp_path, {
        "Views/Journal/Edit.cshtml": '@model Demo.Entry\n<form asp-controller="Journal" asp-action="Save" method="post"><input asp-for="Text" /></form>',
        "JournalController.cs": '''namespace Demo {
class Entry { public string Text { get; set; } }
class JournalController {
  private HttpClient client;
  public object Edit() { return View(); }
  [HttpPost] public object Save(Entry entry) { return client.PostAsync("http://localhost:8000/score", entry); }
}}
''',
        "service.py": '@app.post("/score")\ndef score():\n    return 1\n',
    })
    edges = knowledge.relationships
    assert any(e.kind == "USES_MODEL" and e.target_id for e in edges)
    assert any(e.kind == "REQUESTS" and e.target_name == "Journal/Save" and e.target_id and e.confidence == "static_pattern" for e in edges)
    assert any(e.kind == "RENDERS" and e.target_id and e.confidence == "static_pattern" for e in edges)
    assert any(e.kind == "REQUESTS" and e.target_name.endswith("/score") and e.target_id and
               e.metadata["rule"] == "static_method_path_candidate" for e in edges)


def test_ambiguous_endpoint_and_unimported_function_remain_unresolved(tmp_path):
    knowledge = parsed(tmp_path, {
        "frontend.js": 'fetch("/score", {method:"POST"});',
        "one.py": '@app.post("/score")\ndef score():\n    missing()\n',
        "two.py": '@app.post("/score")\ndef missing():\n    return 1\n',
    })
    assert all(not e.target_id for e in knowledge.relationships if e.kind == "REQUESTS")
    assert all(not e.target_id for e in knowledge.relationships if e.kind == "CALLS" and e.target_name == "missing")


def test_malformed_sql_token_does_not_abort_relationship_discovery(tmp_path):
    knowledge = parsed(tmp_path, {"service.py": 'def run():\n    db.execute("SELECT \\\'unterminated")\n'})
    assert knowledge.symbols
    assert any("malformed inline SQL" in warning for warning in knowledge.warnings)
