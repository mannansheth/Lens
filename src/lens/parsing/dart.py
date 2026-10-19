"""Structural Dart parsing for Flutter and server-side Dart source."""

import re
from tree_sitter import Node, Parser
from lens.core.models import CodeSymbol, Knowledge, Relationship, stable_id
from lens.parsing.syntax import language, text, walk

_DEFINITIONS = {
    "class_definition": "class",
    "enum_declaration": "enum",
    "mixin_declaration": "mixin",
    "extension_declaration": "extension",
    "type_alias": "type",
}
_HTTP_METHODS = {
    "get": "GET", "getJson": "GET", "getJsonValue": "GET",
    "post": "POST", "postJson": "POST", "postJsonValue": "POST", "postMultipart": "POST",
    "put": "PUT", "putJson": "PUT", "patch": "PATCH", "patchJson": "PATCH",
    "delete": "DELETE", "deleteJson": "DELETE",
}
_CALL = re.compile(
    r"\b(?P<receiver>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\.\s*"
    r"(?P<method>getJson|getJsonValue|postJson|postJsonValue|postMultipart|putJson|patchJson|deleteJson|"
    r"get|post|put|patch|delete)(?:\s*<[^;(){}]*>)?\s*\(\s*"
    r"(?P<quote>['\"])(?P<path>[^'\"]+)['\"]"
)


def _descendants(node: Node, kind: str):
    return [item for item in walk(node) if item.type == kind]


def _name(node: Node, data: bytes) -> str:
    candidate = node.child_by_field_name("name")
    if candidate:
        return text(candidate, data)
    signature = next((item for item in walk(node) if item.type == "function_signature"), None)
    candidate = signature.child_by_field_name("name") if signature else None
    return text(candidate, data)


def _parameters(signature: Node, data: bytes) -> list[str]:
    listing = next(iter(_descendants(signature, "formal_parameter_list")), None)
    if not listing:
        return []
    return [text(item, data).strip() for item in _descendants(listing, "formal_parameter")]


def _http_requests(source: str, first_line: int = 1) -> list[dict]:
    requests = []
    for match in _CALL.finditer(source):
        raw_path = match.group("path")
        dynamic = "$" in raw_path
        if dynamic:
            raw_path = re.sub(r"^\$\{?base\}?", "", raw_path)
            def placeholder(value):
                expression = value.group(1) or value.group(2) or "value"
                identifiers = re.findall(r"[A-Za-z_]\w*", expression)
                return ":" + identifiers[-1] if identifiers else ":value"
            raw_path = re.sub(r"\$\{([^}]+)\}|\$([A-Za-z_]\w*)", placeholder, raw_path)
        if not raw_path.startswith(("/", "http://", "https://")):
            raw_path = "/" + raw_path
        requests.append({
            "method": _HTTP_METHODS[match.group("method")],
            "path": raw_path,
            "direct": True,
            "dynamic": dynamic,
            "expression": match.group(0),
            "line_start": first_line + source.count("\n", 0, match.start()),
            "line_end": first_line + source.count("\n", 0, match.end()),
            "callee": match.group("receiver") + "." + match.group("method"),
        })
    return requests


