"""Local-only, non-streaming Ollama transport using the standard library."""

from dataclasses import dataclass
import ipaddress
import json
import math
import os
import socket
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from lens.llm.provider import Generation


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "ollama"
    model: str = "qwen3:8b"
    host: str = "http://127.0.0.1:11434"
    timeout: float = 600
    temperature: float = 0
    context_tokens: int = 32768
    output_tokens: int = 1024
    context_chars: int = 12000

    def __post_init__(self):
        parsed = urlsplit(self.host)
        try:
            # The Compose deployment uses one fixed, private service DNS name.
            # All host-side configurations remain loopback-only.
            local = (parsed.hostname in {"localhost", "ollama"} or
                     ipaddress.ip_address(parsed.hostname).is_loopback)
            parsed.port
        except (ValueError, TypeError):
            local = False
        if not local or parsed.scheme != "http" or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("LLM_HOST must be a loopback HTTP address without credentials or paths")
        if self.provider not in {"ollama", "gemini"}:
            raise ValueError("LLM_PROVIDER must be ollama or gemini")
        if self.model.lower().endswith((":cloud", "-cloud")):
            raise ValueError("Cloud-backed models are excluded; select downloaded local weights")
        if not self.model.strip() or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 1800:
            raise ValueError("Invalid LLM model or timeout (1–1800 seconds)")
        if not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise ValueError("LLM_TEMPERATURE must be between 0 and 2")
        if not 2048 <= self.context_tokens <= 32768 or not 128 <= self.output_tokens <= 4096:
            raise ValueError("Invalid LLM token bounds")
        if not 2000 <= self.context_chars <= 24000 or self.context_chars + 9000 + self.output_tokens > self.context_tokens:
            # Conservative worst-case character/token upper bound, including prompts/query.
            raise ValueError("LLM context budget + prompt reserve + output must fit context tokens")

    @classmethod
    def load(cls, model: str | None = None):
        return cls(provider=os.getenv("LLM_PROVIDER", "ollama"), model=model or os.getenv("LLM_MODEL", "qwen3:8b"),
                   host=os.getenv("LLM_HOST", "http://127.0.0.1:11434"),
                   timeout=float(os.getenv("LLM_TIMEOUT", "600")), temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
                   context_tokens=int(os.getenv("LLM_CONTEXT_TOKENS", "32768")),
                   output_tokens=int(os.getenv("LLM_OUTPUT_TOKENS", "1024")),
                   context_chars=int(os.getenv("LLM_CONTEXT_CHARS", "12000")))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Ollama redirects are refused; inference must remain local")


