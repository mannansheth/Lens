"""Categorical evidence priorities, with no hidden or learned weighting."""

import re
from lens.core.models import DocumentChunk
from lens.search.frontend_resolution import path_matches


def technical_names(query: str) -> set[str]:
    words = re.findall(r"[A-Za-z_$][\w$]*(?:\.[\w$]+)*", query)
    stop = {"where", "what", "which", "how", "get", "post", "put", "delete", "react", "html", "api"}
    return {word.lower() for word in words if word.lower() not in stop and
            ("_" in word or "." in word or re.search(r"[a-z][A-Z]", word) or word[0].isupper())}


def signals(query: str, chunk: DocumentChunk) -> dict:
    names = technical_names(query)
    local_name = chunk.metadata.get("entity_name", chunk.name.rsplit(".", 1)[-1].split("::")[-1]).lower()
    exact = local_name in names or chunk.name.lower() in names
    lower = query.lower()
    navigation_query = bool(re.search(r"\b(page|screen|menu|navigate|navigation|access|open|find|where|edit|view|export|invoice|journal|employee|payroll|billing)\b", lower))
    navigation_evidence = chunk.metadata.get("navigation_candidate", False)
    navigation_score, navigation_terms = 0.0, []
    route_feature_score = 0.0
    if navigation_query and navigation_evidence:
        from lens.search.semantics import words
        ignored = {"where", "which", "page", "screen", "menu", "can", "i", "how", "the", "to", "find",
                   "access", "open", "view", "for", "all", "do", "does", "a", "an", "of", "in",
                   "on", "me", "my", "this", "that", "staff", "user", "users",
                   "please", "company", "people", "show", "tell", "about", "get", "give"}
        query_terms = words(query) - ignored
        # Expand common user vocabulary only for ranking candidates; this never
        # establishes support or changes the abstention contract.
        synonyms = {"edit": {"update", "modify"}, "information": {"info", "details", "profile"},
                    "payroll": {"salary", "wage"}, "invoice": {"invoices"},
                    "receipt": {"receipts", "payment"},
                    "review": {"summary", "billing"}, "export": {"excel", "download"},
                    "info": {"details", "information", "profile"},
                    "particular": {"selected", "id", "specific"}}
        expanded = set(query_terms)
        for term in query_terms:
            expanded.update(synonyms.get(term, ()))
        evidence_text = " ".join([
            chunk.name, chunk.file, str(chunk.metadata.get("page_title", "")),
            " ".join(" ".join(str(link.get(key, "")) for key in
                ("label", "href", "route_template", "controller", "action", "condition", "query_parameters"))
                for link in chunk.metadata.get("navigation_links", [])),
            " ".join(str(route.get("path", "")) for route in chunk.metadata.get("routes", [])),
        ])
        evidence_terms = words(evidence_text)
        for link in chunk.metadata.get("navigation_links", []):
            for parameter in link.get("query_parameters", []):
                key = str(parameter).lower()
                if key.startswith("journal"):
                    evidence_terms.add("journal")
                if "id" in key:
                    evidence_terms.add("id")
                if "year" in key:
                    evidence_terms.add("year")
                if "month" in key:
                    evidence_terms.add("month")
                if "company" in key:
                    evidence_terms.add("company")
                if "employee" in key or key.startswith("emp"):
                    evidence_terms.add("employee")
        specific_query = query_terms - {"journal", "employee", "client", "staff", "page", "information", "info"}
        weights = {}
        for term in sorted(expanded):
            if term in evidence_terms:
                weight = 0.45 if term in {"journal", "employee", "client"} and specific_query else 1.0
                navigation_score += weight
                navigation_terms.append(term)
                weights[term] = weight
        # Score a destination from its own route evidence. Combining tokens
        # across unrelated links on a large page can make a generic Journal
        # screen look like an invoice destination simply because some other
        # link mentions invoices. Keep the best individual route as a distinct
        # feature and reward route parameters that express the requested scope.
        for link in chunk.metadata.get("navigation_links", []):
            link_text = " ".join(str(link.get(key, "")) for key in
                ("label", "href", "route_template", "controller", "action", "condition", "query_parameters"))
            link_terms = words(link_text)
            params = {str(parameter).lower() for parameter in link.get("query_parameters", [])}
            if any(parameter.startswith("journal") for parameter in params):
                link_terms.add("journal")
            if any("id" in parameter for parameter in params):
                link_terms.add("id")
            if any("year" in parameter for parameter in params):
                link_terms.add("year")
            score = sum(weight for term, weight in weights.items() if term in link_terms)
            # User phrases such as "a particular journal" imply a scoped
            # destination; an explicit journal identifier parameter is strong
            # evidence even if the wording uses a different synonym.
            if "particular" in query_terms and ("invoice" in link_terms or "invoices" in link_terms) and (
                    any(parameter.startswith("journal") and "id" in parameter for parameter in params) or
                    re.search(r"journal\w*id|journal\{[^}]*id", link_text, re.I)):
                score += 1.25
            route_feature_score = max(route_feature_score, score)
    relation_kind = "RENDERS" if re.search(r"\b(render|renders|rendering)\b", lower) else "CALLS" if re.search(r"\b(call|calls|calling)\b", lower) else "IMPORTS" if re.search(r"\b(import|imports)\b", lower) else None
    relationships = chunk.metadata.get("relation_evidence", [])
    matching = [relation for relation in relationships if relation["direction"] == "outgoing"
                and relation["kind"] == relation_kind and relation.get("target_id") and
                relation["target_name"].split(".")[-1].lower() in names]
    urls = [value.rstrip(".,;?!") for value in re.findall(r"/(?:[\w:.*{}-]+/?)+", query)]
    url_match = False
    ui_route_match = any(route["path"] in urls for route in [*chunk.metadata.get("routes", []), *chunk.metadata.get("incoming_routes", [])])
    if urls:
        wants_frontend = "frontend" in lower or "editor send" in lower
        wants_caller = "calls" in lower or "sends" in lower or "send " in lower
        endpoint = chunk.metadata.get("endpoint")
        candidates = chunk.metadata.get("requests", []) if wants_frontend or wants_caller else ([endpoint] if endpoint else [])
        methods = set(re.findall(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", query))
        for candidate in candidates:
            path = candidate.get("resolved_path", candidate.get("path", ""))
            if methods and candidate.get("method") not in methods:
                continue
            if any(path == url or path_matches(path, url) or path_matches(url, path) for url in urls):
                url_match = True
    requested_kind = next((kind for word, kind in (("component", "component"), ("function", "function"), ("table", "table"), ("procedure", "procedure"))
                           if re.search(r"\b" + word + r"\b", lower)), None)
    kind_match = chunk.kind == requested_kind or (requested_kind == "component" and chunk.kind == "component_class")
    requested_scopes = [scope for scope in ("frontend", "backend") if scope in lower]
    requested_scope = requested_scopes[0] if len(requested_scopes) == 1 else None
    scope_match = requested_scope is not None and requested_scope in chunk.file.split("/")
    document_topic_match = False
    if chunk.metadata.get("source_type") == "documentation" or chunk.language == "documentation":
        ignored = {"explain", "describe", "tell", "show", "list", "all", "used", "use", "the", "a", "an",
                   "how", "what", "which", "does", "do", "is", "are", "about", "project", "please"}
        query_topics = set(re.findall(r"[a-z0-9]+", lower)) - ignored
        document_topics = set(re.findall(r"[a-z0-9]+", chunk.file.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()))
        document_topics.update(re.findall(r"[a-z0-9]+", str(chunk.metadata.get("document_heading", "")).lower()))
        document_topics.update(re.findall(r"[a-z0-9]+", chunk.name.lower()))
        document_topic_match = bool(query_topics & document_topics)
    return {"exact_identifier": bool(exact), "relationship_match": bool(matching),
            "relationship_kind": relation_kind, "matching_relationships": matching,
            "api_path_match": url_match, "ui_route_match": ui_route_match,
            "navigation_query": navigation_query, "navigation_match": navigation_score > 0,
            "navigation_match_score": navigation_score, "navigation_matched_terms": navigation_terms,
            "navigation_route_feature_score": route_feature_score,
            "navigation_evidence": bool(navigation_evidence),
            "requested_kind": requested_kind, "entity_kind_match": kind_match,
            "requested_scope": requested_scope, "source_scope_match": scope_match,
            "document_topic_match": document_topic_match}
