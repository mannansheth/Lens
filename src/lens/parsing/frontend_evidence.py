"""React, HTTP, route, and inline SQL evidence from actual syntax nodes."""

import re
from tree_sitter import Node
from lens.core.models import CodeFile, CodeSymbol, Knowledge, Relationship, stable_id
from lens.parsing.syntax import FUNCTIONS, bindings, literal, owned, text, walk
from lens.parsing.static_sql import alternatives

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def path_values(node, data, values=None, depth=0):
    """Bounded literal path expressions, preserving environment fallback uncertainty."""
    if node is None or depth > 8:
        return []
    values = values or {}
    value = literal(node, data)
    if value is not None:
        return [value]
    if node.type == "identifier":
        return values.get(text(node, data), [])
    if node.type == "parenthesized_expression" and node.named_children:
        return path_values(node.named_children[0], data, values, depth + 1)
    if node.type == "binary_expression":
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        operator = text(node.child_by_field_name("operator"), data)
        a, b = path_values(left, data, values, depth + 1), path_values(right, data, values, depth + 1)
        if operator == "+" and a and b and len(a) * len(b) <= 16:
            return sorted(set(x + y for x in a for y in b))
        if operator in {"||", "??"}:
            return sorted(set(a + b))[:16]
    if node.type == "ternary_expression":
        condition = node.child_by_field_name("condition")
        if condition and condition.type == "binary_expression":
            a = path_values(condition.child_by_field_name("left"), data, values, depth + 1)
            b = path_values(condition.child_by_field_name("right"), data, values, depth + 1)
            if len(a) == len(b) == 1 and text(condition.child_by_field_name("operator"), data) in {"===", "=="}:
                return path_values(node.child_by_field_name("consequence" if a == b else "alternative"), data, values, depth + 1)
        return sorted(set(path_values(node.child_by_field_name("consequence"), data, values, depth + 1) +
                          path_values(node.child_by_field_name("alternative"), data, values, depth + 1)))[:16]
    return []


def object_values(node: Node | None, data: bytes) -> dict[str, str]:
    if not node or node.type != "object":
        return {}
    result = {text(n.child_by_field_name("key"), data).strip("'\""):
            literal(n.child_by_field_name("value"), data) or text(n.child_by_field_name("value"), data)
            for n in node.named_children if n.type == "pair"}
    for child in node.named_children:
        if child.type == "shorthand_property_identifier":
            result[text(child, data)] = "{dynamic}"
    return result


def url_pattern(node: Node, data: bytes) -> tuple[str | None, bool]:
    static = literal(node, data)
    if static is not None:
        return static, False
    if node.type == "template_string":
        parts = []
        for child in node.named_children:
            if child.type == "string_fragment":
                parts.append(text(child, data))
            elif child.type == "template_substitution":
                parts.append("{dynamic}")
            else:
                return None, True
        return "".join(parts), True
    return None, True


