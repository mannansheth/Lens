"""MVP provider/selection/grounding regression tests; no live API calls."""
from dataclasses import replace
import json
from types import SimpleNamespace as NS
import pytest

from lens.llm.gemini import GeminiProvider
from lens.llm.models import catalogue, create_provider, identify, load_settings, save_selection
from lens.llm.ollama import LLMSettings
from lens.llm.schemas import parse_answer, parse_intent, validate
from lens.llm.diagnostics import redact, write_trace
from lens.search.semantics import understand


def sdk_response(text='{"evidence_status":"insufficient","answer":[]}', finish="STOP", parts=None):
    return NS(text=text, candidates=[NS(finish_reason=finish, content=NS(parts=parts or [NS(text=text)]))],
              usage_metadata=NS(prompt_token_count=50, candidates_token_count=10))


def gemini(response=None, error=None):
    calls = []
    def generate_content(**kwargs):
        calls.append(kwargs)
        if error:
            raise error
        return response or sdk_response()
    client = NS(models=NS(generate_content=generate_content,
                          get=lambda **kw: NS(name=kw["model"], supported_actions=["generateContent"]),
                          list=lambda: [NS(name="models/gemini-3.8-flash")]))
    return GeminiProvider(replace(LLMSettings(), provider="gemini", model="gemini-3.8-flash"), client), calls


def test_gemini_adapter_request_usage_and_shared_validation():
    provider, calls = gemini()
    schema = {"type": "object"}
    result = provider.generate([{"role":"system","content":"rules"}, {"role":"user","content":"selected evidence"}], schema)
    assert calls[0]["config"]["response_json_schema"] == schema
    assert calls[0]["config"]["system_instruction"] == "rules"
    assert calls[0]["contents"] == [{"role":"user","parts":[{"text":"selected evidence"}]}]
    assert "tools" not in calls[0]["config"]
    assert "temperature" not in calls[0]["config"]
    assert (result.input_tokens, result.output_tokens) == (50, 10)
    assert parse_answer(result.text, {}).evidence_status == "insufficient"
    assert provider.model_info()["generation_verified"] is False
    assert provider.health()["models"]


@pytest.mark.parametrize("response", [sdk_response(finish="MAX_TOKENS"), sdk_response(finish="SAFETY"),
                                      sdk_response(text=""), sdk_response(parts=[NS(function_call={"name":"run"})])])
def test_gemini_blocks_incomplete_and_tool_output(response):
    provider, _ = gemini(response)
    with pytest.raises(RuntimeError):
        provider.generate([{"role":"user","content":"question"}])


@pytest.mark.parametrize("code,category", [(400,"invalid argument"), (401,"authentication"), (403,"authentication"), (429,"quota"), (404,"missing model"), (500,"network/API")])
def test_gemini_errors_never_echo_sdk_credentials(code, category):
    class Error(Exception):
        pass
    error = Error("https://example.invalid?key=PRIVATE_SECRET")
    error.code = code
    provider, _ = gemini(error=error)
    with pytest.raises(RuntimeError, match=category) as caught:
        provider.generate([{"role":"user","content":"private source"}])
    assert "PRIVATE_SECRET" not in str(caught.value)
    assert "private source" not in json.dumps(caught.value.inference)


