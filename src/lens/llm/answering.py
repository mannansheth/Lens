"""Read-only evidence -> untrusted provider generation -> validated response."""

from dataclasses import asdict, dataclass, replace
from time import perf_counter
import re
from lens.llm.context import build_context
from lens.llm.prompts import VERSION, messages
from lens.llm.provider import LLMProvider
from lens.llm.schemas import Constraint, generation_schema, parse_answer, parse_intent, subject_support
from lens.search.retrieval import search

ABSTENTION = "I don't have enough evidence in the indexed application to answer that."


def evidence_limit(query, expand=False):
    """Use a wider candidate set for explicit inventory/whole-system questions."""
    if re.search(r"\b(page|screen|menu|navigate|navigation|access|open|find|export|edit)\b", query, re.I):
        return 10
    broad = re.search(r"\b(all|every|entire|complete|comprehensive|list|enumerate)\b", query, re.I)
    return 12 if expand or broad else 5


@dataclass(frozen=True)
class AskResponse:
    mode: str
    project: str
    output: dict
    citations: tuple[dict, ...]
    performance: dict
    debug: dict

    def to_dict(self, debug=False):
        result = asdict(self)
        if not debug:
            result.pop("debug")
        from lens.llm.diagnostics import redact
        return redact(result)


def synthesize(query, response, llm: LLMProvider, project, mode="developer", context_chars=12000, database_schema=None):
    performance = {"retrieval_seconds": response.debug.get("retrieval_seconds"),
                   "context_seconds": None, "llm_seconds": None, "inference": None}
    try:
        return _synthesize(query, response, llm, project, mode, context_chars, performance, database_schema)
    except (ValueError, RuntimeError, OSError) as error:
        if getattr(error, "inference", None) is not None:
            performance["inference"] = error.inference
        error.performance = performance
        performance["status"] = "error"
        performance["error"] = str(error)
        raise


def _synthesize(query, response, llm, project, mode, context_chars, performance, database_schema):
    if mode not in {"developer", "user", "client"}:
        raise ValueError("Unknown ask mode")
    if not query.strip() or len(query) > 1000:
        raise ValueError("Ask query must contain 1–1000 characters")
    start = perf_counter()
    context = build_context(response, project, context_chars, database_schema, query)
    context_seconds = perf_counter() - start
    performance["context_seconds"] = context_seconds
    schema = generation_schema(mode, context)
    sent = messages(query, context, mode, schema)
    from lens.llm.diagnostics import redact
    settings = getattr(llm, "settings", None)
    trace = {"query": query, "project": project, "prompt_version": VERSION,
             "context": asdict(context), "retrieval": response.debug,
             "messages": sent, "schema": schema,
             "generation_settings": asdict(settings) if settings is not None else None,
             "raw_response": None, "validation": "not_run"}
    llm_start = perf_counter()
    if not context.items and mode in {"developer", "client"}:
        # An empty context cannot produce application facts, so do not ask the model.
        status = "insufficient" if context.retrieved_count else "no_evidence"
        import json
        raw = json.dumps({"evidence_status": status, "answer": []})
        input_tokens = output_tokens = None
        model_invoked = False
    else:
        try:
            generation = llm.generate(sent, schema)
        except (ValueError, RuntimeError, OSError) as error:
            error.trace = redact(trace)
            raise
        finally:
            performance["llm_seconds"] = perf_counter() - llm_start
        performance["inference"] = generation.telemetry
        raw, input_tokens, output_tokens = generation.text, generation.input_tokens, generation.output_tokens
        model_invoked = True
    llm_seconds = perf_counter() - llm_start
    performance["llm_seconds"] = llm_seconds
    trace["raw_response"] = raw
    try:
        parsed = validate_generation(query, context, raw, mode)
        trace["validation"] = "passed_structural_checks; semantic_support_not_proven"
    except ValueError as error:
        trace["validation"] = str(error)
        error.trace = redact(trace)
        raise
    result = asdict(parsed)
    trace["parsed_response"] = result
    if mode in {"developer", "client"}:
        ids = list(dict.fromkeys(cid for statement in parsed.answer for cid in statement.citations))
        result["abstention"] = ABSTENTION if parsed.evidence_status not in {"sufficient", "partial"} else None
        if mode == "client":
            result["navigation"] = client_navigation(context, ids, query)
    else:
        ids = list(parsed.citations)
    citations = tuple({key: context.evidence[cid].get(key) for key in
                       ("id", "source_uid", "file", "line_start", "line_end", "kind", "entity_names", "static_only")}
                      for cid in ids)
    performance.update({"retrieval_seconds": response.debug.get("retrieval_seconds"),
                   "context_seconds": context_seconds, "llm_seconds": llm_seconds,
                   "context_chars": len(context.render()),
                   "context_bytes": len(context.render().encode("utf-8")),
                   "prompt_chars": sum(len(item["content"]) for item in sent),
                   "input_tokens": input_tokens, "output_tokens": output_tokens,
                   "model_invoked": model_invoked})
    return AskResponse(mode, project, result, citations, performance,
                       redact(trace))


