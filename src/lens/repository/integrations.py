"""Syntax-grounded integration identities and semantic facts, without vendor registries."""
import posixpath
import re
from urllib.parse import urlsplit
from lens.core.models import CodeSymbol, Relationship, stable_id
from lens.parsing.syntax import tree, walk, owned, text, literal, bindings, FUNCTIONS
from lens.search.semantics import evidence_concepts, words


def extract(knowledge, contents):
    files = {f.id: f for f in knowledge.files}
    modules = {s.file_id: s for s in knowledge.symbols if s.kind == "module" and
               files[s.file_id].language in {"javascript", "jsx", "typescript", "tsx"}}
    require_shadows = {}
    for file_id, module in modules.items():
        file, data = files[file_id], contents[file_id]
        root = tree(file.language, data)
        all_nodes = list(walk(root))
        imports = list(module.metadata.get("imports", []))
        # Top-level, literal CommonJS imports; computed or shadowed require stays unknown.
        require_shadow = any(s.file_id == file_id and s.name == "require" for s in knowledge.symbols) or any(
            n.type in FUNCTIONS and "require" in bindings(n.child_by_field_name("parameters") or n.child_by_field_name("parameter"), data) for n in all_nodes)
        require_shadow |= any(n.type == "assignment_expression" and text(n.child_by_field_name("left"), data) == "require" for n in all_nodes)
        require_shadows[file_id] = require_shadow
        if not require_shadow:
            for statement in root.named_children:
                if statement.type not in {"lexical_declaration", "variable_declaration"}:
                    continue
                for node in statement.named_children:
                    if node.type != "variable_declarator":
                        continue
                    value = node.child_by_field_name("value")
                    if not value:
                        continue
                    call = value.child_by_field_name("object") if value.type == "member_expression" else value
                    if not call or call.type != "call_expression" or text(call.child_by_field_name("function"), data) != "require":
                        continue
                    args = call.child_by_field_name("arguments")
                    source = literal(args.named_children[0], data) if args and len(args.named_children) == 1 else None
                    if source:
                        imports.extend({"local": local, "source": source, "imported": "*",
                                        "line": node.start_point.row + 1, "binding_byte": node.start_byte} for local in bindings(node.child_by_field_name("name"), data)
                                       if not any(i["local"] == local and i["source"] == source and i.get("binding_byte") == node.start_byte for i in imports))
        providers = {}
        for entry in imports:
            source = entry["source"]
            if source.startswith((".", "/", "node:")):
                continue
            # Package identity is literal, not a claim that every package is a remote service.
            key = source
            if key not in providers:
                line = entry.get("line") or next((n.start_point.row + 1 for n in root.named_children
                    if n.type == "import_statement" and literal(n.child_by_field_name("source"), data) == source), module.line_start)
                provider = CodeSymbol(stable_id(file.id, "integration", key), key, "integration",
                    file.path + "::integration:" + key, file.id, line, line, source,
                    {"integration_type": "package", "identity": key, "static_only": True})
                providers[key] = provider
                knowledge.symbols.append(provider)
                knowledge.relationships.append(Relationship(module.id, "DECLARES_INTEGRATION", provider.qualified_name,
                    provider.id, "literal external package import", "high", {"rule": "literal_package_import", "source_line_start": line}))
        exports = []
        export_assignments = 0
        for statement in root.named_children:
            assignment = statement.named_children[0] if statement.type == "expression_statement" and statement.named_children else None
            if assignment and assignment.type == "assignment_expression" and text(assignment.child_by_field_name("left"), data) == "module.exports":
                export_assignments += 1
                value = assignment.child_by_field_name("right")
                if value and value.type == "identifier":
                    entries = [i for i in imports if i["local"] == text(value, data)]
                    if len(entries) == 1 and entries[0]["source"] in providers:
                        exports.append(providers[entries[0]["source"]].qualified_name)
        module.metadata["integration_exports"] = exports if len(exports) == 1 and export_assignments == 1 else []
        owners = [s for s in knowledge.symbols if s.file_id == file_id and s.kind != "integration"]
        for symbol in owners:
            node = next((n for n in walk(root) if n.start_byte == symbol.metadata.get("start_byte") and n.end_byte == symbol.metadata.get("end_byte")
                         and (n.type == "program") == (symbol.kind == "module")), None)
            if node is None:
                continue
            nodes = list(owned(node))
            actions = sorted(set(text(n.child_by_field_name("function") or n.child_by_field_name("constructor"), data)
                for n in nodes if n.type in {"call_expression", "new_expression"}))
            actions = [a for a in actions if a and len(a) <= 120]
            env = sorted(set(re.findall(r"process\.env\.([A-Za-z_$][\w$]*)", text(node, data))))
            hosts = set()
            for n in nodes:
                if n.type not in {"string", "template_string"}:
                    continue
                # Template interpolation after a literal hostname does not invalidate that hostname.
                raw = text(n, data)[1:-1]
                match = re.match(r"https?://([A-Za-z0-9.-]+)(?=[:/]|$)", raw)
                if not match:
                    continue
                host = urlsplit("https://" + match[1]).hostname
                if not host or host in {"localhost", "127.0.0.1"}:
                    continue
                hosts.add(host)
                if host not in providers:
                    line = n.start_point.row + 1
                    provider = CodeSymbol(stable_id(file.id, "integration", "host", host), host, "integration",
                        file.path + "::integration:" + host, file.id, line, line, host,
                        {"integration_type": "host", "identity": host, "static_only": True})
                    providers[host] = provider
                    knowledge.symbols.append(provider)
                target = providers[host]
                knowledge.relationships.append(Relationship(symbol.id, "REFERENCES_INTEGRATION", target.qualified_name,
                    target.id, "literal URL hostname (reference, not proof of runtime request)", "high",
                    {"rule": "literal_hostname", "source_line_start": n.start_point.row + 1}))
            used = []
            for entry in imports:
                provider = providers.get(entry["source"])
                if not provider or entry["local"] in symbol.metadata.get("parameter_bindings", []):
                    continue
                alias = entry["local"]
                if sum(item["local"] == alias for item in imports) != 1:
                    continue
                # Exclude any nested local rebinding, rather than guessing scope/alias propagation.
                shadow = any(n.type == "variable_declarator" and alias in bindings(n.child_by_field_name("name"), data)
                             for n in all_nodes if n.start_byte not in {entry.get("binding_byte")})
                shadow |= any(n.type == "assignment_expression" and text(n.child_by_field_name("left"), data) == alias for n in all_nodes)
                shadow |= any(n.type in FUNCTIONS and alias in bindings(n.child_by_field_name("parameters") or n.child_by_field_name("parameter"), data) for n in nodes)
                parent = symbol
                while parent.metadata.get("parent_id"):
                    parent = next(s for s in owners if s.id == parent.metadata["parent_id"])
                    if alias in parent.metadata.get("parameter_bindings", []):
                        shadow = True
                calls = [n for n in nodes if n.type in {"call_expression", "new_expression"} and
                         text(n.child_by_field_name("function") or n.child_by_field_name("constructor"), data).split(".")[0] == alias]
                if calls and not shadow:
                    used.append(provider.qualified_name)
                    for call in calls:
                        knowledge.relationships.append(Relationship(symbol.id, "USES_INTEGRATION", provider.qualified_name,
                            provider.id, text(call.child_by_field_name("function") or call.child_by_field_name("constructor"), data), "high",
                            {"rule": "import_bound_receiver", "source_line_start": call.start_point.row + 1,
                             "configuration_evidence": bool((call.type == "new_expression" or re.search(r"config|initializ|create|connect", text(call.child_by_field_name("function"), data), re.I))
                                 and re.search(r"credential|api_key|api_secret|process\.env|https?://", text(call, data), re.I))}))
            sql = [site["sql"] for site in symbol.metadata.get("sql_sites", [])]
            facts_text = " ".join([symbol.name, *actions, *env, *hosts, *sql, symbol.metadata.get("documentation", "")])
            symbol.metadata["semantic"] = {"concepts": evidence_concepts(facts_text),
                "actions": actions[:24], "sql_operations": sql[:8], "environment_names": env[:24], "hosts": sorted(hosts),
                "integrations": sorted(set([*used, *(providers[h].qualified_name for h in hosts)])),
                "vocabulary": sorted(words(facts_text))[:96], "static_only": True,
                "remote_configuration": bool(hosts or any(re.search(r"key|secret|credential|token", name, re.I) for name in env)
                    or any("credential" in action.lower() for action in actions))}
    # Literal relative CommonJS references connect configuration modules without claiming calls.
    paths = {files[fid].path: mod for fid, mod in modules.items()}
    for file_id, module in modules.items():
        data = contents[file_id]
        for node in owned(tree(files[file_id].language, data)):
            if node.type != "call_expression" or text(node.child_by_field_name("function"), data) != "require":
                continue
            args = node.child_by_field_name("arguments")
            source = literal(args.named_children[0], data) if args and len(args.named_children) == 1 else None
            if not source or not source.startswith("."):
                continue
            path = posixpath.normpath(posixpath.join(posixpath.dirname(files[file_id].path), source))
            matches = [paths[p] for p in [path, path + ".js", path + "/index.js"] if p in paths]
            if len(matches) == 1 and not require_shadows[file_id]:
                target = matches[0]
                knowledge.relationships.append(Relationship(module.id, "IMPORTS_MODULE", target.qualified_name,
                    target.id, "literal relative CommonJS require", "high", {"rule": "literal_relative_require",
                    "source_line_start": node.start_point.row + 1}))
                declarator = node.parent
                while declarator and declarator.type not in {"variable_declarator", "program"}:
                    declarator = declarator.parent
                if declarator and declarator.type == "variable_declarator":
                    aliases = bindings(declarator.child_by_field_name("name"), data)
                    for symbol in knowledge.symbols:
                        if symbol.file_id != file_id or symbol.kind == "integration" or symbol.id == module.id:
                            continue
                        for alias in aliases:
                            if alias in symbol.metadata.get("parameter_bindings", []):
                                continue
                            if any(alias in ancestor.metadata.get("parameter_bindings", []) for ancestor in knowledge.symbols
                                   if ancestor.file_id == file_id and ancestor.line_start <= symbol.line_start <= ancestor.line_end):
                                continue
                            call_sites = symbol.metadata.get("call_sites", [])
                            if any(site["callee"].split(".")[0] == alias for site in call_sites) and not re.search(r"\b(?:const|let|var)\s+" + re.escape(alias) + r"\b|\b" + re.escape(alias) + r"\s*=(?!=)", symbol.source):
                                callee = next(site["callee"] for site in call_sites if site["callee"].split(".")[0] == alias)
                                knowledge.relationships.append(Relationship(symbol.id, "USES_CONFIGURATION", target.qualified_name,
                                    target.id, callee, "high", {"rule": "require_bound_receiver"}))