@pytest.mark.parametrize("mode", ["developer", "user"])
def test_gemini_wire_schema_preserves_shape_and_local_strictness(mode):
    from copy import deepcopy
    from lens.llm.context import EvidenceContext
    from lens.llm.gemini import gemini_schema
    from lens.llm.schemas import generation_schema
    context = EvidenceContext("p", ({"id":"E-a","entity_names":["upload"],"excerpt":"upload()"},), (), False, 1)
    schema = generation_schema(mode, context)
    original = deepcopy(schema)
    wire = gemini_schema(schema)
    assert schema == original
    assert wire["required"] == schema["required"]
    assert "additionalProperties" not in wire
    assert schema["additionalProperties"] is False
    def check(node):
        assert not {"minLength", "maxLength", "uniqueItems", "anyOf", "additionalProperties",
                    "enum", "minItems", "maxItems"} & node.keys()
        for child in node.get("properties", {}).values():
            check(child)
        if "items" in node:
            check(node["items"])
    check(wire)
    citations = wire["properties"]["answer"]["items"]["properties"]["citations"] if mode == "developer" else wire["properties"]["citations"]
    assert citations["items"] == {"type":"string"}
    original_citations = schema["properties"]["answer"]["items"]["properties"]["citations"] if mode == "developer" else schema["properties"]["citations"]
    assert original_citations["items"]["enum"] == ["E-a"]
    if mode == "developer":
        with pytest.raises(ValueError, match="unsupported LLM output fields"):
            parse_answer(json.dumps({"evidence_status":"insufficient","answer":[],"extra":"field"}), context.evidence)
        with pytest.raises(ValueError, match="invented a citation"):
            parse_answer(json.dumps({"evidence_status":"sufficient","answer":[{"text":"upload()", "basis":"static_fact","citations":["E-missing"]}]}), context.evidence)
        with pytest.raises(ValueError, match="must abstain"):
            parse_answer(json.dumps({"evidence_status":"insufficient","answer":[{"text":"upload()", "basis":"static_fact","citations":["E-a"]}]}), context.evidence)
    else:
        with pytest.raises(ValueError):
            parse_intent(json.dumps({"evidence_status":"insufficient","intent":"data_query","subject":"upload",
                                    "entities":["upload","upload"],"constraints":[],"citations":["E-a"]}), context.evidence)


