"""LLM boundary tests use fake generation or a local HTTP fixture, never model downloads."""

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import pytest
from lens.llm.answering import synthesize, ask
from lens.llm.context import build_context
from lens.llm.ollama import LLMSettings, OllamaProvider
from lens.llm.provider import Generation
from lens.llm.schemas import parse_answer, parse_intent
from lens.core.models import DocumentChunk, SearchHit
from lens.search.retrieval import SearchResponse


@pytest.fixture
def response():
    chunk = DocumentChunk("chunk", "entity", "src/mail.js::notify", "function", "src/mail.js", "javascript",
                          "function notify() { messenger.send(); }", {"entity_name": "notify", "line_start": 4, "line_end": 4})
    return SearchResponse([SearchHit(chunk, rank=1)], {"retrieval_seconds": .01})


def answer(status="sufficient", cid="E-chunk"):
    return {"evidence_status": status, "answer": [{"text": "Source defines notify calling messenger.send.",
            "basis": "static_fact", "citations": [cid]}]}


class Fake:
    def __init__(self, value):
        self.value = value
        self.calls = []
    def generate(self, messages, schema=None):
        self.calls.append((messages, schema))
        return Generation(json.dumps(self.value), 200, 40)


def test_context_stable_bounded_and_excludes_candidates(response):
    response.debug["keyword_results_raw"] = [{"text": "candidate noise"}]
    first = build_context(response, "p")
    assert first.render() == build_context(response, "p").render()
    assert "candidate noise" not in first.render()
    assert first.items[0]["id"] == "E-chunk"
    response.results[0].chunk.text = "line\n" * 2000
    small = build_context(response, "p", 2000)
    assert len(small.render()) <= 2000
    assert small.truncated or small.items[0]["excerpt_truncated"]
    assert len(response.results[0].chunk.text) == 10000


def test_context_does_not_mutate_selected_paths(response):
    edge = {"source_id": "entity", "kind": "CALLS", "target_name": "dynamic", "target_id": None,
            "evidence": "dynamic()", "metadata": {"source_file": "src/mail.js", "source_line_start": 4}}
    path = {"relationships": [edge], "termination": "unresolved"}
    response.debug.update(cross_layer={"selected_paths": [path]}, semantic={"selected_paths": [path]})
    context = build_context(response, "p")
    assert len(response.debug["cross_layer"]["selected_paths"]) == 1
    assert not context.items[-1]["resolved"]
    assert all(p["termination"] == "unresolved" for p in context.paths)


def test_context_project_required_and_foreign_metadata_rejected(response):
    with pytest.raises(ValueError):
        build_context(response, "")
    response.results[0].chunk.metadata["project_id"] = "other"
    with pytest.raises(ValueError, match="another project"):
        build_context(response, "p")


def test_compact_id_collision_fails_closed(response):
    from copy import deepcopy
    first = deepcopy(response.results[0])
    second = deepcopy(first)
    first.chunk.id = "0123456789abcdef" + "a" * 48
    second.chunk.id = "0123456789abcdef" + "b" * 48
    response.results = [first, second]
    with pytest.raises(ValueError, match="collision"):
        build_context(response, "p")


def test_context_specific_schema_ids_and_entities(response):
    from lens.llm.schemas import generation_schema
    context = build_context(response, "p")
    schema = generation_schema("developer", context)
    assert schema["properties"]["answer"]["items"]["properties"]["citations"]["items"]["enum"] == ["E-chunk"]
    assert "enum" not in schema["properties"]["answer"]["items"]["properties"]["text"]
    assert "no_evidence" not in schema["properties"]["evidence_status"]["enum"]
    user = generation_schema("user", context)
    assert "notify" in user["properties"]["entities"]["items"]["enum"]
    assert "enum" not in user["properties"]["subject"]
    assert "enum" not in user["properties"]["constraints"]["items"]["properties"]["value"]


def test_grounded_answer_citations_and_debug(response):
    provider = Fake(answer())
    result = synthesize("Which service sends messages?", response, provider, "p")
    assert result.citations[0]["file"] == "src/mail.js"
    assert result.output["abstention"] is None
    assert result.performance["input_tokens"] == 200
    assert "debug" not in result.to_dict()
    assert result.to_dict(True)["debug"]["prompt_version"] == "lens-mvp-v4-navigation"
    assert "untrusted" in provider.calls[0][0][0]["content"]


def test_empty_context_abstains_without_generation():
    provider = Fake({})
    result = synthesize("Anything?", SearchResponse([], {}), provider, "p")
    assert result.output["evidence_status"] == "no_evidence"
    assert result.output["abstention"]
    assert not provider.calls


@pytest.mark.parametrize("status", ["insufficient", "conflicting"])
def test_partial_and_conflicting_evidence_abstention(response, status):
    with pytest.raises(ValueError, match="Nonsufficient"):
        synthesize("Did delivery succeed?", response, Fake(answer(status)), "p")
    result = synthesize("Did delivery succeed?", response, Fake({"evidence_status": status, "answer": []}), "p")
    assert result.output["abstention"] and not result.output["answer"]