class OllamaProvider:
    def __init__(self, settings: LLMSettings):
        self.settings = settings
        self._transport = build_opener(ProxyHandler({}), NoRedirect())

    def _request(self, path: str, body: dict | None = None) -> dict:
        request = Request(self.settings.host.rstrip("/") + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json"})
        try:
            with self._transport.open(request, timeout=self.settings.timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise RuntimeError("Ollama response exceeds the bounded response size")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Expected object")
            return value
        except HTTPError as error:
            raise RuntimeError(f"Ollama HTTP {error.code}; check local service and configured model") from error
        except (TimeoutError, socket.timeout) as error:
            raise RuntimeError("Ollama request timed out; increase LLM_TIMEOUT or reduce context") from error
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise RuntimeError("Ollama request timed out") from error
            raise RuntimeError("Ollama unavailable; start 'ollama serve' locally") from error
        except (ValueError, UnicodeError) as error:
            raise RuntimeError("Malformed Ollama response") from error

    def health(self) -> dict:
        value = self._request("/api/tags")
        if not isinstance(value.get("models"), list):
            raise RuntimeError("Malformed Ollama model list")
        return value

    def model_info(self) -> dict:
        wanted = self.settings.model if ":" in self.settings.model else self.settings.model + ":latest"
        for model in self.health()["models"]:
            if isinstance(model, dict) and model.get("name") == wanted:
                if model.get("remote_host") or model.get("remote_model"):
                    raise RuntimeError("Remote-backed Ollama models are excluded")
                details = self._request("/api/show", {"model": wanted})
                if details.get("remote_host") or details.get("remote_model"):
                    raise RuntimeError("Remote-backed Ollama models are excluded")
                if not isinstance(details.get("model_info"), dict) or not details["model_info"]:
                    raise RuntimeError("Ollama model has no local weight metadata")
                return model
        raise RuntimeError(f"Ollama model {wanted!r} is missing; install explicitly with 'ollama pull {wanted}'")

    def generate(self, messages: list[dict], schema: dict | None = None) -> Generation:
        # Optional observation only: never change the request or validate source differently.
        started = perf_counter()
        telemetry = {"model": self.settings.model, "status": "ok", "error": None,
                     "timeout": False, "request_seconds": None, "chat_seconds": None,
                     "preflight_seconds": None}
        for name in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration",
                     "prompt_eval_count", "eval_count", "prompt_eval_cached_count"):
            telemetry[name] = None
            if name.endswith("duration"):
                telemetry[name + "_seconds"] = None
        telemetry.update(prompt_tokens_per_second=None, generation_tokens_per_second=None,
                         response_model=None, invalid_metrics=[])
        try:
            result = self._generate(messages, schema, telemetry)
            telemetry["request_seconds"] = perf_counter() - started
            return Generation(result.text, result.input_tokens, result.output_tokens, telemetry)
        except (ValueError, RuntimeError, OSError) as error:
            telemetry.update(request_seconds=perf_counter() - started, status="error",
                             error=str(error), timeout="timed out" in str(error).lower())
            error.inference = telemetry
            raise

    def _generate(self, messages, schema, telemetry):
        required = sum(len(item["content"].encode("utf-8")) for item in messages) + 1024 + self.settings.output_tokens
        if required > self.settings.context_tokens:
            raise ValueError("Prompt exceeds conservative model context budget; reduce LLM_CONTEXT_CHARS")
        preflight = perf_counter()
        try:
            self.model_info()
        finally:
            telemetry["preflight_seconds"] = perf_counter() - preflight
        # Allocate only the conservative prompt/output bound, up to the configured ceiling.
        # This avoids reserving a large idle KV cache on modest local GPUs.
        window = min(self.settings.context_tokens, max(2048, ((required + 2047) // 2048) * 2048))
        request = {"model": self.settings.model, "messages": messages, "stream": False,
                   "think": False, "options": {"temperature": self.settings.temperature,
                   "num_ctx": window, "num_predict": self.settings.output_tokens,
                   "seed": 0}}
        if schema is not None:
            request["format"] = schema
        chat_started = perf_counter()
        try:
            value = self._request("/api/chat", request)
        finally:
            telemetry["chat_seconds"] = perf_counter() - chat_started
        # Ollama durations are nanoseconds. Missing/invalid optional metrics are unknown,
        # not zero and not a reason to reject a previously acceptable generation.
        invalid = []
        for name in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration",
                     "prompt_eval_count", "eval_count", "prompt_eval_cached_count"):
            metric = value.get(name)
            if metric is not None and (type(metric) is not int or not 0 <= metric <= 2**63 - 1):
                invalid.append(name)
                metric = None
            telemetry[name] = metric
            if name.endswith("duration"):
                telemetry[name + "_seconds"] = metric / 1e9 if metric is not None else None
        telemetry["invalid_metrics"] = invalid
        for prefix, count, duration in (("prompt", "prompt_eval_count", "prompt_eval_duration"),
                                       ("generation", "eval_count", "eval_duration")):
            tokens, elapsed = telemetry[count], telemetry[duration]
            telemetry[prefix + "_tokens_per_second"] = tokens * 1e9 / elapsed if tokens is not None and elapsed else None
        telemetry["response_model"] = value.get("model") if isinstance(value.get("model"), str) else None
        if value.get("remote_host") or value.get("remote_model"):
            raise RuntimeError("Remote-backed Ollama generation is excluded")
        message = value.get("message")
        if value.get("done") is not True or value.get("done_reason") == "length" or not isinstance(message, dict) or message.get("tool_calls"):
            raise RuntimeError("Incomplete or unsupported Ollama response")
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Malformed Ollama output: missing text")
        counts = [value.get(name) for name in ("prompt_eval_count", "eval_count")]
        if any(count is not None and (type(count) is not int or count < 0) for count in counts):
            raise RuntimeError("Malformed Ollama token counts")
        return Generation(text, *counts)
