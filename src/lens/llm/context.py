"""Bounded deterministic evidence selection, never raw candidate concatenation."""

from dataclasses import dataclass
import json
import re
from lens.core.models import stable_id


@dataclass(frozen=True)
class EvidenceContext:
    project: str
    items: tuple[dict, ...]
    paths: tuple[dict, ...]
    truncated: bool
    retrieved_count: int = 0

    @property
    def evidence(self):
        return {item["id"]: item for item in self.items}

    def render(self):
        # Canonical IDs remain in debug/source mappings; compact citation IDs are sufficient
        # in the prompt and avoid asking the model to repeatedly generate full SHA-256 strings.
        items = [{key: value for key, value in item.items() if key not in {"entity_id", "source_uid"}}
                 for item in self.items]
        return json.dumps({"project": self.project, "static_only": all(item.get("static_only", True) for item in items), "items": items,
                           "paths": self.paths, "truncated": self.truncated,
                           "retrieved_count": self.retrieved_count}, ensure_ascii=False)


def excerpt(text, budget=1800):
    """Retain complete leading and trailing lines, visibly mark omitted material."""
    if len(text) <= budget:
        return text
    lines = text.splitlines()
    head, tail = [], []
    for line in lines:
        if len("\n".join([*head, line])) > budget // 2 - 30:
            break
        head.append(line)
    for line in reversed(lines[len(head):]):
        if len("\n".join([line, *tail])) > budget // 2 - 30:
            break
        tail.insert(0, line)
    return "\n".join([*head, "[excerpt omitted; not complete source]", *tail])


def source_excerpt(text, budget, query):
    """Keep question-bearing operations when a function's middle is decisive."""
    if len(text) <= budget or not query:
        return excerpt(text, budget)
    from lens.search.semantics import words
    requested = words(query) - {"what", "when", "does", "through", "frontend", "backend", "function", "user", "response"}
    lines = text.splitlines()
    candidates = []
    for index, line in enumerate(lines):
        # Generated summaries are already represented by explicit relationship items.
        if re.search(r"Application data flow:|(?:^| )(?:(?:CONTAINS|CALLS|REFERENCES|RENDERS|HANDLES) |Calls:|Source terms:|Literal evidence:|UI text:|Parameters/props:|React hooks:)", line):
            continue
        operation = bool(re.search(r"\b(?:return|await|model|pipeline|INSERT|SELECT|score|analyze|json|addEntry)\b|\.post\(", line))
        overlap = len(requested & words(line))
        if operation or overlap:
            decisive = bool(re.search(r"\.(?:post|query|json)\(|\b(?:INSERT|UPDATE|DELETE)\b|return\s+jsonify", line))
            candidates.append((-(overlap + int(operation) + 5 * int(decisive)), index))
    chosen, used = set(), 45
    for _, index in sorted(candidates):
        # Adjacent argument/return lines preserve the meaning of multiline calls.
        window = range(max(0, index - 1), min(len(lines), index + 3))
        extra = sum(len(lines[i]) + 1 for i in window if i not in chosen)
        if used + extra <= budget:
            chosen.update(window)
            used += extra
    if not chosen:
        return excerpt(text, budget)
    result, previous = [], -2
    for index in sorted(chosen):
        if index != previous + 1:
            result.append("[source lines omitted]")
        result.append(lines[index])
        previous = index
    if previous != len(lines) - 1:
        result.append("[source lines omitted]")
    return "\n".join(result)


def _relevant_navigation_links(links, query, limit=12):
    if not links:
        return []
    from lens.search.semantics import words
    requested = words(query) - {"where", "how", "can", "i", "do", "the", "a", "an", "of", "to", "for", "all", "me", "find", "see", "view", "page"}
    ranked = sorted(enumerate(links), key=lambda pair: (
        -len(requested & words(" ".join(str(pair[1].get(key, "")) for key in
            ("label", "controller", "action", "href", "route_template", "condition")))), pair[0]))
    positive = [link for score, link in ranked if requested & words(" ".join(str(link.get(key, "")) for key in
        ("label", "controller", "action", "href", "route_template", "condition")))]
    return (positive or [link for _, link in ranked])[:limit]