def ask(query, store, embeddings, llm: LLMProvider, project, mode="developer", context_chars=12000, alpha=0.5, database_schema=None):
    if not project:
        raise ValueError("Ask requires one selected project")
    if not query.strip() or len(query) > 1000:
        raise ValueError("Ask query must contain 1–1000 characters")
    if mode not in {"developer", "user", "client"}:
        raise ValueError("Unknown ask mode")
    begun = perf_counter()
    if mode in {"developer", "client"}:
        direct = definition_answer(query, store, project, context_chars, begun)
        if direct is not None:
            return direct
    from lens.search.multihop import intent
    limit = evidence_limit(query, intent(query)["expand"])
    response = search(query, store, embeddings, project=project, alpha=alpha, top_k=limit)
    try:
        result = synthesize(query, response, llm, project, mode, context_chars, database_schema)
    except (ValueError, RuntimeError, OSError) as error:
        if getattr(error, "performance", None) is not None:
            error.performance["total_seconds"] = perf_counter() - begun
        raise
    result.performance["total_seconds"] = perf_counter() - begun
    return result


def preserve_ownership(query, parsed):
    """Canonicalize explicit possession, never the verb 'mine cryptocurrency'."""
    requested = re.search(r"\bmy\b|\b(?:are|is|only|show|list|of)\s+mine\b", query, re.I)
    if not requested:
        return parsed
    if any(c.field == "owner" and c.value not in {"my", "mine", "current_user"} for c in parsed.constraints):
        raise ValueError("User intent contradicts explicit ownership")
    if any(c.field == "owner" and c.operator == "equals" and c.value == "current_user" for c in parsed.constraints):
        return parsed
    constraints = tuple(c for c in parsed.constraints if not (c.value in {"my", "mine", "current_user"} and c.field.lower() in {"owner", "userid", "user_id", "user"}))
    if len(constraints) >= 12:
        raise ValueError("Explicit ownership would exceed the intent constraint bound")
    return replace(parsed, constraints=(*constraints, Constraint("owner", "equals", "current_user")))


def validate_generation(query, context, raw, mode):
    """One shared validation path for live providers and opt-in frozen-response replay."""
    parsed = parse_answer(raw, context.evidence) if mode in {"developer", "client"} else parse_intent(raw, context.evidence)
    if mode in {"developer", "client"}:
        subject_support(query, parsed, context.evidence)
        if parsed.evidence_status == "sufficient" and re.search(r"\b(successfully.*today|currently stored|saved in production today|currently.*production)\b", query, re.I):
            raise ValueError("Static evidence cannot establish the requested runtime state")
    else:
        parsed = preserve_ownership(query, parsed)
    if parsed.evidence_status == "no_evidence" and context.retrieved_count:
        raise ValueError("Retrieval was nonempty; use insufficient for omitted or irrelevant evidence")
    return parsed