def extract(symbol: CodeSymbol, node: Node, module: CodeSymbol, file: CodeFile,
            data: bytes, result: Knowledge) -> None:
    imports = {item["local"]: item for item in module.metadata["imports"]}
    value = node.child_by_field_name("value")
    body = value if value and value.type in FUNCTIONS else node
    effects = list(owned(body))
    # Module/class effects must not include child declaration bodies.
    if symbol.kind in {"module", "class", "component_class", "interface", "type", "enum"}:
        child_ranges = [(s.metadata["start_byte"], s.metadata["end_byte"]) for s in result.symbols
                        if s.metadata.get("parent_id") == symbol.id]
        effects = [n for n in effects if not any(a <= n.start_byte < b for a, b in child_ranges)]
    seen: set[tuple[str, str, str]] = set()

    def edge(kind: str, name: str, evidence: str) -> None:
        if kind in {"REFERENCES", "RENDERS", "EXTENDS", "IMPLEMENTS"}:
            symbol.metadata.setdefault("reference_sites", []).append({"callee": name, "offset": item.start_byte})
        key = (kind, name, evidence)
        if key not in seen:
            result.relationships.append(Relationship(symbol.id, kind, name, evidence=evidence,
                metadata={"source_line_start": item.start_point.row + 1,
                          "source_line_end": item.end_point.row + 1,
                          "expression": text(item, data), "rule": "syntax_" + kind.lower()}))
            seen.add(key)
        local = name.split(".", 1)[0]
        if kind != "IMPORTS" and local in imports:
            key = ("IMPORTS", local, imports[local]["source"])
            if key not in seen:
                result.relationships.append(Relationship(symbol.id, "IMPORTS", local, evidence=key[2]))
                seen.add(key)

    symbol.metadata["local_bindings"] = []
    route_ranges = []
    for item in effects:
        if symbol.kind == "module" and any(start < item.start_byte < end for start, end in route_ranges):
            continue
        if item.type == "variable_declarator":
            symbol.metadata["local_bindings"].extend(bindings(item.child_by_field_name("name"), data))
        elif item.type == "catch_clause":
            symbol.metadata["local_bindings"].extend(bindings(item.child_by_field_name("parameter"), data))
        if item.type == "call_expression":
            function = item.child_by_field_name("function")
            callee = text(function, data)
            if function and function.type in {"identifier", "member_expression"}:
                edge("CALLS", callee, callee)
                symbol.metadata.setdefault("call_sites", []).append({"callee": callee, "offset": item.start_byte})
            args_node = item.child_by_field_name("arguments")
            args = args_node.named_children if args_node else []
            if not args:
                continue
            path, dynamic = url_pattern(args[0], data)
            fallback_paths = path_values(args[0], data, module.metadata.get("path_constants", {})) if path is None else []
            if len(fallback_paths) == 1:
                path, dynamic = fallback_paths[0], True
            options = object_values(args[1] if len(args) > 1 else None, data)
            method = options.get("method", "GET").upper()
            if len(args) > 1 and (args[1].type != "object" or any(n.type == "spread_element" for n in args[1].named_children)):
                method = None
            if len(args) > 1 and args[1].type == "object" and any(
                    n.type == "pair" and n.child_by_field_name("key") and
                    n.child_by_field_name("key").type == "computed_property_name" for n in args[1].named_children):
                method = None
            if method not in {m.upper() for m in HTTP_METHODS}:
                method = None
            imported = imports.get(callee.split(".", 1)[0], {})
            axios = imported.get("source") == "axios"
            client = module.metadata.get("http_clients", {}).get(callee.split(".", 1)[0])
            direct_http = callee == "fetch" or axios or bool(client)
            if (axios or client) and "." in callee:
                operation = callee.rsplit(".", 1)[-1]
                method = operation.upper() if operation in HTTP_METHODS else None
            if path is None:
                if direct_http:
                    edge("REQUESTS", f"{method or '?'} [dynamic URL]", text(args[0], data))
                else:
                    symbol.metadata.setdefault("unknown_request_sites", []).append({
                        "callee": callee, "offset": item.start_byte, "method": method,
                        "expression": text(args[0], data), "line_start": item.start_point.row + 1,
                        "line_end": item.end_point.row + 1})
            if path is not None and (path.startswith(("/", "http://", "https://")) or client):
                if client and not path.startswith(("http://", "https://")):
                    path = client["base_path"].rstrip("/") + "/" + path.lstrip("/")
                request = {"callee": callee, "path": path, "method": method,
                           "dynamic": dynamic, "expression": text(args[0], data),
                           "direct": direct_http, "offset": item.start_byte,
                           "options_supplied": len(args) > 1,
                           "line_start": item.start_point.row + 1, "line_end": item.end_point.row + 1,
                           "options_known": len(args) == 1 or (method is not None and args[1].type == "object")}
                if client:
                    request["client_configuration"] = client
                symbol.metadata.setdefault("requests", []).append(request)
                if direct_http:
                    edge("REQUESTS", f"{method or '?'} {path}", request["expression"])
            if callee == "fetch" and args[0].type == "template_string":
                substitutions = [n for n in args[0].named_children if n.type == "template_substitution"]
                if len(substitutions) == 1 and substitutions[0].named_children:
                    parameter = text(substitutions[0].named_children[0], data)
                    if symbol.metadata.get("parameter_bindings", [])[:1] == [parameter]:
                        fragments = text(args[0], data)[1:-1].split("${" + parameter + "}")
                        if len(fragments) == 2:
                            symbol.metadata["http_wrapper"] = {"prefix": fragments[0], "suffix": fragments[1],
                                                               "parameter": parameter, "method": method}
                            option_param = symbol.metadata.get("parameter_bindings", [])[1:2]
                            if option_param and len(args) > 1:
                                passed = args[1]
                                passthrough = text(passed, data) == option_param[0] or (
                                    passed.type == "object" and any(n.type == "spread_element" and
                                        text(n, data) == "..." + option_param[0] for n in passed.named_children) and
                                    "method" not in object_values(passed, data))
                                if passthrough:
                                    symbol.metadata["http_wrapper"]["options_parameter"] = option_param[0]
                                    symbol.metadata["http_wrapper"]["default_method"] = "GET"
                                    params = body.child_by_field_name("parameters")
                                    for param in params.named_children if params else []:
                                        if param.type == "assignment_pattern" and text(param.child_by_field_name("left"), data) == option_param[0]:
                                            default = param.child_by_field_name("right")
                                            defaults = object_values(default, data)
                                            default_method = defaults.get("method", "GET").upper()
                                            if not default or default.type != "object" or default_method not in {m.upper() for m in HTTP_METHODS} or any(n.type == "spread_element" for n in default.named_children):
                                                default_method = None
                                            symbol.metadata["http_wrapper"]["default_method"] = default_method
                                    # A mutated parameter is not a proven forwarding wrapper.
                                    if any(n.type in {"assignment_expression", "augmented_assignment_expression", "update_expression"} and
                                           text(n.child_by_field_name("left") or n.child_by_field_name("argument"), data).split(".")[0] == option_param[0] for n in effects):
                                        symbol.metadata.pop("http_wrapper", None)
            # Plain DB calls retain literal SQL; dynamic SQL stays evidence, not invented ASTs.
            if callee.rsplit(".", 1)[-1] in {"query", "execute", "raw"}:
                values = alternatives(args[0], data)
                for sql in values:
                    if re.match(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH|CALL)\b", sql, re.I):
                        symbol.metadata.setdefault("sql_sites", []).append({"sql": sql,
                            "line_start": item.start_point.row + 1, "line_end": item.end_point.row + 1,
                            "expression": text(args[0], data),
                            "conditional": len(values) > 1, "callee": callee})
                if not values:
                    symbol.metadata.setdefault("unresolved_sql", []).append({
                        "expression": text(args[0], data), "line_start": item.start_point.row + 1})
            # Small Express idiom support, enabled only by a literal express() binding.
            if symbol.kind == "module" and function and function.type == "member_expression":
                receiver = text(function.child_by_field_name("object"), data)
                operation = text(function.child_by_field_name("property"), data)
                express_factories = {i["local"] for i in module.metadata["imports"] if i["source"] == "express"}
                express_factories |= {name + ".Router" for name in express_factories}
                express_names = {s.name for s in result.symbols if s.kind in {"constant", "variable"}
                                 and s.metadata.get("parent_id") == module.id and
                                 (s.metadata.get("initializer_call") in express_factories or s.metadata.get("express_router"))}
                if receiver in express_names and operation in HTTP_METHODS and literal(args[0], data):
                    route_ranges.append((item.start_byte, item.end_byte))
                    route_name = f"{operation.upper()} {literal(args[0], data)}"
                    route = CodeSymbol(stable_id(file.id, "endpoint", route_name, str(item.start_byte)),
                                       route_name, "endpoint", file.path + "::" + route_name, file.id,
                                       item.start_point.row + 1, item.end_point.row + 1, text(item, data),
                                       {"parent_id": module.id, "start_byte": item.start_byte, "end_byte": item.end_byte,
                                        "scope": [0, len(data)], "parameters": [],
                                        "method": operation.upper(), "path": literal(args[0], data)})
                    result.symbols.append(route)
                    result.relationships.append(Relationship(module.id, "DEFINES", route.qualified_name,
                                                             route.id, "literal Express registration", "high"))
                    extract(route, item, module, file, data, result)
                    route.metadata["inline_handlers"] = []
                    for position, argument in enumerate(args[1:], 1):
                        site = {"rule": "express_handler_argument", "argument_position": position,
                                "method": operation.upper(), "path": literal(args[0], data),
                                "source_line_start": argument.start_point.row + 1,
                                "source_line_end": argument.end_point.row + 1,
                                "expression": text(argument, data)}
                        if argument.type in FUNCTIONS:
                            qualified = route.qualified_name + f".handler{position}"
                            handler = CodeSymbol(stable_id(route.id, "handler", str(position)),
                                f"handler{position}", "function", qualified, file.id,
                                argument.start_point.row + 1, argument.end_point.row + 1,
                                text(argument, data), {"parent_id": route.id,
                                "start_byte": argument.start_byte, "end_byte": argument.end_byte,
                                "scope": [argument.start_byte, argument.end_byte],
                                "parameters": [], "parameter_bindings": bindings(
                                    argument.child_by_field_name("parameters"), data),
                                "route_handler": True})
                            result.symbols.append(handler)
                            route.metadata["inline_handlers"].append(handler.id)
                            extract(handler, argument, module, file, data, result)
                            result.relationships.append(Relationship(route.id, "HANDLES", qualified,
                                handler.id, "literal inline Express handler", "high", site))
                        elif argument.type in {"identifier", "member_expression"}:
                            result.relationships.append(Relationship(route.id, "HANDLES", text(argument, data),
                                evidence="Express handler/middleware reference", metadata=site))
                            result.relationships.append(Relationship(route.id, "CALLS", text(argument, data),
                                evidence="route handler/middleware reference", metadata=site.copy()))
                        else:
                            result.relationships.append(Relationship(route.id, "HANDLES", text(argument, data),
                                evidence="unresolved Express handler expression", metadata=site))
        elif item.type in {"jsx_opening_element", "jsx_self_closing_element"}:
            name = text(item.child_by_field_name("name"), data)
            attributes = {}
            for attribute in item.named_children:
                if attribute.type != "jsx_attribute" or not attribute.named_children:
                    continue
                key = text(attribute.named_children[0], data)
                value_node = attribute.named_children[1] if len(attribute.named_children) > 1 else None
                attributes[key] = text(value_node, data) if value_node else "true"
                if key.startswith("on") and value_node and value_node.type == "jsx_expression":
                    child = value_node.named_children[0] if value_node.named_children else None
                    if child and child.type in {"identifier", "member_expression"}:
                        edge("REFERENCES", text(child, data), f"JSX event {key}")
            symbol.metadata.setdefault("jsx_elements", []).append({"name": name, "attributes": attributes})
            if name == "Route" and "path" in attributes:
                elements = [text(n.child_by_field_name("name"), data) for n in walk(item)
                            if n.type in {"jsx_self_closing_element", "jsx_opening_element"} and n != item]
                symbol.metadata.setdefault("routes", []).append({"path": attributes["path"].strip("'\""),
                                                                   "renders": [value for value in elements if value]})
            if name and (name[0].isupper() or "." in name):
                edge("RENDERS", name, "JSX element")
                symbol.metadata.setdefault("rendered_components", []).append(name)
        elif item.type == "jsx_text":
            value_text = " ".join(text(item, data).split())
            if value_text:
                symbol.metadata.setdefault("ui_text", []).append(value_text)
    symbol.metadata["hooks"] = sorted({text(n.child_by_field_name("function"), data) for n in effects
                                      if n.type == "call_expression" and
                                      re.match(r"^(?:React\.)?use[A-Z]", text(n.child_by_field_name("function"), data))})
    if symbol.kind in {"component", "component_class"}:
        params = symbol.metadata.get("parameter_bindings", [])
        prop_names = list(params)
        root_props = params[0] if params and symbol.metadata.get("parameters", [""])[0].split(":", 1)[0].strip() == params[0] else None
        for member in walk(node) if symbol.kind == "component_class" else effects:
            if member.type == "member_expression":
                receiver = text(member.child_by_field_name("object"), data)
                if receiver == "this.props" or (root_props and receiver == root_props):
                    prop_names.append(text(member.child_by_field_name("property"), data))
        symbol.metadata["props"] = list(dict.fromkeys(prop_names))
    symbol.metadata["source_terms"] = list(dict.fromkeys(text(n, data) for n in effects
                                                         if n.type in {"property_identifier", "shorthand_property_identifier"}))
    symbol.metadata["string_evidence"] = list(dict.fromkeys(value for n in effects
                                                            if n.type == "string" and (value := literal(n, data))
                                                            and len(value) <= 160))
    # Imported identifier references, excluding declarations and property keys.
    for item in effects:
        if item.type == "identifier" and text(item, data) in imports:
            if item.parent and item.parent.type not in {"import_specifier", "import_clause", "pair", "variable_declarator"}:
                edge("REFERENCES", text(item, data), "imported identifier syntax")