@pytest.mark.parametrize("value", [
    {}, {"evidence_status": "certain", "answer": []},
    {"evidence_status": "sufficient", "answer": []},
    {"evidence_status": "sufficient", "answer": "anything"},
    {**answer(), "sql": "DROP TABLE reports"},
    answer(cid="invented"), {"evidence_status": "no_evidence", "answer": []},
    {"evidence_status": "sufficient", "answer": [{"text": "yes", "basis": "runtime_fact", "citations": ["E-chunk"]}]},
    {"evidence_status": "sufficient", "answer": [{"text": "yes", "basis": "static_fact", "citations": []}]},
])
def test_invalid_answers_fail_closed(response, value):
    with pytest.raises(ValueError):
        synthesize("Anything?", response, Fake(value), "p")


@pytest.mark.parametrize("raw", ["not json", "```json\n{}\n```", '{"answer":[],"answer":[]}',
                                 '{"evidence_status":NaN,"answer":[]}'])
def test_malformed_duplicate_nonfinite_json(raw):
    with pytest.raises(ValueError):
        parse_answer(raw, {})


def intent(**changes):
    value = {"evidence_status": "sufficient", "intent": "capability_query", "subject": "notifications",
             "entities": ["notify"], "constraints": [], "citations": ["E-chunk"]}
    return dict(value, **changes)


def test_typed_intent_grounding_and_unexecuted_constraints(response):
    value = intent(intent="data_query", evidence_status="insufficient", constraints=[
        {"field": "owner", "operator": "equals", "value": "current_user"}])
    result = synthesize("Show my notifications", response, Fake(value), "p", "user")
    assert result.output["intent"] == "data_query"
    assert result.output["constraints"][0]["value"] == "current_user"
    assert "execute" not in result.output


@pytest.mark.parametrize("changes", [{"intent": "execute_sql"}, {"entities": ["nonexistent"]},
                                     {"citations": ["foreign"]}, {"citations": []}, {"constraints": {}},
                                     {"confidence": 1}, {"evidence_status": True}])
def test_invalid_intent(response, changes):
    with pytest.raises(ValueError):
        parse_intent(json.dumps(intent(**changes)), build_context(response, "p").evidence)


@pytest.mark.parametrize("host", ["http://example.com", "https://localhost:11434", "http://localhost@evil.com",
                                  "http://localhost:11434/path", "http://localhost:11434/?secret=x"])
def test_remote_or_ambiguous_hosts_rejected(host):
    with pytest.raises(ValueError):
        LLMSettings(host=host)


def test_configuration_validated_and_model_override(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "another:local")
    assert LLMSettings.load().model == "another:local"
    assert LLMSettings.load("qwen3:8b").model == "qwen3:8b"
    with pytest.raises(ValueError):
        LLMSettings(temperature=float("nan"))
    with pytest.raises(ValueError):
        LLMSettings(context_tokens=2048)
    with pytest.raises(ValueError):
        LLMSettings(model="qwen:cloud")


@pytest.fixture
def server():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"models": [{"name": "qwen3:8b", "digest": "local"}]}
            self.reply(body)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                self.reply({"model_info": {"general.architecture": "qwen3"}})
                return
            received.append(body)
            self.reply({"done": True, "message": {"content": json.dumps(answer())},
                        "prompt_eval_count": 100, "eval_count": 30})
        def reply(self, value):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield http, received
    http.shutdown()
    http.server_close()
    thread.join()


def test_real_http_transport_schema_no_tools_no_thinking(server):
    http, requests = server
    provider = OllamaProvider(LLMSettings(host=f"http://127.0.0.1:{http.server_port}"))
    result = provider.generate([{"role": "user", "content": "data"}], {"type": "object"})
    assert result.input_tokens == 100
    assert requests[0]["stream"] is False and requests[0]["think"] is False
    assert "tools" not in requests[0]
    assert requests[0]["format"] == {"type": "object"}
    assert provider.model_info()["digest"] == "local"
    with pytest.raises(RuntimeError, match="missing"):
        OllamaProvider(LLMSettings(host=provider.settings.host, model="missing:local")).model_info()


@pytest.mark.parametrize("value", [{}, {"models": "bad"}])
def test_malformed_health(monkeypatch, value):
    provider = OllamaProvider(LLMSettings())
    monkeypatch.setattr(provider, "_request", lambda *args: value)
    with pytest.raises(RuntimeError):
        provider.health()


def test_ask_preserves_retrieval_arguments_and_no_execution(response, monkeypatch):
    import lens.llm.answering as module
    calls = []
    monkeypatch.setattr(module, "search", lambda *args, **kwargs: (calls.append((args, kwargs)) or response))
    result = ask("How do messages work?", object(), object(), Fake(answer()), "p")
    assert calls[0][1] == {"project": "p", "alpha": .5, "top_k": 12}
    assert result.performance["total_seconds"] > 0
    assert asdict(result)["project"] == "p"
    with pytest.raises(ValueError):
        ask("anything", None, None, Fake(answer()), None)


