"""Categorical evidence priorities, with no hidden or learned weighting."""

import re
from lens.core.models import DocumentChunk
from lens.search.frontend_resolution import path_matches


NAVIGATION_QUERY = re.compile(
    r"\b(page|screen|menu|navigate|navigation|access|open|find|edit|view|export|change|update|manage)\b|\bwhere\s+(?:can|do|should)\s+i\s+(?:go|find|open|access)\b|\bhow\s+(?:can|do)\s+i\s+(?:get\s+to|open|find|access)\b",
    re.I,
)


def is_navigation_query(query: str) -> bool:
    return bool(NAVIGATION_QUERY.search(query))


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
    navigation_query = is_navigation_query(query)
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
        # Keep this signal corpus-neutral: only exact lexical overlap with
        # indexed navigation evidence is considered here. General synonym
        # expansion belongs in the language model/embedding representation,
        # not in repository-specific hand-written ranking rules.
        expanded = set(query_terms)
        evidence_text = " ".join([
            chunk.name, chunk.file, str(chunk.metadata.get("page_title", "")),
            " ".join(str(value) for value in chunk.metadata.get("ui_text", [])),
            " ".join(str(value) for value in chunk.metadata.get("string_evidence", [])),
            " ".join(" ".join(str(link.get(key, "")) for key in
                ("label", "href", "route_template", "controller", "action", "condition", "query_parameters"))
                for link in chunk.metadata.get("navigation_links", [])),
            " ".join(str(route.get("path", "")) for route in
                     [*chunk.metadata.get("routes", []), *chunk.metadata.get("incoming_routes", [])]),
            str((chunk.metadata.get("endpoint") or {}).get("path", "")),
        ])
        evidence_terms = words(evidence_text)
        for term in sorted(expanded):
            if term in evidence_terms:
                navigation_score += 1.0
                navigation_terms.append(term)
        # Score each route independently so tokens from unrelated links on a
        # large page cannot combine into a false match for a destination.
        for link in chunk.metadata.get("navigation_links", []):
            link_text = " ".join(str(link.get(key, "")) for key in
                ("label", "href", "route_template", "controller", "action", "condition", "query_parameters"))
            link_terms = words(link_text)
            score = len(query_terms & link_terms)
            route_feature_score = max(route_feature_score, score)
        # React Router pages carry a statically resolved incoming route rather
        # than a Razor-style navigation link. Use that page's own visible text
        # to rank it for a feature question without treating text as a URL.
        if chunk.metadata.get("incoming_routes"):
            route_text = " ".join([
                chunk.name, chunk.file, str(chunk.metadata.get("page_title", "")),
                " ".join(str(route.get("path", "")) for route in chunk.metadata.get("incoming_routes", [])),
            ])
            route_terms = words(route_text)
            visible_terms = words(" ".join(str(value) for value in chunk.metadata.get("ui_text", [])))
            page_score = len(query_terms & route_terms) + min(1.5, 0.5 * len(query_terms & visible_terms))
            route_feature_score = max(route_feature_score, page_score)
        endpoint = chunk.metadata.get("endpoint") or {}
        if endpoint:
            endpoint_terms = words(" ".join([chunk.name, str(endpoint.get("method", "")),
                                             str(endpoint.get("path", ""))]))
            route_feature_score = max(route_feature_score, len(expanded & endpoint_terms))
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
    document_topic_score = 0
    document_file_topic = None
    if chunk.metadata.get("source_type") == "documentation" or chunk.language == "documentation":
        ignored = {"explain", "describe", "tell", "show", "list", "all", "used", "use", "the", "a", "an",
                   "how", "what", "which", "does", "do", "is", "are", "about", "project", "please"}
        ignored.update({"this", "these", "those", "application", "app", "system", "whole", "entire",
                        "change", "update", "manage", "edit", "find", "where", "access", "open"})
        query_topics = set(re.findall(r"[a-z0-9]+", lower)) - ignored
        broad_overview = bool(re.search(
            r"\b(whole|entire|overall|across)\b.{0,50}\b(application|system|project|codebase)\b|\ball components\b",
            lower,
        ))
        if broad_overview:
            query_topics.add("architecture")
        document_topics = set(re.findall(r"[a-z0-9]+", chunk.file.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()))
        document_topics.update(re.findall(r"[a-z0-9]+", str(chunk.metadata.get("document_heading", "")).lower()))
        document_topics.update(re.findall(r"[a-z0-9]+", chunk.name.lower()))
        document_topic_match = bool(query_topics & document_topics)
        file_stem = chunk.file.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        document_file_topic = file_stem if file_stem in query_topics else None
        document_topic_score = (len(query_topics & document_topics) +
                                2 * int(document_file_topic is not None) +
                                int(document_file_topic is not None and " > " not in chunk.name))
    return {"exact_identifier": bool(exact), "relationship_match": bool(matching),
            "relationship_kind": relation_kind, "matching_relationships": matching,
            "api_path_match": url_match, "ui_route_match": ui_route_match,
            "navigation_query": navigation_query, "navigation_match": navigation_score > 0,
            "navigation_match_score": navigation_score, "navigation_matched_terms": navigation_terms,
            "navigation_route_feature_score": route_feature_score,
            "navigation_evidence": bool(navigation_evidence),
            "requested_kind": requested_kind, "entity_kind_match": kind_match,
            "requested_scope": requested_scope, "source_scope_match": scope_match,
            "document_topic_match": document_topic_match,
            "document_topic_score": document_topic_score,
            "document_file_topic": document_file_topic}
