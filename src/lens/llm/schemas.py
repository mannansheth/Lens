"""Strict JSON schemas and typed, fail-closed validation of untrusted generation."""

from dataclasses import dataclass
import json
import math
import re
from copy import deepcopy

STATUSES = ["sufficient", "partial", "insufficient", "conflicting", "no_evidence"]
INTENTS = ["capability_query", "data_query", "navigation_query", "general_question", "unsupported"]


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}
CITATIONS = {"type": "array", "items": TEXT, "maxItems": 16, "uniqueItems": True}
STATUS = {"type": "string", "enum": STATUSES}
ANSWER_SCHEMA = obj({"evidence_status": STATUS, "answer": {"type": "array", "maxItems": 8,
    "items": obj({"text": TEXT, "basis": {"type": "string", "enum": ["static_fact", "inference"]},
                  "citations": CITATIONS})}})
INTENT_SCHEMA = obj({"evidence_status": STATUS, "intent": {"type": "string", "enum": INTENTS},
    "subject": TEXT, "entities": {"type": "array", "items": TEXT, "maxItems": 12, "uniqueItems": True},
    "constraints": {"type": "array", "maxItems": 12, "items": obj({"field": TEXT,
        "operator": {"type": "string", "enum": ["equals", "contains", "before", "after"]}, "value": TEXT})},
    "citations": CITATIONS})


def generation_schema(mode, context):
    """Constrain generation to this context, then still validate independently."""
    schema = deepcopy(ANSWER_SCHEMA if mode in {"developer", "client"} else INTENT_SCHEMA)
    citations = schema["properties"]["answer"]["items"]["properties"]["citations"] if mode in {"developer", "client"} else schema["properties"]["citations"]
    # deepcopy preserves shared schema-node aliases. Detach the specific nodes before
    # adding per-context enums, so text/subject/constraint values remain free text.
    citations["items"] = deepcopy(citations["items"])
    if context.items:
        citations["items"]["enum"] = list(context.evidence)
        if mode in {"developer", "client"}:
            citations["minItems"] = 1
        schema["properties"]["evidence_status"]["enum"] = [s for s in STATUSES if s != "no_evidence"]
    else:
        citations["maxItems"] = 0
        schema["properties"]["evidence_status"]["enum"] = ["insufficient" if context.retrieved_count else "no_evidence"]
    if mode == "user":
        schema["properties"]["entities"]["items"] = deepcopy(schema["properties"]["entities"]["items"])
        names = list(dict.fromkeys(name for item in context.items for name in item["entity_names"]))
        if names:
            schema["properties"]["entities"]["items"]["enum"] = names
        else:
            schema["properties"]["entities"]["maxItems"] = 0
    else:
        sufficient, abstaining = deepcopy(schema), deepcopy(schema)
        sufficient["properties"]["evidence_status"]["enum"] = [s for s in ("sufficient", "partial") if s in schema["properties"]["evidence_status"]["enum"]]
        sufficient["properties"]["answer"]["minItems"] = 1
        abstaining["properties"]["evidence_status"]["enum"] = [s for s in schema["properties"]["evidence_status"]["enum"] if s not in {"sufficient", "partial"}]
        abstaining["properties"]["answer"]["maxItems"] = 0
        schema["anyOf"] = ([sufficient] if not abstaining["properties"]["evidence_status"]["enum"] else
                           [abstaining] if "sufficient" not in schema["properties"]["evidence_status"]["enum"] else
                           [sufficient, abstaining])
    return schema


def validate(value, schema):
    kind = schema["type"]
    valid = {"object": type(value) is dict, "array": type(value) is list,
             "string": type(value) is str}[kind]
    if not valid:
        raise ValueError(f"LLM output requires {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Invalid LLM output enum")
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise ValueError("Missing or unsupported LLM output fields")
        for key, item in value.items():
            validate(item, schema["properties"][key])
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise ValueError("Too few LLM output items")
        if len(value) > schema.get("maxItems", 100):
            raise ValueError("Too many LLM output items")
        for item in value:
            validate(item, schema["items"])
        if schema.get("uniqueItems") and len(set(value)) != len(value):
            raise ValueError("Duplicate LLM output items")
    elif not schema.get("minLength", 0) <= len(value.strip()) <= schema.get("maxLength", 2000):
        raise ValueError("Empty or oversized LLM output text")


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def decode(text, schema):
    try:
        value = json.loads(text, object_pairs_hook=_object,
                           parse_constant=lambda token: float(token))
        # Non-finite values are rejected even before schema traversal.
        def finite(item):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Non-finite JSON value")
            if isinstance(item, dict):
                for child in item.values():
                    finite(child)
            elif isinstance(item, list):
                for child in item:
                    finite(child)
        finite(value)
        validate(value, schema)
        return value
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError(f"Invalid structured LLM output: {error}") from error


@dataclass(frozen=True)
class Statement:
    text: str
    basis: str
    citations: tuple[str, ...]


@dataclass(frozen=True)
class DeveloperAnswer:
    evidence_status: str
    answer: tuple[Statement, ...]


@dataclass(frozen=True)
class Constraint:
    field: str
    operator: str
    value: str


@dataclass(frozen=True)
class UserIntent:
    evidence_status: str
    intent: str
    subject: str
    entities: tuple[str, ...]
    constraints: tuple[Constraint, ...]
    citations: tuple[str, ...]


def citation_check(ids, evidence):
    if any(item not in evidence for item in ids):
        raise ValueError("LLM invented a citation outside supplied evidence")


