"""Relative ES module and lexical resolution; unknown runtime values stay unresolved."""

import posixpath
from lens.core.models import CodeSymbol, Knowledge, Relationship

FRONTEND_LANGUAGES = {"javascript", "jsx", "typescript", "tsx"}
EXTENSIONS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


class ModuleResolver:
    def __init__(self, knowledge: Knowledge):
        self.symbols = {s.id: s for s in knowledge.symbols}
        self.modules = {s.file_id: s for s in knowledge.symbols if s.kind == "module"}
        self.paths = {f.path: self.modules[f.id] for f in knowledge.files if f.id in self.modules}
        self.children: dict[str, list[CodeSymbol]] = {}
        for symbol in knowledge.symbols:
            parent = symbol.metadata.get("parent_id")
            if parent:
                self.children.setdefault(parent, []).append(symbol)

    def module_target(self, module: CodeSymbol, source: str) -> CodeSymbol | None:
        if not source.startswith("."):
            return None  # package imports and aliases are not guessed
        file_path = next((path for path, candidate in self.paths.items() if candidate.id == module.id), None)
        if file_path is None:
            return None
        path = posixpath.normpath(posixpath.join(posixpath.dirname(file_path), source))
        if path.startswith("../") or path.startswith("/"):
            return None
        if path in self.paths:
            return self.paths[path]
        candidates = [self.paths[name] for name in
                      [*(path + extension for extension in EXTENSIONS),
                       *(path + "/index" + extension for extension in EXTENSIONS)] if name in self.paths]
        if path.endswith(".js") and not candidates:
            candidates = [self.paths[path[:-3] + ext] for ext in (".ts", ".tsx") if path[:-3] + ext in self.paths]
        return candidates[0] if len(candidates) == 1 else None

    def exported(self, module: CodeSymbol, name: str, seen: frozenset = frozenset()) -> CodeSymbol | None:
        key = (module.id, name)
        if key in seen:
            return None
        seen = seen | {key}
        matches = []
        for export in module.metadata.get("exports", []):
            if export["exported"] != name and not (export["exported"] == "*" and name != "default"):
                continue
            if export.get("entity_id"):
                matches.append(self.symbols[export["entity_id"]])
            elif export.get("source"):
                target = self.module_target(module, export["source"])
                if target:
                    target_name = name if export["exported"] == "*" else export["local"]
                    resolved = self.exported(target, target_name, seen)
                    if resolved:
                        matches.append(resolved)
            else:
                own = [s for s in self.children.get(module.id, []) if s.name == export["local"]]
                if own:
                    matches.extend(own)
                else:
                    resolved = self.imported(module, export["local"], seen)
                    if resolved:
                        matches.append(resolved)
        unique = {s.id: s for s in matches}
        return next(iter(unique.values())) if len(unique) == 1 else None

    def imported(self, module: CodeSymbol, local: str, seen: frozenset = frozenset()) -> CodeSymbol | None:
        entries = [item for item in module.metadata.get("imports", []) if item["local"] == local]
        if len(entries) != 1:
            return None
        item = entries[0]
        target = self.module_target(module, item["source"])
        if not target:
            return None
        return target if item["imported"] in {"*", "side_effect"} else self.exported(target, item["imported"], seen)

    def lookup(self, source: CodeSymbol, name: str, position: int | None = None) -> CodeSymbol | None:
        root, *members = name.split(".")
        position = source.metadata.get("start_byte", 0) if position is None else position
        owner = source
        resolved = None
        while owner:
            matches = [s for s in self.children.get(owner.id, []) if s.name == root and
                       s.metadata.get("scope", [0, 10**12])[0] <= position <= s.metadata.get("scope", [0, 10**12])[1]]
            if owner.name == root and owner.kind in {"function", "component", "method"}:
                matches.append(owner)
            unique = {s.id: s for s in matches}
            if unique:
                resolved = next(iter(unique.values())) if len(unique) == 1 else None
                break
            if root in owner.metadata.get("parameter_bindings", []) or root in owner.metadata.get("local_bindings", []):
                return None
            if root == "this" and owner.kind in {"class", "component_class"}:
                resolved = owner
                break
            parent_id = owner.metadata.get("parent_id")
            owner = self.symbols.get(parent_id)
        module = self.modules.get(source.file_id)
        if not resolved and module:
            resolved = self.imported(module, root)
        for member in members:
            if not resolved:
                return None
            if resolved.kind == "module":
                resolved = self.exported(resolved, member)
            else:
                matches = [s for s in self.children.get(resolved.id, []) if s.name == member]
                resolved = matches[0] if len(matches) == 1 else None
        return resolved

    def shadowed_http(self, source: CodeSymbol, root: str) -> bool:
        owner = source
        while owner:
            if owner.kind == "module" and (root in owner.metadata.get("http_clients", {}) or
                    any(i["local"] == root and i.get("binding_byte") is not None for i in owner.metadata.get("imports", []))):
                return False  # Proven client/CommonJS import binding, rather than an unrelated local receiver.
            if root in owner.metadata.get("parameter_bindings", []) or root in owner.metadata.get("local_bindings", []):
                return True
            if any(child.name == root for child in self.children.get(owner.id, [])):
                return True
            owner = self.symbols.get(owner.metadata.get("parent_id"))
        return False


