"""Provider factory and persistent answer-model selection, separate from embeddings."""

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import re

from lens.llm.ollama import LLMSettings, OllamaProvider
from lens.core.project_context import state_path

GEMINI_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash-lite")


def selection_path():
    return state_path().with_name("model-selection.json")


def identify(model, provider=None):
    if model.startswith(("ollama/", "gemini/")):
        prefix, model = model.split("/", 1)
        if provider and prefix != provider:
            raise ValueError("Model prefix conflicts with provider")
        provider = prefix
    provider = provider or ("gemini" if model.startswith("gemini-") else "ollama")
    if provider not in {"ollama", "gemini"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", model):
        raise ValueError("Invalid provider or model ID")
    if provider == "gemini" and model not in GEMINI_MODELS:
        raise ValueError("Unsupported Gemini model; run 'lens list models'")
    if provider == "ollama" and model.lower().endswith((":cloud", "-cloud")):
        raise ValueError("Select downloaded local Ollama weights")
    return provider, model


def load_settings(model=None, provider=None):
    # Explicit override > saved pair > environment > safe defaults.
    if model is None and selection_path().exists():
        try:
            saved = json.loads(selection_path().read_text(encoding="utf-8"))
            saved_provider, saved_model = identify(saved["model"], saved["provider"])
            if provider is None or provider == saved_provider:
                provider, model = saved_provider, saved_model
        except (ValueError, KeyError, TypeError):
            raise ValueError("Invalid saved model selection; run 'lens model use <model>'") from None
    if model is None:
        provider = provider or os.getenv("LLM_PROVIDER", "ollama")
        configured = os.getenv("LLM_MODEL")
        # A provider-only override must not inherit another provider's model.
        if configured:
            inferred, candidate = identify(configured)
            model = candidate if inferred == provider else None
        model = model or (GEMINI_MODELS[0] if provider == "gemini" else "qwen3:8b")
    provider, model = identify(model, provider)
    # Existing generation knobs remain the single source of configuration.
    return replace(LLMSettings.load(model), provider=provider, model=model)


def create_provider(settings):
    if settings.provider == "ollama":
        from lens.llm.ollama import OllamaProvider
        return OllamaProvider(settings)
    from lens.llm.gemini import GeminiProvider
    return GeminiProvider(settings)


def describe(settings):
    result = asdict(settings)
    result.update(local=settings.provider == "ollama",
                  privacy="Local inference" if settings.provider == "ollama" else
                  "Selected prompt and source evidence sent to Google; usage may incur cost",
                  selection_file=str(selection_path()))
    if settings.provider == "gemini":
        from lens.llm.gemini import request_timeout
        result.update(request_timeout_seconds=request_timeout(settings),
                      sampling="Gemini defaults; LLM_TEMPERATURE applies to Ollama")
    return result


def save_selection(settings, projects=()):
    path = selection_path().resolve()
    if any(path.is_relative_to(Path(p["root"]).resolve()) for p in projects):
        raise ValueError("Model selection must live outside indexed repositories")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"provider": settings.provider, "model": settings.model}), encoding="utf-8")
    temporary.replace(path)


def catalogue(settings):
    installed, error = {}, None
    try:
        local = OllamaProvider(replace(settings, provider="ollama", model="qwen3:8b"))
        installed = {m["name"]: m for m in local.health().get("models", [])
                     if isinstance(m, dict) and isinstance(m.get("name"), str)
                     and not m.get("remote_host") and not m.get("remote_model")
                     and not m["name"].endswith((":cloud", "-cloud"))}
    except (ValueError, RuntimeError, OSError) as exc:
        error = str(exc)
    rows = []
    for model in dict.fromkeys(["qwen3:4b", "qwen3:8b", *installed]):
        rows.append({"provider": "ollama", "model": model, "display_name": model,
                     "local": True, "installed": model in installed,
                     "selected": settings.provider == "ollama" and settings.model == model,
                     "strengths": "Historically stronger complete intent interpretation" if model == "qwen3:8b" else "Local inference; 4B historically decoded faster" if model == "qwen3:4b" else "Discovered local weights; quality unmeasured",
                     "weaknesses": "Slow on measured hardware" if model == "qwen3:8b" else "4B historically missed intent constraints" if model == "qwen3:4b" else "Lens quality and schema adherence unverified",
                     "latency": "Hardware/context dependent; see historical measurements",
                     "capabilities": "JSON schema requested; shared validation always applied",
                     "hardware": installed.get(model, {}).get("details"),
                     "credentials": "None", "privacy": "Loopback local weights only"})
    for model in GEMINI_MODELS:
        rows.append({"provider": "gemini", "model": model, "display_name": model,
                     "local": False, "configured": bool(os.getenv("GEMINI_API_KEY")),
                     "account_available": None, "generation_verified": False,
                     "selected": settings.provider == "gemini" and settings.model == model,
                     "strengths": "Official remote structured-output API",
                     "weaknesses": "Network, quotas and credentials required; Lens quality unmeasured",
                     "latency": "Unmeasured", "capabilities": "JSON schema plus shared validation",
                     "hardware": "Remote inference", "credentials": "GEMINI_API_KEY",
                     "privacy": "Selected prompt/evidence sent to Google; may incur cost"})
    return {"models": rows, "ollama_discovery_error": error,
            "gemini_discovery": "Curated supported catalogue; account access is not inferred"}