def test_cli_ask_project_selection_json_and_failure(tmp_path, monkeypatch, capsys, response):
    import lens.commands.cli as cli
    import lens.llm.ollama as transport
    from lens.core.models import stable_id
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
    projects = [{"id": stable_id(str(root.resolve())), "root": str(root.resolve())} for root in roots]
    checks = []
    class Store:
        def projects(self):
            return projects
        def check_model(self, model, dimension, project):
            checks.append(project)
        def keyword_search(self, *args):
            return response.results
        def vector_search(self, *args):
            return response.results
    class Embeddings:
        model = "local"
        dimension = 3
        def embed(self, text):
            return [[1, 0, 0]]
    class LLM(Fake):
        def model_info(self):
            return {"name": "test"}
    monkeypatch.setattr(cli, "PostgresStore", lambda url: Store())
    monkeypatch.setattr(cli, "LocalEmbeddingProvider", lambda settings: Embeddings())
    monkeypatch.setattr(transport, "OllamaProvider", lambda settings: LLM(answer()))
    monkeypatch.setenv("LENS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "qwen3:8b")
    monkeypatch.chdir(roots[0])
    assert cli.main(["ask", "Which service sends messages?", "--json", "--timings"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["project"] == projects[0]["id"]
    assert "performance" in json.loads(captured.err.splitlines()[-1])
    assert "src/mail.js" not in captured.err
    assert cli.main(["ask", "Which service sends messages?", "--project", str(roots[1]), "--debug"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["project"] == projects[1]["id"] and "context" in result["debug"]
    assert checks[-1] == projects[1]["id"]
    monkeypatch.setattr(transport, "OllamaProvider", lambda settings: LLM(answer(cid="invented")))
    assert cli.main(["ask", "Which service sends messages?", "--json", "--timings"]) == 1
    captured = capsys.readouterr()
    assert not captured.out and "invented a citation" in captured.err
    assert next(json.loads(line)["performance"] for line in captured.err.splitlines()
                if line.startswith('{"performance"'))["status"] == "error"


@pytest.mark.parametrize("failure", ["unavailable", "timeout", "malformed", "http"])
def test_transport_errors_are_clear(monkeypatch, failure):
    import io
    import socket
    from urllib.error import URLError, HTTPError
    provider = OllamaProvider(LLMSettings())
    errors = {"unavailable": URLError("refused"), "timeout": socket.timeout(),
              "http": HTTPError("local", 404, "missing", {}, None)}
    class BadTransport:
        def open(self, *args, **kwargs):
            if failure == "malformed":
                return io.BytesIO(b"not JSON")
            raise errors[failure]
    monkeypatch.setattr(provider, "_transport", BadTransport())
    with pytest.raises(RuntimeError, match={"unavailable": "unavailable", "timeout": "timed out", "malformed": "Malformed", "http": "HTTP 404"}[failure]):
        provider.health()


def test_remote_alias_detected_before_evidence_is_sent(monkeypatch):
    provider = OllamaProvider(LLMSettings(model="alias:local"))
    requests = []
    def request(path, body=None):
        requests.append(path)
        return {"models": [{"name": "alias:local"}]} if path == "/api/tags" else {"remote_host": "https://cloud", "remote_model": "large"}
    monkeypatch.setattr(provider, "_request", request)
    with pytest.raises(RuntimeError, match="Remote-backed"):
        provider.generate([{"role": "user", "content": "source"}])
    assert requests == ["/api/tags", "/api/show"]


@pytest.mark.parametrize("reply", [
    {}, {"done": True, "message": {}},
    {"done": True, "done_reason": "length", "message": {"content": "{}"}},
    {"done": True, "message": {"content": "{}", "tool_calls": [{"name": "execute"}]}},
    {"done": True, "message": {"content": "{}"}, "eval_count": True},
    {"done": True, "message": {"content": "{}"}, "remote_host": "https://cloud"},
])
def test_invalid_ollama_generation_envelopes(monkeypatch, reply):
    provider = OllamaProvider(LLMSettings())
    monkeypatch.setattr(provider, "model_info", lambda: {"name": "local"})
    monkeypatch.setattr(provider, "_request", lambda *args: reply)
    with pytest.raises(RuntimeError):
        provider.generate([{"role": "user", "content": "source evidence"}])


def test_redirect_is_refused():
    from lens.llm.ollama import NoRedirect
    with pytest.raises(RuntimeError, match="redirects"):
        NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://remote")


def test_model_context_budget_does_not_send_oversized_source(monkeypatch):
    provider = OllamaProvider(LLMSettings())
    called = []
    monkeypatch.setattr(provider, "model_info", lambda: called.append(True))
    with pytest.raises(ValueError, match="context budget"):
        provider.generate([{"role": "user", "content": "a" * 40000}])
    assert not called
