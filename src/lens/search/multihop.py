"""Query intent, source-backed expansion, and deterministic evidence diversity."""

import re
from lens.search.evidence import layer
from lens.search.ranking_signals import technical_names

STOP = {"what", "which", "where", "when", "does", "from", "show", "chain", "through", "the", "a", "an",
        "and", "to", "of", "is", "in", "this", "that", "how", "for", "with", "by", "on", "it", "do",
        "function", "component", "table", "database", "backend", "frontend", "api", "endpoint", "handler",
        "data", "evidence", "flow", "path", "ultimately", "happens", "user", "form", "called", "calls", "click", "clicks"}


def terms(value: str) -> set[str]:
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    result = {word.removesuffix("s") for word in re.findall(r"[a-zA-Z]+", value.lower())
              if word not in STOP and len(word) > 2}
    if "photo" in result:
        result.update({"picture", "camera", "capture"})
    return result


def intent(query: str) -> dict:
    lower = query.lower()
    groups = {"frontend": r"\b(component|frontend|page|homepage|form|displayed|shown)\b",
              "api": r"\b(api|endpoint|request)\b", "backend": r"\b(backend|handler|service)\b",
              "database": r"\b(database|table|sql|stored|procedure|columns?)\b"}
    layers = sorted(name for name, pattern in groups.items() if re.search(pattern, lower))
    flow = bool(re.search(r"\b(chain|flow|ultimately|hops?|originates?|supplies)\b|what happens|how (?:does|do).+(?:submit|send|sav|process|travel|work)|where does.+come from|where.+fetched.+stored", lower))
    api_flow = "api" in layers and bool(re.search(r"\b(calls?|sends?|receives?|uses?|supplies)\b", lower))
    return {"expand": flow or len(layers) >= 2 or api_flow, "flow_language": flow,
            "requested_layers": layers, "query_terms": sorted(terms(query)),
            "reason": "flow_intent" if flow else "api_relationship" if api_flow else "multiple_evidence_layers" if len(layers) >= 2 else "ordinary_retrieval"}


