"""Bounded, provider-independent concept interpretation and source vocabulary."""
import re
from dataclasses import asdict, dataclass
from lens.search.ranking_signals import technical_names

# These are general software concepts, never vendor names or repository identifiers.
CONCEPTS = {
    "language_model": ("llm", "language model", "sentiment", "emotion", "text classification", "ai", "generative", "model", "models", "generate content", "prediction", "inference"),
    "storage": ("storage", "upload", "attachment", "object storage", "bucket", "uploader"),
    "notifications": ("notification", "notifications", "messaging", "message", "push"),
    "transcription": ("transcription", "transcribe", "speech", "audio", "transcript", "audio converted to text"),
    "authentication": ("authentication", "authenticate", "auth", "login", "session", "credential"),
    "document": ("pdf", "document", "render", "font", "report documents"),
    "submission": ("submission", "submit", "form", "attachment"),
    "repository": ("repository", "repositories", "code hosting", "metadata"),
}
TRIGGERS = {
    "language_model": ("llm", "language model", "sentiment analysis", "sentiment model", "emotion model", "generative model", "ai"),
    "storage": ("storage", "upload", "uploads", "attachments"),
    "notifications": ("notification", "notifications", "push messages"),
    "transcription": ("transcription", "speech", "audio", "transcribe"),
    "authentication": ("authentication", "authenticates", "administrator sessions"),
    "document": ("pdf", "documents"), "submission": ("submission", "submitting"),
    "repository": ("repository information", "repository metadata", "code hosting", "repository api"),
}


def words(value):
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    value = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", value)
    tokens = set(re.findall(r"[a-z0-9]+", value.lower()))
    return tokens | {t[:-1] for t in tokens if len(t) > 4 and t.endswith("s") and not t.endswith("ss")}


def phrase(value, text):
    return bool(re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text))


@dataclass(frozen=True)
class QueryUnderstanding:
    original: str
    identifiers: tuple[str, ...]
    concepts: tuple[str, ...]
    intents: tuple[str, ...]
    expansions: tuple[str, ...]
    scopes: tuple[str, ...]
    relationship_kinds: tuple[str, ...]
    actions: tuple[str, ...]
    entity_kinds: tuple[str, ...]
    semantic: bool

    def describe(self):
        return asdict(self)


def understand(query):
    lower = query.lower()
    concepts = tuple(key for key, triggers in TRIGGERS.items() if any(phrase(t, lower) for t in triggers))
    # Media alone can mean analysis/rendering; require an explicit storage question.
    if "storage" not in concepts and re.search(r"\b(images?|photos?|pictures?|media|files?)\b", lower) and re.search(r"\b(stored|storage|saved|uploaded|uploads?)\b", lower):
        concepts += ("storage",)
    intents = []
    for intent, pattern in (
        ("provider", r"\b(provider|llm|what .*model|which .*model)\b"),
        ("dependency", r"\b(external services|depend on|dependencies)\b"),
        ("integration", r"\b(integration|external|remote|cloud)\b"),
        ("configuration", r"\b(configured|configuration|config)\b"),
        ("location", r"\b(where|defined)\b"),
        ("definition", r"\b(what is|what does)\b"),
        ("implementation", r"\b(implemented|implementation|handles|converts|converted)\b"),
        ("data_flow", r"\b(flow|submission|submitting)\b"),
        ("architecture", r"\b(architecture|layers)\b"),
        ("troubleshooting", r"\b(fails|error|failure|broken)\b"),
    ):
        if re.search(pattern, lower):
            intents.append(intent)
    identifiers = tuple(sorted(technical_names(query) - {"llm", "ai", "pdf"}))
    if "language_model" in concepts and re.search(r"\b(what|which)\b", lower) and "provider" not in intents:
        intents.append("technology")
    expansions = tuple(dict.fromkeys(" ".join(CONCEPTS[key]) for key in concepts))[:4]
    kinds = tuple(kind for word, kind in (("call", "CALLS"), ("render", "RENDERS"),
                  ("request", "REQUESTS"), ("import", "IMPORTS")) if word in lower)
    actions = tuple(action for action in ("generate", "upload", "send", "fetch", "analyze", "convert", "validate", "submit")
                    if any(word.startswith(action) for word in words(lower)))
    entity_kinds = tuple(kind for kind in ("function", "component", "table", "procedure", "service", "configuration") if phrase(kind, lower))
    return QueryUnderstanding(query, identifiers, concepts, tuple(intents), expansions,
        tuple(scope for scope in ("frontend", "backend") if scope in lower), kinds, actions, entity_kinds,
        bool((concepts or any(i in intents for i in ("dependency", "integration", "provider"))) and not identifiers))


def evidence_concepts(text):
    tokens = words(text)
    return [key for key, values in CONCEPTS.items() if any(words(value) <= tokens for value in values)]


def relevance(understanding, chunk):
    facts = chunk.metadata.get("semantic", {})
    if not facts:
        source_words = words(chunk.text)
        facts = {"concepts": evidence_concepts(chunk.text), "vocabulary": list(source_words),
                 "actions": [action for action in ("analyze", "convert", "classify", "generate", "upload", "send", "validate")
                             if action in source_words]}
    matches = sorted(set(understanding.concepts) & set(facts.get("concepts", [])))
    coverage = len(matches) / len(understanding.concepts) if understanding.concepts else 0
    integration = bool(facts.get("integrations")) or chunk.kind == "integration"
    external = bool(facts.get("hosts")) or chunk.metadata.get("integration_type") == "host" or facts.get("remote_configuration", False)
    # Equal-weight evidence dimensions, all inspectable. Source roles > name similarity.
    strength = min(1, len(facts.get("actions", [])) / 4)
    provider_intent = any(i in understanding.intents for i in ("provider", "integration", "dependency", "technology"))
    action_concepts = set(evidence_concepts(" ".join([*facts.get("actions", []), *facts.get("sql_operations", [])])))
    action_match = bool(action_concepts & set(understanding.concepts)) or bool(facts.get("environment_names") and matches) or bool(set(understanding.actions) & set(facts.get("vocabulary", [])))
    score = (coverage + int(action_match) + (int(integration) + int(external) if provider_intent else strength * 2)) / 4
    if "dependency" in understanding.intents and not understanding.concepts:
        score = (int(integration) + int(external)) / 2
    return score, {"matched_concepts": matches, "integration_evidence": integration,
                   "external_host_evidence": external, "semantic_score": score}