def test_gemini_missing_credentials_and_real_sdk_constructor(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = replace(LLMSettings(), provider="gemini", model="gemini-3.8-flash")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiProvider(settings)
    genai = pytest.importorskip("google.genai")
    calls = []
    monkeypatch.setattr(genai, "Client", lambda **kw: calls.append(kw) or NS())
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    GeminiProvider(settings)
    assert calls[0]["api_key"] == "test-placeholder"
    assert calls[0]["vertexai"] is False
    from google.genai import types
    assert types.HttpOptions(**calls[0]["http_options"]).timeout == 60000
    assert calls[0]["http_options"]["retry_options"]["attempts"] == 3
    types.GenerateContentConfig(response_json_schema={"type":"object"}, response_mime_type="application/json")


@pytest.mark.parametrize("code,failures,expected_calls", [(503, 2, 3), (503, 3, 3), (401, 1, 1), (429, 1, 1)])
def test_gemini_sdk_transient_retries_are_bounded(monkeypatch, code, failures, expected_calls):
    genai = pytest.importorskip("google.genai")
    import httpx
    from lens.llm.gemini import RETRY_OPTIONS
    requests = []
    def transport(request):
        requests.append(request)
        if len(requests) <= failures:
            return httpx.Response(code, json={"error":{"code":code,"message":"PRIVATE_SECRET","status":"UNAVAILABLE"}})
        return httpx.Response(200, json={"candidates":[{"finishReason":"STOP","content":{"role":"model","parts":[{"text":'{"evidence_status":"insufficient","answer":[]}'}]}}]})
    http = httpx.Client(transport=httpx.MockTransport(transport))
    options = dict(RETRY_OPTIONS, initial_delay=0.001, max_delay=0.002, jitter=0)
    actual_client = genai.Client(api_key="test-placeholder", vertexai=False,
                                 http_options={"httpx_client":http,"retry_options":options})
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    captured = []
    monkeypatch.setattr(genai, "Client", lambda **kwargs: captured.append(kwargs) or actual_client)
    provider = GeminiProvider(replace(LLMSettings(), provider="gemini", model="gemini-3.8-flash"))
    try:
        if failures < expected_calls:
            assert parse_answer(provider.generate([{"role":"user","content":"question"}]).text, {}).evidence_status == "insufficient"
        else:
            with pytest.raises(RuntimeError) as caught:
                provider.generate([{"role":"user","content":"question"}])
            assert "PRIVATE_SECRET" not in str(caught.value)
            if code == 503:
                assert "temporarily unavailable" in str(caught.value)
                assert caught.value.inference["status"] == "error"
        assert len(requests) == expected_calls
        assert captured[0]["http_options"]["retry_options"] == RETRY_OPTIONS
    finally:
        actual_client.close()


def test_selection_precedence_persistence_and_no_reindex(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "qwen3:8b")
    assert load_settings().model == "qwen3:8b"
    save_selection(load_settings("qwen3:4b"))
    assert load_settings().model == "qwen3:4b"
    assert load_settings("gemini/gemini-3.8-flash").provider == "gemini"
    assert load_settings("qwen3:8b").model == "qwen3:8b"
    with pytest.raises(ValueError, match="outside"):
        save_selection(load_settings(), [{"root":str(tmp_path)}])
    assert create_provider(load_settings("qwen3:4b")).settings.provider == "ollama"
    for model in ("../bad", "qwen3:8b-cloud", "gemini/unknown"):
        with pytest.raises(ValueError):
            identify(model)


def test_catalogue_distinguishes_supported_installed_and_unknown_account(monkeypatch, tmp_path):
    import lens.llm.models as models
    monkeypatch.setenv("LENS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(models.OllamaProvider, "health", lambda self: {"models":[{"name":"qwen3:4b"},{"name":"extra:latest"}]})
    rows = catalogue(LLMSettings())["models"]
    assert next(r for r in rows if r["model"] == "qwen3:8b")["installed"] is False
    assert next(r for r in rows if r["model"] == "extra:latest")["installed"] is True
    assert all(r["account_available"] is None for r in rows if r["provider"] == "gemini")


def test_diagnostics_redact_and_refuse_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "TEST_KEY_PRIVATE")
    result = redact({"api_key":"x", "prompt":"key TEST_KEY_PRIVATE password=embedded-secret", "url":"postgresql://u:p@host/db"})
    assert "PRIVATE" not in json.dumps(result) and "embedded-secret" not in json.dumps(result)
    path = tmp_path / "trace.json"
    write_trace(path, result)
    with pytest.raises(FileExistsError):
        write_trace(path, {})
    assert redact("AIza" + "x" * 35) == "[REDACTED]"
    assert redact("sk-proj-" + "x" * 25) == "[REDACTED]"
    assert redact("api_key: process.env.GEMINI_API_KEY") == "api_key: process.env.GEMINI_API_KEY"


def test_credentials_are_redacted_before_provider_messages():
    from lens.llm.context import EvidenceContext
    from lens.llm.prompts import messages
    secret = "AIza" + "x" * 35
    context = EvidenceContext("p", ({"id":"E-a","excerpt":f'new Client("{secret}")',"entity_names":["client"]},), (), False, 1)
    sent = messages("Which provider?", context, "developer", {})
    assert secret not in json.dumps(sent) and "[REDACTED]" in sent[1]["content"]


def test_media_storage_trigger_does_not_capture_image_analysis():
    assert understand("Where are the user images stored?").concepts == ("storage",)
    assert understand("Where are photos saved?").semantic
    assert "storage" not in understand("Which service analyzes report images?").concepts


def test_contradiction_and_unrelated_intent_fail_closed():
    evidence = {"E-1":{"entity_names":["notify"]}}
    with pytest.raises(ValueError, match="contradictory"):
        parse_answer(json.dumps({"evidence_status":"sufficient", "answer":[{"text":"Insufficient evidence, but images use Cloudinary", "basis":"static_fact", "citations":["E-1"]}]}), evidence)
    with pytest.raises(ValueError, match="unrelated"):
        parse_intent(json.dumps({"evidence_status":"insufficient", "intent":"unsupported", "subject":"mining", "entities":["notify"], "constraints":[], "citations":["E-1"]}), evidence)
    with pytest.raises(ValueError, match="Too few"):
        validate([], {"type":"array", "items":{"type":"string"}, "minItems":1})


def test_real_citation_with_absent_code_literal_rejected():
    evidence = {"E-ui":{"excerpt":"save report image locally", "file":"frontend.js"},
                "E-upload":{"excerpt":"folder: `uploads/reports/${report_id}`", "file":"upload.js"}}
    value = {"evidence_status":"sufficient", "answer":[{"text":"Folder is `uploads/reports/${report_id}`.",
             "basis":"static_fact", "citations":["E-ui"]}]}
    with pytest.raises(ValueError, match="Quoted code literal"):
        parse_answer(json.dumps(value), evidence)
    value["answer"][0]["citations"] = ["E-upload"]
    assert parse_answer(json.dumps(value), evidence).evidence_status == "sufficient"


def test_runtime_and_ownership_checks_preserve_failure_trace():
    from lens.llm.answering import synthesize
    from lens.llm.provider import Generation
    from lens.core.models import DocumentChunk, SearchHit
    from lens.search.retrieval import SearchResponse
    response = SearchResponse([SearchHit(DocumentChunk("c","e","notify","function","a.js","javascript",
                                                      "function notify() {}", {"entity_name":"notify"}))], {})
    class Fake:
        def __init__(self, value):
            self.value = value
        def generate(self, *args):
            return Generation(json.dumps(self.value))
    with pytest.raises(ValueError, match="runtime state") as caught:
        synthesize("Did notify successfully deliver today?", response,
                   Fake({"evidence_status":"sufficient","answer":[{"text":"Delivery succeeded.","basis":"static_fact","citations":["E-c"]}]}), "p")
    assert caught.value.trace["raw_response"] and caught.value.performance["status"] == "error"
    result = synthesize("Show my notifications", response,
                       Fake({"evidence_status":"insufficient","intent":"data_query","subject":"notifications","entities":[],"constraints":[],"citations":[]}), "p", "user")
    assert list(result.output["constraints"]) == [{"field":"owner","operator":"equals","value":"current_user"}]
    result = synthesize("Can this application mine cryptocurrency?", response,
                       Fake({"evidence_status":"insufficient","intent":"unsupported","subject":"mining","entities":[],"constraints":[],"citations":[]}), "p", "user")
    assert not result.output["constraints"]


def test_official_sdk_serializes_actual_context_schema_without_network(monkeypatch):
    genai = pytest.importorskip("google.genai")
    import httpx
    from lens.llm.context import EvidenceContext
    from lens.llm.schemas import generation_schema
    from lens.llm.prompts import messages
    seen = []
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"candidates":[{"finishReason":"STOP","content":{"role":"model","parts":[{"text":'{"evidence_status":"insufficient","answer":[]}'}]}}],
                                         "usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5}})
    http = httpx.Client(transport=httpx.MockTransport(transport))
    client = genai.Client(api_key="test-placeholder", vertexai=False, http_options={"httpx_client":http})
    provider = GeminiProvider(replace(LLMSettings(), provider="gemini", model="gemini-3.8-flash"), client)
    context = EvidenceContext("p", ({"id":"E-a","entity_names":["upload"],"excerpt":"upload()"},), (), False, 1)
    schema = generation_schema("developer", context)
    result = provider.generate(messages("Where are images stored?", context, "developer", schema), schema)
    assert parse_answer(result.text, context.evidence).evidence_status == "insufficient"
    from lens.llm.gemini import gemini_schema
    assert seen[0]["generationConfig"]["responseJsonSchema"] == gemini_schema(schema)
    assert "anyOf" not in seen[0]["generationConfig"]["responseJsonSchema"]
    assert "systemInstruction" in seen[0] and "tools" not in seen[0]
    client.close()


def test_definition_fast_path_requires_exact_query_and_global_uniqueness():
    from lens.llm.answering import definition_answer
    from lens.core.models import DocumentChunk, SearchHit
    from time import perf_counter
    hit = SearchHit(DocumentChunk("c", "e", "upload", "function", "src/upload.js", "javascript",
                                  "function upload() {}", {"entity_name":"upload", "line_start":2, "line_end":3}))
    class Store:
        def __init__(self, hits):
            self.hits = hits
        def definition_hits(self, name, project):
            assert name == "upload" and project == "p"
            return self.hits
    direct = definition_answer("Where is upload defined?", Store([hit]), "p", 12000, perf_counter())
    assert direct.performance["model_invoked"] is False
    assert direct.citations[0]["file"] == "src/upload.js" and direct.output["evidence_status"] == "sufficient"
    assert definition_answer("Where is upload defined?", Store([hit, hit]), "p", 12000, perf_counter()) is None
    assert definition_answer("Where is upload defined?", Store([]), "p", 12000, perf_counter()) is None
    assert definition_answer("Where are uploaded images stored?", Store([hit]), "p", 12000, perf_counter()) is None


def test_ownership_canonicalization_rejects_conflicts_and_preserves_bounds():
    from lens.llm.answering import preserve_ownership
    from lens.llm.schemas import Constraint, UserIntent
    parsed = UserIntent("insufficient", "data_query", "reports", (),
                        (Constraint("status","equals","pending"), Constraint("userID","equals","my")), ())
    normalized = preserve_ownership("Show my pending reports", parsed)
    assert normalized.constraints == (Constraint("status","equals","pending"), Constraint("owner","equals","current_user"))
    assert preserve_ownership("Can this application mine cryptocurrency?", parsed) == parsed
    with pytest.raises(ValueError, match="contradicts"):
        preserve_ownership("Show my reports", UserIntent("insufficient", "data_query", "reports", (),
                           (Constraint("owner","equals","Bob"),), ()))
    with pytest.raises(ValueError, match="bound"):
        preserve_ownership("Show my reports", UserIntent("insufficient", "data_query", "reports", (),
                           tuple(Constraint(str(i),"equals","x") for i in range(12)), ()))


def test_subject_checks_reject_real_but_unrelated_citations_without_claiming_entailment():
    from lens.llm.schemas import DeveloperAnswer, Statement, subject_support
    parsed = DeveloperAnswer("sufficient", (Statement("Cloudinary uploads report images.", "static_fact", ("E-a",)),))
    evidence = {"E-a":{"excerpt":"cloudinary.uploader.upload_stream fileBuffer report image", "entity_names":["upload"]}}
    with pytest.raises(ValueError, match="provider subject"):
        subject_support("What quantum computing provider runs this application?", parsed, evidence)
    with pytest.raises(ValueError, match="profile-image scope"):
        subject_support("Are user profile pictures stored in the report folder?", parsed, evidence)
    subject_support("What storage provider is used?", parsed, evidence)
    subject_support("Where are the user images stored?", parsed, evidence)
    subject_support("What quantum computing provider is used?", parsed, {"E-a":{"excerpt":"quantum computing client"}})


def test_literal_support_accepts_cited_const_path_composition_only():
    from lens.llm.schemas import Statement, literal_support
    excerpt = 'const folder = `${FileSystem.documentDirectory}reports/${report.id}/`; copy({to: `${folder}image.jpeg`});'
    evidence = {"E-a":{"kind":"function","excerpt":excerpt}}
    statement = Statement('Saved to `${FileSystem.documentDirectory}reports/${report.id}/image.jpeg`.', "static_fact", ("E-a",))
    literal_support(statement, evidence)
    for changed in [excerpt.replace("const folder", "let folder"),
                    excerpt + ' folder = `different/`;',
                    excerpt.replace("reports/", "other/"),
                    'function outer(){function inner(){' + excerpt + '}}',
                    'copy({to: `${folder}image.jpeg`});']:
        with pytest.raises(ValueError, match="absent"):
            literal_support(statement, {"E-a":{"kind":"function","excerpt":changed}})
    with pytest.raises(ValueError, match="absent"):
        literal_support(statement, {"E-a":{"kind":"module","excerpt":excerpt}})


def test_gemini_timeout_is_short_by_default_and_explicitly_configurable(monkeypatch):
    from lens.llm.gemini import request_timeout
    monkeypatch.delenv("GEMINI_TIMEOUT", raising=False)
    assert request_timeout(LLMSettings()) == 60
    assert request_timeout(replace(LLMSettings(), timeout=20)) == 20
    monkeypatch.setenv("GEMINI_TIMEOUT", "120")
    assert request_timeout(LLMSettings()) == 120
    for invalid in ("invalid", "nan", "inf", "0", "1801"):
        monkeypatch.setenv("GEMINI_TIMEOUT", invalid)
        with pytest.raises(ValueError, match="GEMINI_TIMEOUT"):
            request_timeout(LLMSettings())
