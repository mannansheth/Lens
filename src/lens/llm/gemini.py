"""Official optional Google GenAI transport; shared Lens validation remains authoritative."""

import os
import math
from time import perf_counter
from lens.llm.provider import Generation

RETRY_OPTIONS = {"attempts": 3, "initial_delay": 1.0, "max_delay": 2.0,
                 "exp_base": 2.0, "jitter": 0.5,
                 "http_status_codes": [500, 502, 503, 504]}


def gemini_schema(schema):
    """Send the live-tested shape subset; enforce all value constraints locally."""
    result = {key: value for key, value in schema.items()
              if key not in {"minLength", "maxLength", "uniqueItems", "anyOf", "additionalProperties",
                             "enum", "minItems", "maxItems"}}
    if "properties" in result:
        result["properties"] = {name: gemini_schema(child) for name, child in result["properties"].items()}
    if isinstance(result.get("items"), dict):
        result["items"] = gemini_schema(result["items"])
    # Live Developer API calls rejected Lens's richer constraint combination.
    # Keep the base shape; the unchanged prompt/local validators enforce values,
    # item bounds, citation membership, extra fields and the status/answer condition.
    # Standalone unions remain supported without sibling shape constraints.
    if "anyOf" in schema and "type" not in schema and "properties" not in schema:
        result["anyOf"] = [gemini_schema(child) for child in schema["anyOf"]]
    return result


def request_timeout(settings):
    try:
        timeout = float(os.environ["GEMINI_TIMEOUT"]) if "GEMINI_TIMEOUT" in os.environ else min(settings.timeout, 60)
    except ValueError:
        raise ValueError("GEMINI_TIMEOUT must be 1–1800 seconds") from None
    if not math.isfinite(timeout) or not 1 <= timeout <= 1800:
        raise ValueError("GEMINI_TIMEOUT must be 1–1800 seconds")
    return timeout


class GeminiProvider:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.request_timeout = request_timeout(settings)
        if client is not None:
            self.client = client
            return
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise ValueError("Gemini requires GEMINI_API_KEY; configure it in your environment")
        try:
            from google import genai
        except ImportError:
            raise RuntimeError('Install the official SDK with pip install -e ".[gemini]"') from None
        self.client = genai.Client(api_key=key, vertexai=False,
                                  http_options={"timeout": int(self.request_timeout * 1000),
                                                "retry_options": dict(RETRY_OPTIONS)})

    def _error(self, error):
        # SDK exception strings may include URLs/credentials; never propagate them.
        code = getattr(error, "code", None)
        if code == 503:
            return RuntimeError("Gemini service temporarily unavailable (status 503). "
                                "Try again shortly, select another Gemini model, or use "
                                "--model qwen3:8b for local generation if installed.")
        if code == 400:
            return RuntimeError("Gemini rejected the request (status 400): invalid argument. "
                                "Check model compatibility and structured-output settings; retrying unchanged will not help.")
        name = type(error).__name__.lower()
        category = ("authentication" if code in {401, 403} else "quota/rate limit" if code == 429
                    else "missing model" if code == 404 else "timeout" if "timeout" in name
                    else "network/API")
        return RuntimeError(f"Gemini {category} failure (status {code if isinstance(code, int) else 'unknown'})")

    def health(self):
        try:
            return {"models": [{"name": m.name} for m in self.client.models.list()]}
        except Exception as error:
            raise self._error(error) from None

    def model_info(self):
        try:
            model = self.client.models.get(model=self.settings.model)
            actions = getattr(model, "supported_actions", None)
            if actions is not None and "generateContent" not in actions:
                raise ValueError("Gemini model does not advertise generateContent")
            return {"name": model.name, "supported_actions": actions, "generation_verified": False}
        except ValueError:
            raise
        except Exception as error:
            raise self._error(error) from None

    def generate(self, messages, schema=None):
        started = perf_counter()
        telemetry = {"provider": "gemini", "model": self.settings.model, "status": "ok",
                     "request_timeout_seconds":self.request_timeout, "max_attempts":RETRY_OPTIONS["attempts"]}
        if sum(len(m["content"].encode("utf-8")) for m in messages) + self.settings.output_tokens + 1024 > self.settings.context_tokens:
            raise ValueError("Prompt exceeds conservative context budget")
        config = {"system_instruction": "\n".join(m["content"] for m in messages if m["role"] == "system"),
                  "max_output_tokens": self.settings.output_tokens}
        if schema is not None:
            config.update(response_mime_type="application/json", response_json_schema=gemini_schema(schema))
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": m["content"]}]} for m in messages if m["role"] != "system"]
        try:
            try:
                from lens.core.logging import event
                event("answer_generation_started", provider="gemini", model=self.settings.model,
                      request_timeout_seconds=self.request_timeout, max_attempts=RETRY_OPTIONS["attempts"])
                response = self.client.models.generate_content(model=self.settings.model, contents=contents, config=config)
            except Exception as error:
                raise self._error(error) from None
            candidates = response.candidates or []
            if len(candidates) != 1 or getattr(candidates[0].finish_reason, "value", candidates[0].finish_reason) != "STOP":
                raise RuntimeError("Gemini blocked, truncated or incomplete response")
            parts = getattr(getattr(candidates[0], "content", None), "parts", None) or []
            if any(getattr(p, "function_call", None) or getattr(p, "executable_code", None) or getattr(p, "code_execution_result", None) for p in parts):
                raise RuntimeError("Gemini tool responses are unsupported")
            text = response.text
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("Gemini returned no text")
            usage = getattr(response, "usage_metadata", None)
            counts = [getattr(usage, k, None) for k in ("prompt_token_count", "candidates_token_count")]
            if any(c is not None and (type(c) is not int or c < 0) for c in counts):
                raise RuntimeError("Invalid Gemini usage metadata")
            telemetry.update(request_seconds=perf_counter() - started,
                             input_tokens=counts[0], output_tokens=counts[1])
            return Generation(text, *counts, telemetry)
        except (ValueError, RuntimeError, OSError) as error:
            telemetry.update(status="error", request_seconds=perf_counter() - started, error=str(error))
            error.inference = telemetry
            raise
