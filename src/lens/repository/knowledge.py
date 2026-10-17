"""Resolve unique targets and construct meaningful retrieval documents."""

import re
from collections import defaultdict
import sqlglot
from sqlglot.errors import SqlglotError
from lens.core.models import CodeSymbol, DocumentChunk, Knowledge, Relationship, stable_id
from lens.parsing.sql import statement_relationships


def lexical_text(text: str) -> str:
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    split = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", split)
    return text + "\n" + re.sub(r"[^\w\s]|_", " ", split)


def resolve(knowledge: Knowledge) -> None:
    from lens.search.frontend_resolution import resolve as resolve_frontend

    files = {file.id: file for file in knowledge.files}
    _application_links(knowledge)
    resolve_frontend(knowledge)
    entities = knowledge.entities
    by_id = {e.id: e for e in entities}
    languages = {f.id: f.language for f in knowledge.files}
    types_by_name, methods_by_name, bases_by_name = defaultdict(list), defaultdict(list), defaultdict(list)
    db_by_name, db_by_qualified, procedures_by_name = defaultdict(list), defaultdict(list), defaultdict(list)
    for entity in entities:
        if entity.kind in {"class", "interface", "struct", "record"}:
            for name in {entity.name, entity.qualified_name}:
                types_by_name[name].append(entity)
                if entity.kind in {"class", "interface"}:
                    bases_by_name[name].append(entity)
        if entity.kind == "method":
            methods_by_name[entity.qualified_name].append(entity)
    for entity in knowledge.database_objects:
        db_by_name[entity.name.lower()].append(entity)
        db_by_qualified[entity.qualified_name.lower()].append(entity)
        if entity.kind == "procedure":
            for name in {entity.name.lower(), entity.qualified_name.lower()}:
                procedures_by_name[name].append(entity)
    for symbol in knowledge.symbols:
        sites = list(symbol.metadata.get("sql_sites", []))
        sites += [{"sql": literal} for literal in symbol.metadata.get("sql_literals", [])]
        for site in sites:
            literal = site["sql"]
            try:
                call = re.fullmatch(r"\s*CALL\s+([\w`.]+)\s*\([^;]*\)\s*;?\s*", literal, re.I)
                if call:
                    from lens.core.models import Relationship
                    edges = [Relationship(symbol.id, "CALLS_PROCEDURE", call[1].replace("`", ""), evidence="SQL CALL routine")]
                else:
                    statement = sqlglot.parse_one(literal, read="mysql")
                    edges = statement_relationships(symbol.id, statement)
                for edge in edges:
                    edge.metadata.update({"rule": "finite_sql_alternatives" if site.get("conditional") else "literal_sql_ast",
                        "sql": literal, "expression": site.get("expression", literal),
                        "source_line_start": site.get("line_start", symbol.line_start),
                        "source_line_end": site.get("line_end", symbol.line_end),
                        "conditional": site.get("conditional", False)})
                knowledge.relationships.extend(edges)
            except SqlglotError:
                knowledge.warnings.append(f"{symbol.qualified_name}: malformed inline SQL")
        for site in symbol.metadata.get("unresolved_sql", []):
            from lens.core.models import Relationship
            knowledge.relationships.append(Relationship(symbol.id, "QUERIES", "[dynamic SQL]",
                evidence="SQL expression cannot be statically enumerated", metadata={
                    "rule": "unresolved_sql_expression", "expression": site["expression"],
                    "source_line_start": site["line_start"], "source_line_end": site["line_start"]}))
    render_edges = defaultdict(list)
    for relation in knowledge.relationships:
        if relation.kind == "RENDERS" and relation.target_id:
            render_edges[relation.source_id].append(relation)
    for source in knowledge.symbols:
        for route in source.metadata.get("routes", []):
            for relation in render_edges.get(source.id, []):
                if relation.source_id == source.id and relation.kind == "RENDERS" and relation.target_id and relation.target_name in route.get("renders", []):
                    target = by_id[relation.target_id]
                    target.metadata.setdefault("incoming_routes", []).append({
                        "path": route["path"], "owner": source.qualified_name,
                        "source_file": files[source.file_id].path, "line": route.get("line"),
                    })
    for relationship in knowledge.relationships:
        if relationship.target_id:
            continue
        source = by_id[relationship.source_id]
        if relationship.kind == "CALLS" and languages.get(source.file_id) == "python":
            continue  # Python scope/import resolver owns these; C# receiver rules do not apply.
        if source.metadata.get("start_byte") is not None and relationship.kind in {"CALLS", "HANDLES", "EXTENDS", "IMPLEMENTS", "IMPORTS", "EXPORTS", "RENDERS", "REQUESTS", "QUERIES"}:
            continue  # handled by lexical/module resolution, not the C# resolver
        if source.metadata.get("start_byte") is not None and relationship.kind == "REFERENCES" and not relationship.evidence.startswith("SQL"):
            continue
        name = relationship.target_name
        candidates = []
        if relationship.kind == "CALLS":
            calls = source.metadata.get("calls", [])
            evidence = next(
                (
                    call
                    for call in calls
                    if call["name"] == name
                    and relationship.evidence
                    == ((call["receiver"] + ".") if call["receiver"] else "") + name
                ),
                {},
            )
            receiver = evidence.get("receiver", "")
            receiver_type = evidence.get("receiver_type", "")
            owner = evidence.get("owner", "")
            if receiver in {"", "this"}:
                target_owner = owner
            else:
                type_name = receiver_type or receiver
                types = types_by_name.get(type_name, [])
                target_owner = types[0].qualified_name if len(types) == 1 else ""
            if target_owner:
                candidates = methods_by_name.get(f"{target_owner}.{name}", [])
        elif relationship.kind == "CALLS_PROCEDURE":
            candidates = procedures_by_name.get(name.lower(), [])
        elif relationship.kind in {"READS", "WRITES", "REFERENCES"}:
            candidates = db_by_qualified.get(name.lower()) or db_by_name.get(name.lower(), [])
        elif relationship.kind == "INHERITS":
            candidates = bases_by_name.get(name, [])
        if len(candidates) == 1:
            relationship.target_id = candidates[0].id
            relationship.confidence = "high"
    from lens.search.evidence import annotate
    annotate(knowledge)