def parse(file, data: bytes) -> Knowledge:
    """Extract Dart modules, declarations, imports, inheritance and literal HTTP requests."""
    result = Knowledge()
    root = Parser(language("dart")).parse(data).root_node
    if root.has_error:
        result.warnings.append(f"{file.path}: dart syntax errors; recoverable declarations only")

    symbols: list[CodeSymbol] = []
    module = CodeSymbol(stable_id(file.id, "module", file.path), "module", "module",
                        file.path + "::module", file.id, 1, max(1, data.count(b"\n") + 1),
                        data.decode("utf-8", errors="replace"),
                        {"parent_id": None, "start_byte": 0, "end_byte": len(data),
                         "imports": [], "exports": [], "requests": [], "calls": []})
    result.symbols.append(module)
    symbols.append(module)

    def add(node: Node, name: str, kind: str, parent: CodeSymbol,
            end_node: Node | None = None, metadata: dict | None = None) -> CodeSymbol:
        end_node = end_node or node
        start_line = node.start_point.row + 1
        end_line = end_node.end_point.row + 1
        qualified = (file.path + "::" if parent.kind == "module" else parent.qualified_name + ".") + name
        details = {"parent_id": parent.id,
                   "start_byte": node.start_byte, "end_byte": end_node.end_byte,
                   "parameters": [], "documentation": ""}
        if metadata:
            details.update(metadata)
        previous = node.prev_named_sibling
        if previous and previous.type in {"documentation_comment", "comment"}:
            details["documentation"] = text(previous, data)
        symbol = CodeSymbol(stable_id(file.id, kind, qualified, str(node.start_byte)),
                            name, kind, qualified, file.id, start_line, end_line,
                            text(node, data) if end_node.id == node.id else
                            data[node.start_byte:end_node.end_byte].decode("utf-8", errors="replace"),
                            details)
        result.symbols.append(symbol)
        symbols.append(symbol)
        result.relationships.append(Relationship(parent.id, "CONTAINS", qualified, symbol.id,
                                                 "Dart syntax declaration", "high"))
        return symbol

    # Import/export directives are literals. Conditional imports are retained as
    # alternatives; no URI expressions are evaluated.
    for directive in (item for item in root.named_children if item.type == "import_or_export"):
        directive_text = text(directive, data)
        uri_match = re.search(r"\b(?:import|export)\s+['\"]([^'\"]+)['\"]", directive_text)
        if not uri_match:
            continue
        uri = uri_match.group(1)
        is_export = directive_text.lstrip().startswith("export ")
        prefix_match = re.search(r"\bas\s+([A-Za-z_]\w*)", directive_text)
        names = [{"name": name, "alias": name} for name in re.findall(r"\b(?:show|hide)\s+([^;]+)", directive_text)
                 for name in re.findall(r"[A-Za-z_]\w*", name)]
        entry = {"source": uri, "module": uri, "local": prefix_match.group(1) if prefix_match else uri,
                 "prefix": prefix_match.group(1) if prefix_match else "", "names": names,
                 "line": directive.start_point.row + 1}
        if is_export:
            module.metadata["exports"].append(entry)
            result.relationships.append(Relationship(module.id, "EXPORTS_MODULE", uri,
                                                     evidence="Dart export directive"))
        else:
            module.metadata["imports"].append(entry)
            result.relationships.append(Relationship(module.id, "IMPORTS_MODULE", uri,
                                                     evidence="Dart import directive"))

    definition_nodes: list[tuple[Node, CodeSymbol]] = []
    for node in _descendants(root, "class_definition"):
        name = _name(node, data)
        if not name:
            continue
        superclass = node.child_by_field_name("superclass")
        super_text = text(superclass, data) if superclass else ""
        base = re.search(r"\bextends\s+([\w.]+)", super_text)
        kind = "component" if base and (base.group(1).endswith("Widget") or
                                         re.search(r"(?:Page|Screen)$", name)) else "class"
        entity = add(node, name, kind, module, metadata={"summary": node_text_prefix(node, data),
                                                         "superclass": super_text,
                                                         "component_framework": "flutter" if kind == "component" else ""})
        definition_nodes.append((node, entity))
        if base:
            result.relationships.append(Relationship(entity.id, "INHERITS", base.group(1),
                                                     evidence="Dart extends clause"))
        mixins = re.search(r"\bwith\s+(.+?)(?:\bimplements\b|$)", super_text)
        if mixins:
            for target in re.findall(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", mixins.group(1)):
                result.relationships.append(Relationship(entity.id, "MIXES_IN", target,
                                                         evidence="Dart with clause"))
        interfaces = node.child_by_field_name("interfaces")
        if interfaces:
            for child in interfaces.named_children:
                if child.type in {"type_identifier", "identifier"}:
                    result.relationships.append(Relationship(entity.id, "IMPLEMENTS", text(child, data),
                                                             evidence="Dart implements clause"))

    for node in _descendants(root, "enum_declaration"):
        name = _name(node, data)
        if name:
            add(node, name, "enum", module, metadata={"summary": node_text_prefix(node, data)})

    # Top-level function signatures are paired with their following body node;
    # class methods follow the same pattern inside class_body.
    def add_function(signature: Node, parent: CodeSymbol, kind: str, body: Node | None = None):
        name = _name(signature, data)
        if not name:
            return
        parameters = _parameters(signature, data)
        body = body if body and body.type == "function_body" else None
        end_node = body or signature
        entity = add(signature, name, kind, parent, end_node,
                     {"parameters": parameters,
                      "return_type": text(signature.child_by_field_name("type"), data) if signature.child_by_field_name("type") else ""})
        return entity

    for node in root.named_children:
        if node.type != "function_signature":
            continue
        position = root.named_children.index(node)
        sibling = root.named_children[position + 1] if position + 1 < len(root.named_children) else None
        add_function(node, module, "function", sibling)

    for class_node, owner in definition_nodes:
        body = class_node.child_by_field_name("body")
        if not body:
            continue
        members = body.named_children
        for index, member in enumerate(members):
            if member.type == "method_signature":
                signature = next((item for item in member.named_children if item.type == "function_signature"), None)
                following = members[index + 1] if index + 1 < len(members) else None
                if signature:
                    add_function(signature, owner, "method", following)
            elif member.type in {"getter_signature", "setter_signature"}:
                name = _name(member, data)
                following = members[index + 1] if index + 1 < len(members) else None
                if name:
                    add(member, name, "method", owner, following if following and following.type == "function_body" else None,
                        {"parameters": []})
            elif member.type == "declaration":
                constructor = next((item for item in walk(member) if item.type in {
                    "constructor_signature", "constant_constructor_signature", "factory_constructor_signature"}), None)
                if constructor:
                    raw = text(constructor, data)
                    match = re.search(r"(?:const\s+|factory\s+)?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*\(", raw)
                    constructor_name = match.group(1) if match else owner.name
                    add(constructor, constructor_name, "constructor", owner,
                        metadata={"parameters": [text(p, data) for p in _descendants(constructor, "formal_parameter")]})

    # Attach literal HTTP calls to their smallest containing function/method.
    # Dynamic route segments are represented as placeholders for endpoint matching.
    for request in _http_requests(data.decode("utf-8", errors="replace")):
        owners = [symbol for symbol in symbols if symbol.kind in {"function", "method"} and
                  symbol.line_start <= request["line_start"] <= symbol.line_end]
        owner = min(owners, key=lambda item: item.line_end - item.line_start) if owners else module
        owner.metadata.setdefault("requests", []).append(request)
        result.relationships.append(Relationship(owner.id, "REQUESTS",
            request["method"] + " " + request["path"], evidence=request["expression"],
            metadata={"method": request["method"], "path": request["path"],
                      "source_line_start": request["line_start"], "source_line_end": request["line_end"],
                      "static_only": True, "dynamic": request["dynamic"]}))

    return result


def node_text_prefix(node: Node, data: bytes) -> str:
    """A compact declaration header for retrieval when a body is large."""
    raw = text(node, data)
    brace = raw.find("{")
    return raw[:brace].strip() if brace >= 0 else raw[:240].strip()