def template_forms(item):
    """Bounded symbolic expansion of const templates in a single cited function."""
    if item.get("kind") not in {"function", "method"}:
        return []
    excerpt = item.get("excerpt", "")
    # Avoid joining constants across nested function scopes in a large excerpt.
    if len(re.findall(r"=>|\bfunction\s+\w+\s*\(", excerpt)) > 1:
        return []
    declarations = re.findall(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*`([^`]{1,2000})`", excerpt)
    constants = {}
    for name, value in declarations:
        assignments = re.findall(r"(?<![\w$.])" + re.escape(name) + r"\s*=(?!=)", excerpt)
        if len(assignments) == 1:
            constants[name] = value
    forms = []
    for value in re.findall(r"`([^`]{1,2000})`", excerpt):
        seen = {value}
        for _ in range(4):
            expanded = re.sub(r"\$\{([A-Za-z_$][\w$]*)\}",
                              lambda match: constants.get(match.group(1), match.group(0)), value)
            if expanded in seen or len(expanded) > 4000:
                break
            forms.append(expanded)
            seen.add(expanded)
            value = expanded
    return forms


def literal_support(statement, evidence):
    """Necessary support for explicit code literals; never a semantic entailment claim."""
    cited = "\n".join(str(evidence[cid].get(key, "")) for cid in statement.citations
                       for key in ("excerpt", "file", "entity_names", "literal_sql", "evidence"))
    cited += "\n" + "\n".join(form for cid in statement.citations for form in template_forms(evidence[cid]))
    def compact(text):
        return re.sub(r"\s+", "", text).casefold()
    for literal in re.findall(r"`([^`]+)`", statement.text):
        if len(literal.strip()) >= 3 and compact(literal) not in compact(cited):
            raise ValueError("Quoted code literal is absent from the statement's cited evidence")


def subject_support(query, parsed, evidence):
    """Necessary lexical subject coverage for narrow explicit scopes, not entailment."""
    if parsed.evidence_status not in {"sufficient", "partial"}:
        return
    cited_ids = {cid for statement in parsed.answer for cid in statement.citations}
    text = " ".join(str(evidence[cid].get(key, "")) for cid in cited_ids
                    for key in ("excerpt", "file", "entity_names")).casefold()
    if re.search(r"\b(?:profile|avatar)\s+(?:pictures?|images?|photos?)\b", query, re.I):
        if not re.search(r"\b(?:profile|avatar)", text):
            raise ValueError("Cited report-image evidence does not establish the requested profile-image scope")
    match = re.match(r"\s*(?:what|which)\s+(.+?)\s+(?:provider|integration|service)\b", query, re.I)
    if match:
        from lens.search.semantics import CONCEPTS, words
        generic = {"the", "a", "an", "external", "local", "remote", "cloud", "application"}
        generic.update(word for values in CONCEPTS.values() for phrase in values for word in words(phrase))
        subject = words(match.group(1)) - generic
        if subject and not subject <= words(text):
            raise ValueError("Cited evidence does not address the explicitly requested provider subject")


def parse_answer(text, evidence):
    value = decode(text, ANSWER_SCHEMA)
    statements = tuple(Statement(row["text"], row["basis"], tuple(row["citations"])) for row in value["answer"])
    if value["evidence_status"] not in {"sufficient", "partial"} and statements:
        raise ValueError("Nonsufficient answers must abstain with an empty answer")
    for statement in statements:
        if value["evidence_status"] == "sufficient" and re.search(r"\b(?:not enough evidence|insufficient evidence|cannot answer|can't answer|don't have enough evidence)\b", statement.text, re.I):
            raise ValueError("Sufficient answer contains contradictory abstention text")
        if statement.text in evidence:
            raise ValueError("Answer requires an explanation, not only an evidence identifier")
        if not statement.citations:
            raise ValueError("Every answer statement must cite supplied evidence")
        citation_check(statement.citations, evidence)
        literal_support(statement, evidence)
    if value["evidence_status"] in {"sufficient", "partial"} and not statements:
        raise ValueError("Missing answer for sufficient evidence")
    if value["evidence_status"] == "no_evidence" and (statements or evidence):
        raise ValueError("no_evidence is reserved for empty retrieval context")
    if not evidence and (value["evidence_status"] not in {"no_evidence", "insufficient"} or statements):
        raise ValueError("Cannot answer without evidence")
    return DeveloperAnswer(value["evidence_status"], statements)


def parse_intent(text, evidence):
    value = decode(text, INTENT_SCHEMA)
    citation_check(value["citations"], evidence)
    if value["intent"] == "unsupported" and (value["entities"] or value["citations"]):
        raise ValueError("Unsupported intent must not cite unrelated application entities")
    if value["evidence_status"] in {"sufficient", "partial"} and not value["citations"]:
        raise ValueError("Grounded intent requires citations")
    if value["evidence_status"] == "no_evidence" and (evidence or value["citations"] or value["entities"]):
        raise ValueError("no_evidence is reserved for empty retrieval context")
    if not evidence and value["evidence_status"] not in {"no_evidence", "insufficient"}:
        raise ValueError("Cannot ground intent without evidence")
    names = {name for cid in value["citations"] for name in evidence[cid].get("entity_names", [])}
    if any(name not in names for name in value["entities"]):
        raise ValueError("Intent entities must identify cited application entities")
    return UserIntent(value["evidence_status"], value["intent"], value["subject"], tuple(value["entities"]),
                      tuple(Constraint(**row) for row in value["constraints"]), tuple(value["citations"]))