def _application_links(knowledge: Knowledge) -> None:
    """Join parser facts by explicit names and routes; ambiguous links stay unresolved.

    A matched path proves a static candidate only: deployments, reverse proxies and
    runtime configuration can still send a request elsewhere.
    """
    import posixpath
    from lens.search.frontend_resolution import path_matches

    files = {f.id: f for f in knowledge.files}
    symbols = [s for s in knowledge.symbols if s.file_id in files]
    by_id = {s.id: s for s in symbols}
    by_file_name, by_kind, by_kind_name = defaultdict(list), defaultdict(list), defaultdict(list)
    python_by_file = {}
    for symbol in symbols:
        by_file_name[symbol.file_id, symbol.name].append(symbol)
        by_kind[symbol.kind].append(symbol)
        for name in {symbol.name, symbol.qualified_name}:
            by_kind_name[symbol.kind, name].append(symbol)
        if symbol.kind == "python_module":
            python_by_file[symbol.file_id] = symbol
    edge_index, source_calls = {}, defaultdict(list)
    for edge in knowledge.relationships:
        edge_index.setdefault((edge.source_id, edge.kind, edge.target_name), edge)
        if edge.kind == "CALLS":
            source_calls[edge.source_id].append(edge)

    def link(source, kind, name, candidates, rule, evidence, line=None, confidence="high"):
        unique = {s.id: s for s in candidates}
        target = next(iter(unique.values())) if len(unique) == 1 else None
        key = (source.id, kind, name)
        existing = edge_index.get(key)
        edge = existing or Relationship(source.id, kind, name)
        edge.target_id = target.id if target else None
        edge.confidence = confidence if target else "unresolved"
        edge.evidence = evidence
        edge.metadata.update(rule=rule, source_line_start=line or source.line_start,
                             candidate_count=len(unique), static_only=True)
        if not existing:
            knowledge.relationships.append(edge)
            edge_index[key] = edge
        return target

    # Python imports resolve by module paths, never by a repository-wide name match.
    python_modules = {posixpath.splitext(files[s.file_id].path)[0].replace("/", "."): s
                      for s in symbols if s.kind == "python_module"}
    python_modules.update({k[:-9]: v for k, v in list(python_modules.items()) if k.endswith(".__init__")})
    default_aliases = {}

    def aliases(owner):
        """Only explicit parameter-or-function defaults; arbitrary assignments stay unknown."""
        if owner.id in default_aliases:
            return default_aliases[owner.id]
        import ast
        import textwrap
        found = {}
        try:
            parsed = ast.parse(textwrap.dedent(owner.source))
            declaration = next((n for n in parsed.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
            params = {p["name"] for p in owner.metadata.get("parameters", [])}
            # Top-level assignments in this lexical function only, excluding nested bodies.
            for statement in declaration.body if declaration else []:
                if not isinstance(statement, ast.Assign) or len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    continue
                value = statement.value
                if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) and len(value.values) == 2 and all(isinstance(n, ast.Name) for n in value.values) and value.values[0].id in params:
                    found[statement.targets[0].id] = {"parameter": value.values[0].id, "default": value.values[1].id,
                        "expression": ast.unparse(statement), "line": owner.line_start + statement.lineno - 1}
        except SyntaxError:
            pass
        default_aliases[owner.id] = found
        return found
    for source in symbols:
        if files[source.file_id].language != "python":
            continue
        module = python_by_file.get(source.file_id)
        imports = list(module.metadata.get("imports", [])) if module else []
        if module and module.id != source.id:
            imports += source.metadata.get("imports", [])
        for edge in source_calls.get(source.id, []):
            if edge.target_id:
                continue
            name = edge.evidence or edge.target_name
            root, _, member = name.partition(".")
            if root in {p["name"] for p in source.metadata.get("parameters", [])}:
                continue
            if re.search(r'^\s*' + re.escape(root) + r'\s*(?::[^=\n]+)?=(?!=)', source.source, re.M):
                continue  # Local bindings shadow imports; no speculative alias propagation.
            candidates = []
            if not member:
                candidates = [s for s in by_file_name.get((source.file_id, root), []) if
                              s.kind in {"function", "class"} and s.metadata.get("parent_id") == (module.id if module else None)]
            for entry in imports:
                module_name = entry.get("module", "")
                if module_name.startswith(".") and module:
                    level = len(module_name) - len(module_name.lstrip("."))
                    package = module.name.split(".")[:-level]
                    module_name = ".".join([*package, module_name.lstrip(".")]).rstrip(".")
                # Bare Python imports are looked up only at an explicit repository
                # module path or beside the calling module, never by basename.
                bases = [module_name]
                if module and not entry.get("module", "").startswith("."):
                    caller_directory = module.name.rpartition(".")[0]
                    if caller_directory:
                        bases.append(caller_directory + "." + module_name)
                for item in entry.get("names", [{"name": entry.get("imported", ""), "alias": entry.get("local")} ]):
                    if isinstance(item, str):
                        imported, alias = item, item
                    else:
                        imported = item.get("name", "")
                        alias = item.get("alias") or imported
                    if root == alias:
                        target_name = member or imported
                        for base in bases:
                            imported_module = python_modules.get(base)
                            if imported_module:
                                candidates += [s for s in by_file_name.get((imported_module.file_id, target_name), []) if
                                               s.metadata.get("parent_id") == imported_module.id]
                            # `from helpers import sentiment_analysis as s` can
                            # import a namespace-package submodule without __init__.
                            child_module = python_modules.get(base + "." + imported)
                            if child_module and member:
                                candidates += [s for s in by_file_name.get((child_module.file_id, member), []) if
                                               s.metadata.get("parent_id") == child_module.id]
            if candidates:
                link(source, "CALLS", edge.target_name, candidates, "python_lexical_import", edge.evidence)
            elif not member:
                owner = source
                while owner and owner.kind != "python_module":
                    default = aliases(owner).get(root)
                    if default:
                        targets = [s for s in by_file_name.get((source.file_id, default["default"]), []) if
                                   s.kind == "function" and s.metadata.get("parent_id") == (module.id if module else None)]
                        target = link(source, "CALLS", edge.target_name, targets, "python_parameter_default_callable",
                            default["expression"] + "; conditional default callable", confidence="static_pattern")
                        if target:
                            edge.metadata.update(condition="when " + default["parameter"] + " is not supplied or is falsey",
                                assignment_line=default["line"], assignment_expression=default["expression"],
                                call_expression=name, default_target=target.qualified_name)
                        break
                    owner = by_id.get(owner.metadata.get("parent_id"))

    # Publish handler-owned routes using the existing endpoint/HANDLES representation.
    symbol_ids = set(by_id)
    for source in symbols:
        routes = list(source.metadata.get("routes", [])) if files[source.file_id].language == "python" else []
        if files[source.file_id].language == "csharp" and source.kind == "method":
            owner = by_id.get(source.metadata.get("parent_id"))
            prefix_match = re.search(r'\[Route\(\s*"([^"]+)"', owner.metadata.get("signature", "")) if owner else None
            prefix = prefix_match[1] if prefix_match else ""
            if owner:
                prefix = prefix.replace("[controller]", re.sub("Controller$", "", owner.name))
            for match in re.finditer(r'\[Http(Get|Post|Put|Patch|Delete|Head|Options)(?:\(\s*"([^"]*)"[^\]]*)?\]', source.metadata.get("signature", "")):
                suffix = match[2] or ""
                if prefix or suffix:
                    path = suffix[1:] if suffix.startswith("~/") else "/".join(filter(None, [prefix.strip("/"), suffix.strip("/")]))
                    routes.append({"path": "/" + path.lstrip("/"), "method": match[1].upper()})
        for route in routes:
            if not route.get("path") or not route.get("method"):
                continue
            method, path = route["method"].upper(), route["path"]
            endpoint = CodeSymbol(stable_id(source.id, "route", method, path), f"{method} {path}", "endpoint",
                                  source.qualified_name + "::" + method + " " + path, source.file_id,
                                  route.get("line", source.line_start), source.line_end, source.source,
                                  {"method": method, "path": re.sub(r"\{([^}:]+)(?::[^}]+)?\}", r":\1", path),
                                   "declared_path": path, "static_only": True})
            if endpoint.id not in symbol_ids:
                knowledge.symbols.append(endpoint)
                symbol_ids.add(endpoint.id)
                link(endpoint, "HANDLES", source.qualified_name, [source], "declared_handler_route", "handler route declaration")

    endpoints = [s for s in knowledge.symbols if s.kind == "endpoint"]
    for source in symbols:
        if files[source.file_id].language == "csharp" and source.kind == "method":
            owner = by_id.get(source.metadata.get("parent_id"))
            # Literal View("Name") and View() use the documented MVC convention.
            view_call = re.search(r'\breturn\s+View\s*\(\s*(?:"([^"\\]+)"|(?=[\w)]))', source.source)
            if owner and owner.name.endswith("Controller") and view_call:
                view_name = view_call[1] or source.name
                expected = "Views/" + owner.name[:-10] + "/" + view_name + ".cshtml"
                link(source, "RENDERS", expected, [s for s in by_kind.get("razor_view", []) if
                     files[s.file_id].path.endswith(expected)], "mvc_view_convention", "MVC View return and view path convention", confidence="static_pattern")
        for form in source.metadata.get("forms", []):
            controller, action = form.get("controller"), form.get("action")
            candidates = [s for s in by_kind_name.get(("method", action), []) if
                          (owner := by_id.get(s.metadata.get("parent_id"))) and owner.name == str(controller) + "Controller"] if controller and action else []
            candidates = [s for s in candidates if not re.search(r'\[Http(?:Get|Post|Put|Patch|Delete)', s.metadata.get("signature", "")) or
                          re.search(r'\[Http' + form.get("method", "GET").title() + r'(?:\]|\()', s.metadata.get("signature", ""))]
            link(source, "REQUESTS", str(controller or "?") + "/" + str(action or "?"), candidates,
                 "razor_form_controller_action", "Razor form tag helpers (routing convention)", form.get("line"), "static_pattern")
        # A Razor anchor is direct UI evidence. Resolve asp-controller/action only
        # when exactly one matching action exists; literal hrefs remain destinations
        # in their own right and are never treated as verified deployment URLs.
        for navigation_link in source.metadata.get("navigation_links", []):
            controller, action = navigation_link.get("controller"), navigation_link.get("action")
            href = navigation_link.get("href", "")
            route_match = re.match(r'^/([A-Za-z_][\w-]*)/([A-Za-z_][\w-]*)(?:[?#].*)?$', href)
            if not action and route_match:
                controller, action = route_match.groups()
            if not action:
                continue
            candidates = [s for s in by_kind_name.get(("method", action), []) if
                          (owner := by_id.get(s.metadata.get("parent_id"))) and
                          owner.name == str(controller) + "Controller"] if controller else []
            candidates = [s for s in candidates if not re.search(
                r'\[Http(?:Post|Put|Patch|Delete)(?:\]|\()', s.metadata.get("signature", ""))]
            # Link targets conventionally invoke GET actions. An action with an
            # explicit non-GET verb is not a verified page destination.
            if candidates:
                edge_name = ".".join(filter(None, [controller, action]))
                link(source, "NAVIGATES_TO", edge_name, candidates,
                     "razor_anchor_controller_action" if navigation_link.get("action") else "razor_internal_href_convention",
                     "Razor anchor destination", navigation_link.get("line"), "static_pattern")
                edge = edge_index[(source.id, "NAVIGATES_TO", edge_name)]
                fact = {"controller": controller, "action": action,
                    "href": href, "label": navigation_link.get("label", ""),
                    "condition": navigation_link.get("condition", ""),
                    "origin": navigation_link.get("origin", "razor_anchor"),
                    "route_expression": navigation_link.get("href_expression", ""),
                    "route_template": href,
                    "query_parameters": sorted(set(re.findall(r'[?&]([A-Za-z_][\w-]*)=', href)) |
                                                set(navigation_link.get("route_values", {}))),
                    "dynamic_parameters": navigation_link.get("dynamic_parameters") or
                        [list(pair) for pair in re.findall(r'\$\{([^}]+)\}|@([A-Za-z_]\w*)', href)],
                    "route_values": navigation_link.get("route_values", {}), "user_visible": True}
                fact["line"] = navigation_link.get("line")
                facts = edge.metadata.setdefault("navigation_links", [])
                if fact not in facts:
                    facts.append(fact)
                edge.metadata.update(fact)
        model = source.metadata.get("model")
        if model:
            link(source, "USES_MODEL", model, [*by_kind_name.get(("class", model), []), *by_kind_name.get(("record", model), [])],
                 "razor_model_directive", "Razor @model directive")
        for name in set(source.metadata.get("environment_variables", [])) | set(source.metadata.get("semantic", {}).get("environment_names", [])):
            link(source, "USES_CONFIGURATION", name, by_kind_name.get(("configuration", name), []),
                 "environment_key_reference", "explicit environment variable name")
        # Non-JS parsers can emit the same simple HTTP facts without JS scope metadata.
        if files[source.file_id].language in {"javascript", "jsx", "typescript", "tsx"}:
            continue
        requests = list(source.metadata.get("requests", []))
        if files[source.file_id].language == "csharp":
            for call in source.metadata.get("calls", []):
                if call.get("receiver_type", "").split(".")[-1] != "HttpClient":
                    continue
                operation = re.fullmatch(r'(Get|Post|Put|Patch|Delete)(?:Async|AsJsonAsync)', call["name"])
                if not operation:
                    continue
                pattern = re.escape(call["receiver"] + "." + call["name"]) + r'\s*\(\s*"([^"\\]+)"'
                requests.extend({"path": match[1], "method": operation[1].upper()} for match in re.finditer(pattern, source.source))
        for request in requests:
            path, method = request.get("path", ""), request.get("method", "GET").upper()
            if not path:
                continue
            from urllib.parse import urlsplit
            route_path = urlsplit(path).path if path.startswith(("http://", "https://")) else path
            candidates = [s for s in endpoints if s.metadata.get("method") == method and path_matches(route_path,
                          re.sub(r"\{([^}:]+)(?::[^}]+)?\}", r":\1", s.metadata.get("path", "")))]
            link(source, "REQUESTS", method + " " + path, candidates, "static_method_path_candidate",
                 "literal HTTP method and path; deployment identity unverified", request.get("line_start"), "static_pattern")


def chunks(knowledge: Knowledge, application: str) -> list[DocumentChunk]:
    files = {f.id: f for f in knowledge.files}
    names = {e.id: e.qualified_name for e in knowledge.entities}
    outgoing = {}
    incident = defaultdict(list)
    for relation in knowledge.relationships:
        incident[relation.source_id].append(relation)
        if relation.target_id and relation.target_id != relation.source_id:
            incident[relation.target_id].append(relation)
        if relation.target_id and relation.kind in {"CALLS", "CALLS_PROCEDURE", "READS", "WRITES", "REQUESTS", "RENDERS"}:
            outgoing.setdefault(relation.source_id, []).append(relation)
    result = []
    for entity in knowledge.entities:
        if entity.kind == "namespace":
            continue
        related = []
        navigation_links = list(entity.metadata.get("navigation_links", []))
        for relation in incident.get(entity.id, []):
            if relation.kind == "NAVIGATES_TO" and relation.target_id == entity.id:
                navigation_links.extend(relation.metadata.get("navigation_links", []))
        # Keep route/action details compact and ordered for embedding and context.
        deduplicated_links = []
        seen_navigation = set()
        for item in navigation_links:
            key = (item.get("label"), item.get("route_template") or item.get("href"),
                   item.get("condition"), tuple(item.get("query_parameters", [])))
            if key not in seen_navigation:
                seen_navigation.add(key)
                deduplicated_links.append(item)
        navigation_links = deduplicated_links
        for relation in incident.get(entity.id, []):
            if relation.kind in {"DECLARES_INTEGRATION", "REFERENCES_INTEGRATION", "USES_INTEGRATION", "IMPORTS_MODULE", "USES_CONFIGURATION"} and entity.kind != "integration":
                continue  # Preserve structural embedding inputs; semantic facts have their own branch.
            if relation.source_id == entity.id:
                related.append(f"{entity.qualified_name} {relation.kind} {relation.target_name}")
            elif relation.target_id == entity.id:
                related.append(
                    f"{names[relation.source_id]} {relation.kind} {entity.qualified_name}"
                )
        source = entity.source if entity.kind == "python_module" else entity.metadata.get("summary", entity.source)
        # Bounded explicit paths describe cross-layer flow, without inferred edges.
        paths = []
        if entity.kind in {"method", "constructor", "procedure", "function", "component", "endpoint", "razor_view", "html_element"}:

            def walk(current: str, path: list[str], visited: set[str], depth: int) -> None:
                edges = outgoing.get(current, [])
                if not edges or depth == 4:
                    if len(path) > 1:
                        paths.append("Application data flow: " + " -> ".join(path))
                    return
                for edge in edges[:10]:
                    if edge.target_id not in visited and len(paths) < 20:
                        walk(
                            edge.target_id,
                            [*path, edge.kind, names[edge.target_id]],
                            visited | {edge.target_id},
                            depth + 1,
                        )

            walk(entity.id, [entity.qualified_name], {entity.id}, 0)
        content = "\n".join(
            [
                f"{entity.kind}: {entity.qualified_name}",
                entity.metadata.get("documentation", ""),
                source,
                *related,
                *paths,
            ]
        )
        file = files[entity.file_id]
        result.append(
            DocumentChunk(
                stable_id(entity.id, "chunk"),
                entity.id,
                entity.qualified_name,
                entity.kind,
                file.path,
                file.language,
                content,
                {
                    "source": entity.id,
                    "entity_name": entity.name,
                    "semantic": entity.metadata.get("semantic", {}),
                    "integration_type": entity.metadata.get("integration_type"),
                    "line_start": entity.line_start,
                    "line_end": entity.line_end,
                    "symbol": entity.id if file.language != "mysql" else None,
                    "database_object": entity.id if file.language == "mysql" else None,
                    "relationships": related,
                    "relation_evidence": [{"direction": "outgoing" if r.source_id == entity.id else "incoming",
                                           "kind": r.kind, "target_name": r.target_name,
                                           "target_id": r.target_id, "source_name": names[r.source_id],
                                           "confidence": r.confidence, "evidence": r.evidence,
                                           "metadata": r.metadata}
                                          for r in incident.get(entity.id, [])],
                    "requests": entity.metadata.get("requests", []),
                    "routes": entity.metadata.get("routes", []),
                    "incoming_routes": entity.metadata.get("incoming_routes", []),
                    "ui_text": entity.metadata.get("ui_text", []),
                    "string_evidence": entity.metadata.get("string_evidence", []),
                    "navigation_links": navigation_links,
                    "page_title": entity.metadata.get("page_title", ""),
                    "navigation_candidate": bool(entity.metadata.get("navigation_links") or entity.metadata.get("routes") or
                        entity.metadata.get("incoming_routes") or entity.kind in {"endpoint", "razor_view"} or
                        any(r.kind in {"RENDERS", "REQUESTS", "NAVIGATES_TO", "SUBMITS_TO", "LINKS_TO"}
                            for r in incident.get(entity.id, []))),
                    "endpoint": {"method": entity.metadata["method"], "path": entity.metadata["path"]} if entity.kind == "endpoint" else None,
                    "relationship_paths": paths,
                    "tenant": None,
                    "application": application,
                    "resource": entity.qualified_name,
                    "permission": None,
                    "visibility": None,
                    "source_type": entity.metadata.get("source_type", "code"),
                    "document_heading": entity.metadata.get("document_heading"),
                    "document_version": entity.metadata.get("document_version"),
                    "document_date": entity.metadata.get("document_date"),
                    "document_status": entity.metadata.get("document_status"),
                    "freshness": entity.metadata.get("freshness"),
                },
            )
        )
        if entity.kind == "python_module":
            # Retrieval keeps actual module-level assignments/model configuration.
            # Preserve the compact embedding input independently of source length.
            result[-1].embedding_text = "\n".join([
                f"{entity.kind}: {entity.qualified_name}",
                entity.metadata.get("documentation", ""),
                entity.metadata.get("summary", entity.source), *related, *paths])
        if file.language == "documentation":
            result[-1].embedding_text = "\n".join(filter(None, [
                f"Project documentation: {file.path}",
                f"Section: {entity.qualified_name}",
                entity.source[:900],
            ]))[:1100]
        if file.language in {"javascript", "jsx", "typescript", "tsx", "html", "css"}:
            from lens.repository.documents import embedding_summary, retrieval_document
            result[-1].embedding_text = embedding_summary(entity, file.path, related, paths)
            result[-1].text = retrieval_document(entity, file.path, related, paths)
        if (file.language == "razor" and entity.kind == "razor_view") or navigation_links:
            nav_lines = []
            for item in navigation_links[:24]:
                destination = item.get("route_template") or item.get("href") or item.get("href_expression", "")
                target = ".".join(filter(None, [item.get("controller", ""), item.get("action", "")]))
                label = item.get("label", "")
                query = ", ".join(item.get("query_parameters", []))
                condition = item.get("condition", "")
                nav_lines.append(" | ".join(value for value in (
                    "UI label: " + label if label else "",
                    "Destination: " + destination if destination else "",
                    "Controller/action: " + target if target else "",
                    "Parameters: " + query if query else "",
                    "Available when: " + condition if condition else "") if value))
            title = entity.metadata.get("page_title", "")
            page_identity = ("Razor page " if file.language == "razor" else "MVC action ") + file.path + " " + entity.qualified_name + ("; page title " + title if title else "")
            summary = "\n".join([page_identity, *nav_lines])[:2200]
            result[-1].text += "\nNavigation evidence:\n" + summary
            result[-1].embedding_text = summary[:1600]
        if entity.kind == "integration":
            from lens.repository.integrations import fact_lines
            facts = "\n".join(fact_lines(entity))
            result[-1].text += "\n" + facts
            lines = [f"External integration evidence: {entity.name}"]
            for line in fact_lines(entity):
                if len("\n".join([*lines, line])) <= 1800:
                    lines.append(line)
            result[-1].embedding_text = "\n".join(lines)
    return result
