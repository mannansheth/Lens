"""Observation must not alter generation, validation, local boundaries or CLI streams."""

import json
import pytest
from lens.llm.ollama import LLMSettings, OllamaProvider
from lens.llm.answering import synthesize
from lens.llm.provider import Generation
from lens.search.retrieval import SearchResponse


def provider(monkeypatch, metrics):
    llm = OllamaProvider(LLMSettings())
    calls = []
    monkeypatch.setattr(llm, "model_info", lambda: {"name": "local"})
    def request(path, body):
        calls.append(body)
        return {"done": True, "model": "qwen3:8b", "message": {"content": "{}"}, **metrics}
    monkeypatch.setattr(llm, "_request", request)
    return llm, calls


def test_raw_nanoseconds_seconds_and_token_rates_do_not_change_request(monkeypatch):
    llm, calls = provider(monkeypatch, {"total_duration": 9_000_000_000, "load_duration": 1_000_000_000,
                         "prompt_eval_duration": 2_000_000_000, "prompt_eval_count": 200,
                         "eval_duration": 6_000_000_000, "eval_count": 30})
    result = llm.generate([{"role": "user", "content": "data"}], {"type": "object"})
    t = result.telemetry
    assert t["total_duration"] == 9_000_000_000 and t["total_duration_seconds"] == 9
    assert t["load_duration_seconds"] == 1 and t["prompt_eval_duration_seconds"] == 2
    assert t["generation_tokens_per_second"] == 5 and t["prompt_tokens_per_second"] == 100
    assert result.text == "{}" and result.input_tokens == 200 and result.output_tokens == 30
    assert t["request_seconds"] >= t["chat_seconds"] >= 0 and t["status"] == "ok"
    assert calls == [{"model": "qwen3:8b", "messages": [{"role": "user", "content": "data"}],
                      "stream": False, "think": False,
                      "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 1024, "seed": 0},
                      "format": {"type": "object"}}]


@pytest.mark.parametrize("metrics", [{}, {"eval_duration": 0, "eval_count": 0},
                                    {"eval_duration": -1, "load_duration": True},
                                    {"prompt_eval_duration": "wrong", "total_duration": 2**100}])
def test_optional_bad_or_missing_telemetry_does_not_reject_old_valid_output(monkeypatch, metrics):
    llm, _ = provider(monkeypatch, metrics)
    result = llm.generate([{"role": "user", "content": "data"}])
    assert result.text == "{}" and result.telemetry["generation_tokens_per_second"] is None
    json.dumps(result.telemetry, allow_nan=False)


def test_timeout_attached_without_response_metrics_or_source(monkeypatch):
    llm, _ = provider(monkeypatch, {})
    def failed(*args):
        raise RuntimeError("Ollama request timed out")
    monkeypatch.setattr(llm, "_request", failed)
    with pytest.raises(RuntimeError, match="timed out") as caught:
        llm.generate([{"role": "user", "content": "private source"}])
    t = caught.value.inference
    assert t["timeout"] and t["status"] == "error" and t["request_seconds"] >= 0
    assert t["eval_count"] is None and t["eval_duration"] is None
    assert "private source" not in json.dumps(t)


def test_preflight_failure_does_not_send_evidence(monkeypatch):
    llm, calls = provider(monkeypatch, {})
    def missing():
        raise RuntimeError("Ollama model missing")
    monkeypatch.setattr(llm, "model_info", missing)
    with pytest.raises(RuntimeError) as caught:
        llm.generate([{"role": "user", "content": "private source"}])
    assert not calls and caught.value.inference["chat_seconds"] is None
    assert caught.value.inference["preflight_seconds"] >= 0


def test_old_providers_and_empty_context_still_work():
    class Fake:
        def generate(self, *args):
            return Generation('{"evidence_status":"insufficient","intent":"unsupported",'
                              '"subject":"unknown","entities":[],"constraints":[],"citations":[]}', 1, 1)
    result = synthesize("Unknown capability?", SearchResponse([], {}), Fake(), "p", "user")
    assert result.performance["inference"] is None
    result = synthesize("Unknown capability?", SearchResponse([], {}), Fake(), "p")
    assert not result.performance["model_invoked"] and result.performance["inference"] is None


def test_invalid_output_retains_generation_timings_but_fails_closed():
    class Fake:
        def generate(self, *args):
            return Generation("malformed", 1, 1, {"status": "ok", "model": "test", "eval_count": 1})
    with pytest.raises(ValueError) as caught:
        synthesize("Unknown?", SearchResponse([], {}), Fake(), "p", "user")
    assert caught.value.performance["inference"]["eval_count"] == 1
    assert caught.value.performance["status"] == "error"
    assert caught.value.performance["llm_seconds"] >= 0