def expand(query, ranked, graph, get_chunks, top_k, max_depth=8):
    requested = intent(query)
    identifiers = technical_names(query)
    words = terms(query)
    urls = [value.rstrip(".,;?!") for value in re.findall(r"/(?:[\w:.*{}-]+/?)+", query)]
    methods = set(re.findall(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", query))
    backend_start = bool(urls) and not re.search(r"\b(frontend|component|page|form)\b", query.lower())
    eligible = [hit for hit in ranked if hit.chunk.entity_id in graph.nodes and
                hit.chunk.kind in {"component", "component_class", "function", "method", "endpoint", "procedure"}]
    if requested["requested_layers"] == ["frontend"]:
        frontend = [hit for hit in eligible if graph.nodes[hit.chunk.entity_id].layer == "frontend"]
        if frontend:
            eligible = frontend
    if "component" in query.lower() and not requested["flow_language"]:
        components = [hit for hit in eligible if hit.chunk.kind in {"component", "component_class"}]
        if components:
            eligible = components

    def names(hit):
        return {hit.chunk.metadata.get("entity_name", "").lower(),
                hit.chunk.name.rsplit("::", 1)[-1].lower(), hit.chunk.name.lower()}

    exact = [hit for hit in eligible if names(hit) & identifiers]
    if exact:
        eligible = exact
    elif backend_start:
        matching = [hit for hit in eligible if hit.chunk.kind == "endpoint" and
                    hit.chunk.metadata.get("endpoint", {}).get("path") in urls and
                    (not methods or hit.chunk.metadata.get("endpoint", {}).get("method") in methods)]
        if matching:
            eligible = matching

    # Do not let a well-resolved but unrelated application path replace the
    # requested subject. Natural language may use "photo" for takePicture.
    focused = []
    if not exact and words:
        focused = [hit for hit in eligible if words & terms(hit.chunk.metadata.get("entity_name", hit.chunk.name.rsplit("::", 1)[-1]))]
        if focused:
            eligible = focused

    def anchor_key(hit):
        name = hit.chunk.metadata.get("entity_name", hit.chunk.name.rsplit("::", 1)[-1])
        return (-int(bool(names(hit) & identifiers)),
                -int(hit.ranking_signals.get("api_path_match", False)),
                -int("api" in requested["requested_layers"] and bool(hit.chunk.metadata.get("requests"))),
                -len(words & terms(name)),
                -int(requested["flow_language"] and hit.chunk.kind in {"component", "component_class"}),
                -hit.hybrid_score, hit.chunk.id)

    anchors = sorted(eligible, key=anchor_key)[:5]

    def needs_upstream(hit):
        if backend_start:
            return False
        # A workflow starting at a page follows its operations. Parent renderers
        # provide placement context, not preceding execution steps.
        if requested["flow_language"] and hit.chunk.kind in {"component", "component_class"}:
            return False
        outgoing = graph.outgoing.get(hit.chunk.entity_id, [])
        if any(edge.kind == "REQUESTS" for edge in outgoing):
            return False
        handlers = [edge.target_id for edge in outgoing if edge.target_id and edge.evidence.startswith("JSX event")]
        return not any(edge.kind == "REQUESTS" for handler in handlers for edge in graph.outgoing.get(handler, []))

    reports = [graph.reconstruct(hit.chunk.entity_id, max_depth=max_depth, upstream=needs_upstream(hit),
                                preferred_terms=frozenset(words)) for hit in anchors]
    paths = [path for report in reports for path in report["paths"]]

    def path_key(path):
        nodes = [node for node in path["nodes"] if node["id"]]
        matched = words & terms(" ".join(node["name"].rsplit("::", 1)[-1] for node in nodes))
        technical = any(node["name"].rsplit("::", 1)[-1].lower() in identifiers or
                        node["name"].rsplit("::", 1)[-1].split(".")[-1].lower() in identifiers for node in nodes)
        layers = {node["layer"] for node in nodes}
        endpoint = any(node["layer"] == "api" for node in nodes)
        column_query = bool(re.search(r"\bcolumns?\b", query.lower()))
        terminal_kind = nodes[-1]["kind"] if nodes else ""
        api_edges = [edge for edge in path["relationships"] if edge["kind"] in {"REQUESTS", "HANDLES"}]
        exact_api = any(edge["metadata"].get("path") in urls and
                        (not methods or edge["metadata"].get("method") in methods) for edge in api_edges)
        operation = "WRITES" if re.search(r"\b(write|writes|save|store|stored|storage|submit|update|delete)\b", query.lower()) else "READS" if re.search(r"\b(read|reads|supplies|supply|origin|come from|load|loads)\b", query.lower()) else None
        operation_match = operation is not None and any(edge["kind"] == operation for edge in path["relationships"])
        public_match = "public" in query.lower() and any(edge["metadata"].get("path", "").startswith("/api/") and
                           not edge["metadata"].get("path", "").startswith(("/api/admin/", "/api/auth/")) for edge in api_edges)
        literal_request = any(edge["kind"] == "REQUESTS" and edge["target_id"] and
                              not edge["metadata"].get("dynamic") for edge in api_edges)
        # Require a useful boundary before preferring depth; unresolved paths remain inspectable.
        return (-int(technical), -int(exact_api),
                -sum(edge["kind"] == "REQUESTS" and edge.get("target_id") is not None for edge in path["relationships"]),
                -int(public_match), -int(operation_match),
                -int(literal_request and public_match),
                -len(matched), -int(endpoint and "database" in layers),
                -int(terminal_kind == ("column" if column_query else "table")),
                -len(layers - {"code", "unknown"}), -int(endpoint),
                -int(path["termination"] != "unresolved"),
                -int(path["nodes"][0]["kind"] in {"component", "component_class"} and requested["flow_language"]),
                sum(edge["direction"] == "reverse" for edge in path["relationships"]), len(path["relationships"]),
                tuple(node["id"] or "" for node in path["nodes"]))

    paths.sort(key=path_key)
    selected_paths = []
    seen = set()
    for path in paths:
        signature = tuple((edge["edge_id"], edge["direction"]) for edge in path["relationships"])
        if signature not in seen:
            selected_paths.append(path)
            seen.add(signature)
        if len(selected_paths) == 8:
            break
    wrapper_ids = list(dict.fromkeys(edge["metadata"]["wrapper_id"] for path in selected_paths
                                    for edge in path["relationships"] if edge["metadata"].get("wrapper_id")))
    ids = list(dict.fromkeys([*(node["id"] for path in selected_paths for node in path["nodes"] if node["id"]), *wrapper_ids]))
    hits = {hit.chunk.entity_id: hit for hit in ranked}
    for hit in get_chunks(ids):
        hits.setdefault(hit.chunk.entity_id, hit)
    selected, selected_ids, slots = [], set(), set()

    def add(node, path_index, position, diverse):
        entity_id = node["id"]
        hit = hits.get(entity_id)
        if not hit or entity_id in selected_ids or len(selected) >= top_k:
            return
        category = "event_or_data_function" if hit.chunk.kind in {"function", "method"} else hit.chunk.kind
        slot = (node["layer"], category)
        if diverse and slot in slots:
            return
        hit.ranking_signals = {**hit.ranking_signals, "chain_evidence": True, "chain_index": path_index,
                               "chain_position": position, "source_layer": node["layer"],
                               "selection_rule": "distinct_layer_and_role" if diverse else "chain_order"}
        selected.append(hit)
        selected_ids.add(entity_id)
        slots.add(slot)

    # First reserve one representative per layer on the best path, then distinct roles.
    # Preserve the strongest subject-matching entry point even when a neighboring
    # read/lookup path spans more layers than the requested click/submit handler.
    if focused and anchors:
        from dataclasses import asdict
        add(asdict(graph.nodes[anchors[0].chunk.entity_id]), -1, 0, False)
    if selected_paths:
        best = selected_paths[0]
        represented = set()
        for position, node in enumerate(best["nodes"]):
            if node["id"] and node["layer"] not in represented:
                add(node, 0, position, False)
                represented.add(node["layer"])
        for position, node in enumerate(best["nodes"]):
            add(node, 0, position, True)
        # Include complementary branches before spending the remaining budget on
        # repeated roles along one path (e.g. analysis service beside persistence).
        for path_index, path in enumerate(selected_paths[1:], 1):
            for position, node in enumerate(path["nodes"]):
                add(node, path_index, position, True)
        for position, node in enumerate(best["nodes"]):
            add(node, 0, position, False)
        for entity_id in wrapper_ids:
            if entity_id in graph.nodes:
                from dataclasses import asdict
                add(asdict(graph.nodes[entity_id]), 0, -1, False)
        for path_index, path in enumerate(selected_paths[1:], 1):
            for position, node in enumerate(path["nodes"]):
                add(node, path_index, position, True)
    fallback = sorted(ranked, key=lambda hit: -len(words & terms(hit.chunk.metadata.get("entity_name", hit.chunk.name.rsplit("::", 1)[-1])))) if focused else ranked
    for hit in fallback:
        if len(selected) == top_k:
            break
        if hit.chunk.entity_id not in selected_ids:
            selected.append(hit)
            selected_ids.add(hit.chunk.entity_id)
    for position, hit in enumerate(selected, 1):
        hit.rank = position
    return selected, {"intent": requested, "anchors": [hit.chunk.name for hit in anchors],
        "reconstruction": reports, "selected_paths": selected_paths,
        "candidate_paths": sum(report["candidate_paths"] for report in reports),
        "reconstruction_seconds": sum(report["reconstruction_seconds"] for report in reports),
        "selected_layers": sorted({layer(hit.chunk.kind, hit.chunk.file, hit.chunk.language) for hit in selected}),
        "wrapper_provenance_entities": [graph.nodes[entity_id].name for entity_id in wrapper_ids if entity_id in graph.nodes],
        "selection": "distinct layers, then entity roles, then original ranking", "static_only": True}