def build_context(response, project: str, max_chars=12000, database_schema=None, query=""):
    if not project or not 2000 <= max_chars <= 24000:
        raise ValueError("Context requires a selected project and a bounded character budget")
    items, paths, seen = [], [], set()
    truncated = False

    def add(item):
        nonlocal truncated
        if item["id"] in seen:
            original = next(value for value in items if value["id"] == item["id"])
            if original["source_uid"] != item["source_uid"]:
                raise ValueError("Compact evidence identifier collision")
            return True
        proposal = EvidenceContext(project, tuple([*items, item]), tuple(paths), True, len(response.results))
        if len(proposal.render().encode("utf-8")) > max_chars:
            truncated = True
            return False
        items.append(item)
        seen.add(item["id"])
        return True

    if database_schema is not None:
        # Accept only introspection fields. Connection strings, defaults, records,
        # comments and arbitrary caller fields are never sent to the provider.
        def fields(value, allowed):
            return {key: value[key] for key in allowed if key in value}
        tables = []
        from lens.search.semantics import words
        requested = words(query)
        available = database_schema.get("tables", [])
        available = sorted(available, key=lambda table: -len(requested & words(
            str(table.get("name", "")) + " " + " ".join(str(c.get("name", "")) for c in table.get("columns", [])))))
        for table in available[:40]:
            safe = fields(table, ("schema", "name"))
            safe["columns"] = [fields(column, ("name", "type", "nullable", "primary_key"))
                               for column in table.get("columns", [])[:200]]
            safe["foreign_keys"] = [fields(edge, ("column", "target_schema", "target_table", "target_column", "confidence"))
                                    for edge in table.get("foreign_keys", [])]
            safe["indexes"] = [fields(index, ("name", "unique")) for index in table.get("indexes", [])]
            tables.append(safe)
        from lens.llm.diagnostics import redact
        from lens.database.application import _relevant_routines
        routines = []
        ranked_routines = _relevant_routines(database_schema.get("routines", []), query,
                                             limit=8, definition_budget=6000)
        for routine in ranked_routines:
            safe = fields(routine, ("name", "type", "returns", "data_access", "parameters"))
            safe["parameters"] = [fields(parameter, ("name", "mode", "type"))
                                  for parameter in routine.get("parameters", [])[:30]]
            if routine.get("definition"):
                safe["definition"] = routine["definition"][:2500]
            routines.append(safe)
        text = json.dumps(redact({"dialect": database_schema.get("dialect"), "tables": tables,
                                  "routines": routines,
                                  "routine_metadata": database_schema.get("routine_metadata", {}),
                                  "metadata_only": True}), ensure_ascii=False, indent=1)
        budget = min(4000, max_chars // 3)
        add({"id": "D-schema", "source_uid": stable_id(project, "live_schema", text),
             "type": "database_metadata", "entity_names": [t.get("name", "") for t in tables[:20]],
             "file": "live database schema metadata", "line_start": None, "line_end": None,
             "kind": "database_schema", "excerpt": excerpt(text, budget),
             "excerpt_truncated": len(text) > budget or len(available) > 40, "static_only": False})
    # Only Phase 4's selected, source-backed paths; no unused candidate paths.
    selected = [*response.debug.get("cross_layer", {}).get("selected_paths", []),
                *response.debug.get("semantic", {}).get("selected_paths", [])]
    path_limit = 2 if response.debug.get("cross_layer", {}).get("enabled") else 4
    for path in selected[:path_limit]:
        ids, directions = [], []
        for edge in path["relationships"][:8]:
            details = edge.get("metadata", {})
            if not details.get("source_file"):
                continue
            uid = stable_id(project, str(edge["source_id"]), edge["kind"], str(edge.get("target_id")),
                                  edge["target_name"], edge.get("evidence", ""), str(details.get("source_line_start")))
            eid = "R-" + uid[:16]
            item = {"id": eid, "source_uid": uid, "type": "relationship", "entity_names": [], "file": details["source_file"],
                    "line_start": details.get("source_line_start"), "line_end": details.get("source_line_end"),
                    "kind": edge["kind"], "source_name": details.get("source_name"),
                    "target_name": details.get("target_name", edge["target_name"]),
                    "resolved": edge.get("target_id") is not None, "target_id": edge.get("target_id"),
                    "confidence": edge.get("confidence"), "rule": details.get("rule"),
                    "conditional": details.get("conditional", bool(details.get("condition"))),
                    "condition": details.get("condition"),
                    "static_only": True,
                    "excerpt": excerpt(edge.get("evidence", ""), 600)}
            if details.get("sql"):
                item["literal_sql"] = excerpt(details["sql"], 1000)
            if add(item):
                ids.append(eid)
                directions.append(edge.get("direction", "forward"))
        record = {"evidence_ids": ids, "termination": path["termination"],
                  "directions": directions,
                  "partial": len(ids) != len(path["relationships"]), "static_only": True}
        if ids and len(EvidenceContext(project, tuple(items), tuple([*paths, record]), True, len(response.results)).render().encode("utf-8")) <= max_chars:
            paths.append(record)
        else:
            truncated = True
    unique_hits, locations, compact_ids = [], set(), {}
    for hit in response.results:
        chunk = hit.chunk
        if chunk.metadata.get("project_id") not in {None, project}:
            raise ValueError("Retrieved evidence belongs to another project")
        compact = chunk.id[:16]
        if compact in compact_ids and compact_ids[compact] != chunk.id:
            raise ValueError("Compact evidence identifier collision")
        compact_ids[compact] = chunk.id
        location = (chunk.file, chunk.metadata.get("line_start"), chunk.metadata.get("line_end"))
        if location[1] is not None and location in locations:
            continue
        unique_hits.append(hit)
        locations.add(location)
    entity_count = len(unique_hits)
    # Share the budget across selected evidence so an early large function cannot
    # crowd out connected downstream implementations. Leave room for edge provenance.
    excerpt_budget = max(240, min(2400, (max_chars - len(EvidenceContext(project, tuple(items), tuple(paths), True, len(response.results)).render().encode("utf-8"))) // max(1, entity_count) - 350))
    for hit in unique_hits:
        chunk = hit.chunk
        application = chunk.metadata.get("project_id")
        if application is not None and application != project:
            raise ValueError("Retrieved evidence belongs to another project")
        add({"id": "E-" + chunk.id[:16], "source_uid": chunk.id,
             "type": "entity", "entity_id": chunk.entity_id,
             "entity_names": list(dict.fromkeys([chunk.name, chunk.metadata.get("entity_name", chunk.name)])),
             "file": chunk.file, "line_start": chunk.metadata.get("line_start"),
             "line_end": chunk.metadata.get("line_end"), "kind": chunk.kind,
             "excerpt": source_excerpt(chunk.text, max(excerpt_budget, 800) if
                 re.search(r"\.post\(", chunk.text) and re.search(r"\bINSERT\b", chunk.text) else excerpt_budget, query),
             "excerpt_truncated": len(chunk.text) > excerpt_budget,
             "source_type": chunk.metadata.get("source_type", "code"),
             "navigation_candidate": chunk.metadata.get("navigation_candidate", False),
             "navigation_links": _relevant_navigation_links(chunk.metadata.get("navigation_links", []), query),
             "page_title": chunk.metadata.get("page_title", ""),
             "source_page_title": chunk.metadata.get("page_title", ""),
             "routes": chunk.metadata.get("routes", []),
             "incoming_routes": chunk.metadata.get("incoming_routes", []),
             "endpoint": chunk.metadata.get("endpoint"),
             "navigation_relationships": [{
                 "kind": row.get("kind"), "target_name": row.get("target_name"),
                 "target_id": row.get("target_id"), "confidence": row.get("confidence"),
                 "rule": (row.get("metadata") or {}).get("rule"),
                 "route_template": (row.get("metadata") or {}).get("route_template"),
                 "href": (row.get("metadata") or {}).get("href"),
                 "label": (row.get("metadata") or {}).get("label"),
                 "source_line_start": (row.get("metadata") or {}).get("source_line_start"),
             } for row in chunk.metadata.get("relation_evidence", [])
                 if row.get("kind") in {"NAVIGATES_TO", "RENDERS", "REQUESTS", "SUBMITS_TO", "LINKS_TO", "HANDLES"}],
             "document_heading": chunk.metadata.get("document_heading"),
             "document_version": chunk.metadata.get("document_version"),
             "document_date": chunk.metadata.get("document_date"),
             "document_status": chunk.metadata.get("document_status"),
             "freshness": chunk.metadata.get("freshness"),
             "static_only": True, "rank": hit.rank})
    # Keep the public entity-first presentation while reserving relationship
    # provenance before excerpt allocation internally.
    items.sort(key=lambda item: item["type"] == "relationship")
    return EvidenceContext(project, tuple(items), tuple(paths),
                           truncated or any(item.get("excerpt_truncated", False) for item in items),
                           len(response.results))