def resolve(knowledge: Knowledge) -> None:
    resolver = ModuleResolver(knowledge)
    # Mount prefixes come from literal Express registrations, including bounded
    # literal loops. Preserve the router-local declaration and its source range.
    endpoints = [s for s in knowledge.symbols if s.kind == "endpoint"]
    by_file = {}
    for endpoint in endpoints:
        by_file.setdefault(endpoint.file_id, []).append(endpoint)
    for module in resolver.modules.values():
        for mount in module.metadata.get("router_mounts", []):
            target = resolver.module_target(module, mount["source"])
            if target:
                for endpoint in by_file.get(target.file_id, []):
                    path = mount["prefix"].rstrip("/") + "/" + endpoint.metadata["path"].lstrip("/")
                    endpoint.metadata.setdefault("mounted_paths", []).append(path)
                    endpoint.metadata.setdefault("mount_evidence", []).append({**mount, "module_id": module.id})
    for relation in knowledge.relationships:
        source = resolver.symbols.get(relation.source_id)
        if not source or source.file_id not in resolver.modules or relation.target_id:
            continue
        module = resolver.modules[source.file_id]
        target = None
        if relation.kind == "IMPORTS":
            if source.kind == "module":
                target = resolver.imported(module, relation.target_name)
            else:
                sites = [site for site in [*source.metadata.get("call_sites", []), *source.metadata.get("reference_sites", [])]
                         if site["callee"].split(".", 1)[0] == relation.target_name]
                targets = [resolver.lookup(source, relation.target_name, site["offset"]) for site in sites]
                unique = {candidate.id: candidate for candidate in targets if candidate}
                target = next(iter(unique.values())) if len(unique) == 1 and all(targets) else None
        elif relation.kind == "EXPORTS":
            target = resolver.exported(module, relation.target_name)
        elif relation.kind in {"CALLS", "HANDLES", "REFERENCES", "RENDERS", "EXTENDS", "IMPLEMENTS"}:
            sites = [site for site in source.metadata.get("call_sites" if relation.kind == "CALLS" else "reference_sites", []) if site["callee"] == relation.target_name]
            positions = [site["offset"] for site in sites] or [source.metadata.get("start_byte", 0)]
            targets = [resolver.lookup(source, relation.target_name, position) for position in positions]
            unique = {candidate.id: candidate for candidate in targets if candidate}
            if len(unique) == 1 and all(targets):
                target = next(iter(unique.values()))
            if relation.kind == "RENDERS" and target and target.kind not in {"component", "component_class", "function", "class"}:
                target = None
        if target:
            relation.target_id = target.id
            relation.confidence = "high"
    # Enrich only wrappers whose source explicitly composes a parameter into fetch's URL.
    from urllib.parse import urlsplit
    for symbol in list(knowledge.symbols):
        if symbol.file_id not in resolver.modules:
            continue
        for site in symbol.metadata.get("unknown_request_sites", []):
            target = resolver.lookup(symbol, site["callee"], site["offset"])
            if target and target.metadata.get("http_wrapper"):
                knowledge.relationships.append(Relationship(symbol.id, "REQUESTS", "? [dynamic URL]",
                    evidence="wrapper URL/method expression cannot be proven", metadata={
                        "rule": "unresolved_wrapper_expression", "wrapper_id": target.id,
                        "expression": site["expression"], "source_line_start": site["line_start"],
                        "source_line_end": site["line_end"]}))
        for request in symbol.metadata.get("requests", []):
            path = request["path"]
            method = request["method"]
            if not request["direct"]:
                target = resolver.lookup(symbol, request["callee"], request["offset"])
                wrapper = target.metadata.get("http_wrapper") if target else None
                if not wrapper:
                    continue
                path = wrapper["prefix"] + path + wrapper["suffix"]
                if wrapper.get("options_parameter"):
                    method = wrapper.get("default_method", "GET") if not request.get("options_supplied", True) else method if request.get("options_known", False) else None
                else:
                    method = wrapper["method"]
            shadowed = request["direct"] and resolver.shadowed_http(symbol, request["callee"].split(".")[0])
            match_path = urlsplit(path).path if path.startswith(("http://", "https://")) else path
            external_host = path.startswith(("http://", "https://")) and urlsplit(path).hostname not in {"localhost", "127.0.0.1", "::1"}
            matches = [endpoint for endpoint in endpoints if not shadowed and not external_host and endpoint.metadata["method"] == method and
                       any(path_matches(match_path, declared) for declared in endpoint.metadata.get("mounted_paths", [endpoint.metadata["path"]]))]
            target = matches[0] if len(matches) == 1 else None
            if not request["direct"]:
                knowledge.relationships.append(Relationship(symbol.id, "REQUESTS", f"{method or '?'} {path}",
                                                             target.id if target else None,
                                                             f"literal wrapper call via {request['callee']}",
                                                             "static_pattern" if target and request["dynamic"] else "high" if target else "unresolved"))
            else:
                for relation in knowledge.relationships:
                    if relation.source_id == symbol.id and relation.kind == "REQUESTS" and relation.target_name == f"{method or '?'} {path}":
                        relation.target_id = target.id if target else None
                        relation.confidence = "static_pattern" if target and request["dynamic"] else "high" if target else "unresolved"
            request["resolved_path"] = path
            request["target_id"] = target.id if target else None
            request["resolved_method"] = method
            for relation in knowledge.relationships:
                if relation.source_id == symbol.id and relation.kind == "REQUESTS" and relation.target_name == f"{method or '?'} {path}":
                    relation.metadata.update({"rule": "unique_method_path_match" if target else "unresolved_method_path",
                        "method": method, "path": path, "dynamic": request["dynamic"],
                        "expression": request["expression"],
                        "source_line_start": request.get("line_start", symbol.line_start),
                        "source_line_end": request.get("line_end", symbol.line_end),
                        "wrapper_id": target_wrapper.id if (target_wrapper := resolver.lookup(symbol, request["callee"], request["offset"])) and not request["direct"] else None})
                    if request.get("client_configuration"):
                        relation.metadata["client_configuration"] = request["client_configuration"]
                        relation.confidence = "static_pattern" if target else "unresolved"
                    if target and target.metadata.get("mount_evidence"):
                        relation.metadata["mount_evidence"] = target.metadata["mount_evidence"]


def path_matches(request: str, declared: str) -> bool:
    """Match static path shapes; no host inference or expression evaluation."""
    if not request.startswith("/"):
        return False
    request = request.split("?", 1)[0].split("#", 1)[0]
    actual = request.split("/")
    route = declared.split("/")
    if len(actual) != len(route):
        return False
    return all(a == b or (b.startswith(":") and a != "") for a, b in zip(actual, route))