def documents(knowledge):
    """Propagate only explicit integration references, not similarity-derived graph edges."""
    by_id = {s.id: s for s in knowledge.entities}
    for edge in knowledge.relationships:
        if edge.kind not in {"USES_INTEGRATION", "REFERENCES_INTEGRATION"} or not edge.target_id:
            continue
        source, target = by_id[edge.source_id], by_id[edge.target_id]
        target.metadata.setdefault("semantic", {}).setdefault("usage", []).append(source.qualified_name)
        concepts = source.metadata.get("semantic", {}).get("concepts", []) if edge.kind == "REFERENCES_INTEGRATION" else evidence_concepts(edge.evidence)
        target.metadata["semantic"]["concepts"] = sorted(set(target.metadata["semantic"].get("concepts", [])) | set(concepts))
        target.metadata["semantic"]["actions"] = sorted(set(target.metadata["semantic"].get("actions", [])) |
            (set(source.metadata.get("semantic", {}).get("actions", [])) if edge.kind == "REFERENCES_INTEGRATION" else {edge.evidence}))[:24]
        target.metadata["semantic"]["remote_configuration"] = bool(target.metadata["semantic"].get("remote_configuration") or
            edge.kind == "REFERENCES_INTEGRATION" or edge.metadata.get("configuration_evidence", False))
    for edge in knowledge.relationships:
        if edge.kind == "USES_CONFIGURATION" and edge.target_id:
            source, target = by_id[edge.source_id], by_id[edge.target_id]
            facts = source.metadata.setdefault("semantic", {})
            facts["integrations"] = sorted(set(facts.get("integrations", [])) | set(target.metadata.get("integration_exports", [])))
            facts["remote_configuration"] = target.metadata.get("semantic", {}).get("remote_configuration", False)
            for provider_name in facts["integrations"]:
                provider = next((s for s in by_id.values() if s.qualified_name == provider_name), None)
                if provider:
                    provider.metadata["semantic"]["concepts"] = sorted(set(provider.metadata["semantic"].get("concepts", [])) | set(evidence_concepts(edge.evidence)))
                    provider.metadata["semantic"]["actions"] = sorted(set(provider.metadata["semantic"].get("actions", [])) | {edge.evidence})
    for edge in knowledge.relationships:
        if edge.kind == "USES_INTEGRATION" and edge.target_id:
            source, target = by_id[edge.source_id], by_id[edge.target_id]
            source.metadata["semantic"]["remote_configuration"] |= target.metadata.get("semantic", {}).get("remote_configuration", False)


def fact_lines(entity):
    facts = entity.metadata.get("semantic", {})
    lines = []
    for key, label in (("actions", "Source calls"), ("environment_names", "Environment variable names"),
                       ("hosts", "Literal external hostnames"), ("integrations", "Integration references"),
                       ("usage", "Referenced by")):
        if facts.get(key):
            lines.append(label + ": " + ", ".join(facts[key][:12]))
    if entity.kind == "integration":
        lines.insert(0, "Literal " + entity.metadata["integration_type"] + " identity: " + entity.name)
    return lines