def client_navigation(context, citations, query=""):
    """Expose source-backed UI destinations as data for the host application."""
    destinations, actions, steps = [], [], []
    for evidence_id in citations:
        item = context.evidence.get(evidence_id, {})
        links = list(item.get("navigation_links", []))
        if links and query:
            from lens.search.semantics import words
            requested = words(query) - {"where", "how", "can", "i", "do", "the", "a", "an", "of", "to", "for", "all", "me", "find", "see", "view", "page"}
            scored = [(len(requested & words(" ".join(str(link.get(key, "")) for key in
                       ("label", "controller", "action", "href", "condition")))), link) for link in links]
            best = max((score for score, _ in scored), default=0)
            if best:
                links = [link for score, link in scored if score == best]
        # Prefer the link-level facts filtered for this question. Relationship
        # edges are a fallback for action-only evidence; appending all of them
        # reintroduces unrelated links (for example proforma and receipt beside
        # an invoice button).
        if not links:
            for relation in item.get("navigation_relationships", []):
                if relation.get("kind") != "NAVIGATES_TO":
                    continue
                target = relation.get("target_name", "")
                controller, _, action = target.partition(".")
                links.append({"controller": controller, "action": action,
                              "href": relation.get("href", ""), "label": relation.get("label", ""),
                              "line": relation.get("source_line_start")})
        for link in links:
            href = str(link.get("href", "")).strip()
            controller, action = str(link.get("controller", "")), str(link.get("action", ""))
            if (href == "#" or href.lower().startswith("javascript:void")) and not (controller or action):
                continue
            label = link.get("label")
            if not label and str(link.get("condition", "")).startswith("btn-"):
                label = str(link["condition"])[4:].replace("-", " ").title()
            match = re.match(r"^/([A-Za-z_][\w-]*)/([A-Za-z_][\w-]*)(?:[?#].*)?$", href)
            if match:
                controller, action = controller or match[1], action or match[2]
            route_values = link.get("route_values", {})
            dynamic = bool(re.search(r"\$\{|@[A-Za-z_]\w*|<[^>]+>|\{[A-Za-z_$][^}]*\}", href) or
                           any(re.search(r"@|\$\{|<[^>]+>", str(value)) for value in route_values.values()))
            destination = {"label": label or None,
                           "page_title": None,
                           "source_page_title": item.get("source_page_title") or item.get("page_title") or None,
                           "path_template": href or None,
                           "direct_url": href if href.startswith(("http://", "https://")) and not dynamic else None,
                           "controller": controller or None, "action": action or None,
                           "routing_basis": link.get("origin") or ("literal_ui_link" if href else "controller_action_link"),
                           "route_expression": link.get("href_expression") or None,
                           "query_parameters": sorted(set(re.findall(r"[?&]([A-Za-z_][\w-]*)=", href)) | set(route_values)),
                           "route_values": route_values,
                           "condition": link.get("condition") or None,
                           "dynamic_values_present": dynamic, "evidence_id": evidence_id,
                           "source": {"file": item.get("file"), "line_start": link.get("line") or item.get("line_start")}}
            destination_key = (destination["path_template"], destination["controller"], destination["action"],
                               tuple(destination["query_parameters"]), tuple(sorted(destination["route_values"].items())))
            existing_keys = {(value["path_template"], value["controller"], value["action"],
                              tuple(value["query_parameters"]), tuple(sorted(value["route_values"].items())))
                             for value in destinations}
            if destination_key not in existing_keys:
                destinations.append(destination)
            if label:
                step = {"instruction": "Choose " + str(label), "evidence_id": evidence_id}
                if step not in steps:
                    steps.append(step)
        if item.get("kind") == "method" and item.get("navigation_candidate"):
            qualified = next((name for name in item.get("entity_names", []) if "." in name), "")
            pieces = qualified.split(".")
            if len(pieces) >= 2:
                action = {"controller": pieces[-2], "action": pieces[-1], "evidence_id": evidence_id,
                          "source": {"file": item.get("file"), "line_start": item.get("line_start")}}
                if action not in actions:
                    actions.append(action)
    status = ("verified_destination" if any(item.get("path_template") or item.get("direct_url") for item in destinations)
              else "action_only" if destinations or actions else "no_verified_destination")
    gaps = [] if destinations else ["The cited evidence does not establish a user-facing link or menu path."]
    return {"status": status, "destinations": destinations, "actions": actions, "steps": steps, "gaps": gaps}


def definition_answer(query, store, project, context_chars, begun):
    """Only exact, globally unambiguous indexed declaration locations bypass generation."""
    match = re.fullmatch(r"\s*where is (?:the )?(?:(function|class|table) )?`?([A-Za-z_$][\w.$]*)`? (?:defined|declared)\??\s*", query, re.I)
    if not match or not hasattr(store, "definition_hits"):
        return None
    kind, name = match.groups()
    hits = store.definition_hits(name, project)
    if kind:
        hits = [h for h in hits if h.chunk.kind == kind.lower()]
    if len(hits) != 1:
        return None
    from lens.search.retrieval import SearchResponse
    import json
    retrieval = perf_counter() - begun
    context_start = perf_counter()
    context = build_context(SearchResponse(hits, {}), project, context_chars)
    if len(context.items) != 1:
        return None
    item = context.items[0]
    text = f"`{name}` is defined in `{item['file']}` at lines {item['line_start']}–{item['line_end']}."
    raw = json.dumps({"evidence_status":"sufficient", "answer":[{"text":text, "basis":"static_fact", "citations":[item["id"]]}]})
    parsed = asdict(parse_answer(raw, context.evidence))
    parsed["abstention"] = None
    citations = tuple({key:item.get(key) for key in ("id","source_uid","file","line_start","line_end","kind","entity_names","static_only")} for item in context.items)
    performance = {"retrieval_seconds":retrieval, "context_seconds":perf_counter() - context_start,
                   "llm_seconds":0.0, "inference":None, "input_tokens":None, "output_tokens":None,
                   "model_invoked":False, "total_seconds":perf_counter() - begun,
                   "fast_path":"unique_indexed_definition"}
    return AskResponse("developer", project, parsed, citations, performance,
                       {"query":query, "context":asdict(context), "fast_path":"unique_indexed_definition",
                        "validation":"direct indexed location; full-project exact-name uniqueness checked"})
