"""JS/TS declarations, modules, exports, and React recognition from syntax trees."""

from pathlib import PurePosixPath
import re
from tree_sitter import Node
from lens.core.models import CodeFile, CodeSymbol, Knowledge, Relationship, stable_id
from lens.parsing.syntax import FUNCTIONS, bindings, literal, owned, parameters, text, tree, walk
from lens.parsing.frontend_evidence import extract, path_values

DECLARATIONS = {
    "function_declaration": "function", "generator_function_declaration": "function",
    "class_declaration": "class", "abstract_class_declaration": "class",
    "method_definition": "method", "interface_declaration": "interface",
    "type_alias_declaration": "type", "enum_declaration": "enum",
}


def parse(file: CodeFile, data: bytes) -> Knowledge:
    root = tree(file.language, data)
    result = Knowledge()
    if root.has_error:
        result.warnings.append(f"{file.path}: {file.language} grammar could not fully parse source; recoverable declarations only")
    nodes: dict[str, Node] = {}

    def add(node: Node, name: str, kind: str, parent: CodeSymbol | None) -> CodeSymbol:
        context = parent.qualified_name if parent and parent.kind != "module" else file.path + "::"
        qualified = context + ("." if parent and parent.kind != "module" else "") + name
        symbol = CodeSymbol(stable_id(file.id, kind, qualified, str(node.start_byte)), name, kind,
                            qualified, file.id, node.start_point.row + 1, node.end_point.row + 1,
                            text(node, data), {"parent_id": parent.id if parent else None,
                                               "parameters": parameters(node, data),
                                               "start_byte": node.start_byte, "end_byte": node.end_byte})
        scope = node.parent
        while scope and scope.type not in {"statement_block", "program", "class_body", "object"}:
            scope = scope.parent
        symbol.metadata["scope"] = [scope.start_byte, scope.end_byte] if scope else [0, len(data)]
        comments = []
        previous = node.prev_named_sibling
        while previous and previous.type == "comment":
            comments.insert(0, text(previous, data))
            previous = previous.prev_named_sibling
        symbol.metadata["documentation"] = "\n".join(comments)
        result.symbols.append(symbol)
        nodes[symbol.id] = node
        if parent:
            result.relationships.append(Relationship(parent.id, "DEFINES" if parent.kind == "module" else "CONTAINS",
                                                     symbol.qualified_name, symbol.id, "syntax declaration", "high"))
        return symbol

    module = add(root, "module", "module", None)
    module.qualified_name = file.path + "::module"
    module.metadata["imports"] = []
    module.metadata["exports"] = []
    require_shadowed = any(
        (n.type == "assignment_expression" and text(n.child_by_field_name("left"), data) == "require") or
        (n.type == "variable_declarator" and "require" in bindings(n.child_by_field_name("name"), data)) or
        (n.type in FUNCTIONS and "require" in bindings(n.child_by_field_name("parameters") or n.child_by_field_name("parameter"), data))
        for n in walk(root))

    for statement in root.named_children:
        if statement.type == "import_statement":
            source = literal(statement.child_by_field_name("source"), data)
            if source is None:
                continue
            clause = next((n for n in statement.named_children if n.type == "import_clause"), None)
            imported = []
            if clause:
                for child in clause.named_children:
                    if child.type == "identifier":
                        imported.append((text(child, data), "default"))
                    elif child.type == "namespace_import":
                        imported.append((text(child.named_children[-1], data), "*"))
                    elif child.type == "named_imports":
                        for spec in child.named_children:
                            name = text(spec.child_by_field_name("name"), data)
                            alias = text(spec.child_by_field_name("alias"), data) or name
                            imported.append((alias, name))
            for local, name in imported or [(source, "side_effect")]:
                module.metadata["imports"].append({"local": local, "imported": name, "source": source})
                result.relationships.append(Relationship(module.id, "IMPORTS", local, evidence=source))
        elif not require_shadowed and statement.type in {"lexical_declaration", "variable_declaration"}:
            for declaration in statement.named_children:
                if declaration.type != "variable_declarator":
                    continue
                value = declaration.child_by_field_name("value")
                if not value or value.type != "call_expression" or text(value.child_by_field_name("function"), data) != "require":
                    continue
                args = value.child_by_field_name("arguments")
                source = literal(args.named_children[0], data) if args and len(args.named_children) == 1 else None
                if source:
                    for local in bindings(declaration.child_by_field_name("name"), data):
                        module.metadata["imports"].append({"local": local, "imported": "*", "source": source,
                                                          "line": declaration.start_point.row + 1, "binding_byte": declaration.start_byte})

    def visit(node: Node, parent: CodeSymbol) -> None:
        current = parent
        kind = DECLARATIONS.get(node.type)
        name = text(node.child_by_field_name("name"), data)
        value = None
        wrapped_body = None
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            name_node = node.child_by_field_name("name")
            name = text(name_node, data) if name_node and name_node.type == "identifier" else ""
            if value and value.type in FUNCTIONS:
                kind = "function"
            elif value and value.type == "call_expression":
                factory = text(value.child_by_field_name("function"), data)
                react_wrappers = {i["local"] for i in module.metadata["imports"] if i["source"] == "react" and i["imported"] in {"memo", "forwardRef"}}
                react_wrappers |= {"React.memo", "React.forwardRef"}
                args = value.child_by_field_name("arguments")
                if factory in react_wrappers and args and args.named_children and args.named_children[0].type in FUNCTIONS:
                    wrapped_body = args.named_children[0]
                    kind = "function"
                elif parent.kind == "module" and name and not any(ancestor.type in FUNCTIONS for ancestor in ancestors(node)):
                    kind = "constant"
            elif parent.kind == "module" and name and not any(ancestor.type in FUNCTIONS for ancestor in ancestors(node)):
                kind = "constant" if node.parent and "const" in text(node.parent, data)[:8] else "variable"
        elif node.type == "pair" and node.child_by_field_name("value"):
            value = node.child_by_field_name("value")
            if value.type in FUNCTIONS:
                kind = "method"
                name = text(node.child_by_field_name("key"), data).strip("'\"")
        # Handle default anonymous function/arrow/class exports with a stable file-derived name.
        if node.type in FUNCTIONS | {"class"} and node.parent and node.parent.type == "export_statement" and not name:
            kind = "class" if node.type == "class" else "function"
            name = PurePosixPath(file.path).stem
        if kind and name:
            body_node = wrapped_body or value or node
            has_jsx = any(n.type in {"jsx_element", "jsx_self_closing_element"} for n in owned(body_node))
            if kind == "function" and name[0].isupper() and has_jsx:
                kind = "component"
            if kind == "class":
                heritage = next((n for n in node.named_children if n.type == "class_heritage"), None)
                if heritage:
                    heritage_text = text(heritage, data)
                    react_bases = {i["local"] for i in module.metadata["imports"] if i["source"] == "react" and
                                   i["imported"] in {"Component", "PureComponent"}}
                    if "React.Component" in heritage_text or "React.PureComponent" in heritage_text or any(base in heritage_text.split() for base in react_bases):
                        kind = "component_class"
            current = add(node, name, kind, parent)
            if value and value.type == "call_expression":
                current.metadata["initializer_call"] = text(value.child_by_field_name("function"), data)
                factory = value.child_by_field_name("function")
                if factory and factory.type == "member_expression" and text(factory.child_by_field_name("property"), data) == "Router":
                    receiver = factory.child_by_field_name("object")
                    args = receiver.child_by_field_name("arguments") if receiver and receiver.type == "call_expression" else None
                    if args and text(receiver.child_by_field_name("function"), data) == "require" and len(args.named_children) == 1 and literal(args.named_children[0], data) == "express":
                        current.metadata["express_router"] = True
            if body_node.type in FUNCTIONS:
                current.metadata["parameters"] = parameters(body_node, data)
            current.metadata["parameter_bindings"] = bindings(body_node.child_by_field_name("parameters") or
                                                              body_node.child_by_field_name("parameter"), data)
            if kind in {"class", "component_class", "interface"}:
                current.metadata["summary"] = text(node, data).split("{", 1)[0]
                for heritage in node.named_children:
                    if heritage.type in {"class_heritage", "extends_type_clause"}:
                        for clause in heritage.named_children:
                            relation = "IMPLEMENTS" if clause.type == "implements_clause" else "EXTENDS"
                            targets = clause.named_children if clause.type in {"extends_clause", "implements_clause"} else [clause]
                            for target in targets:
                                result.relationships.append(Relationship(current.id, relation, text(target, data),
                                                                         evidence="declared heritage"))
            export = node.parent
            if export and export.type in {"lexical_declaration", "variable_declaration"}:
                export = export.parent
            if export and export.type == "export_statement":
                exported = "default" if any(c.type == "default" for c in export.children) else name
                module.metadata["exports"].append({"exported": exported, "local": name, "entity_id": current.id})
        for child in node.named_children:
            visit(child, current)

    for child in root.named_children:
        visit(child, module)
    for statement in root.named_children:
        if statement.type != "export_statement":
            continue
        source = literal(statement.child_by_field_name("source"), data)
        clause = next((n for n in statement.named_children if n.type == "export_clause"), None)
        if clause:
            for spec in clause.named_children:
                name = text(spec.child_by_field_name("name"), data)
                alias = text(spec.child_by_field_name("alias"), data) or name
                module.metadata["exports"].append({"exported": alias, "local": name, "source": source})
        elif source:
            module.metadata["exports"].append({"exported": "*", "local": "*", "source": source})
        else:
            value = statement.child_by_field_name("value")
            if value and value.type == "identifier":
                module.metadata["exports"].append({"exported": "default", "local": text(value, data)})
    for export in module.metadata["exports"]:
        result.relationships.append(Relationship(module.id, "EXPORTS", export["exported"],
                                                 export.get("entity_id"), evidence=export.get("source") or export["local"],
                                                 confidence="high" if export.get("entity_id") else "unresolved"))
    # HTTP client configuration and Express mounts are syntax facts shared with
    # cross-file resolution; configuration values are reduced to URL paths only.
    from urllib.parse import urlsplit
    constants = {}
    module.metadata["http_clients"] = {}
    declarations = [n for statement in root.named_children if statement.type in {"lexical_declaration", "variable_declaration"}
                    for n in statement.named_children if n.type == "variable_declarator"]
    for declaration in declarations:
        name = text(declaration.child_by_field_name("name"), data)
        values = path_values(declaration.child_by_field_name("value"), data, constants)
        if values and all(v.startswith(("/", "http://", "https://")) for v in values):
            constants[name] = values
        value = declaration.child_by_field_name("value")
        if value and value.type == "call_expression":
            callee = text(value.child_by_field_name("function"), data)
            args = value.child_by_field_name("arguments")
            alias = callee.split(".")[0]
            if callee.endswith(".create") and any(i["local"] == alias and i["source"] == "axios" for i in module.metadata["imports"]) and args and args.named_children:
                option = args.named_children[0]
                base = next((p.child_by_field_name("value") for p in option.named_children if p.type == "pair" and text(p.child_by_field_name("key"), data).strip("'\"") == "baseURL"), None)
                bases = path_values(base, data, constants)
                if len(bases) == 1:
                    module.metadata["http_clients"][name] = {"base_path": urlsplit(bases[0]).path,
                        "rule": "axios_create_base_url", "source_line_start": declaration.start_point.row + 1,
                        "conditional": True, "environment_names": sorted(set(re.findall(r'process\.env\.(\w+)', module.source)))}
    module.metadata["path_constants"] = {name: [urlsplit(v).path if v.startswith(("http://", "https://")) else v for v in vals]
                                       for name, vals in constants.items()}
    module.metadata["router_mounts"] = []
    express = {i["local"] for i in module.metadata["imports"] if i["source"] == "express"}
    app_names = {text(n.child_by_field_name("name"), data) for n in walk(root) if n.type == "variable_declarator" and
                 (value := n.child_by_field_name("value")) and value.type == "call_expression" and text(value.child_by_field_name("function"), data) in express}
    for call in walk(root):
        if call.type != "call_expression":
            continue
        function = call.child_by_field_name("function")
        if not function or function.type != "member_expression" or text(function.child_by_field_name("object"), data) not in app_names or text(function.child_by_field_name("property"), data) != "use":
            continue
        args = call.child_by_field_name("arguments")
        if not args or len(args.named_children) < 2:
            continue
        environments = [constants]
        ancestor = call.parent
        while ancestor and ancestor.type != "program":
            if ancestor.type == "for_in_statement":
                left, right = ancestor.child_by_field_name("left"), ancestor.child_by_field_name("right")
                if left and right and right.type == "array":
                    names = bindings(left, data)
                    vals = [literal(n, data) for n in right.named_children]
                    if len(names) == 1 and all(v is not None for v in vals) and len(vals) <= 16:
                        environments = [{**constants, names[0]: [v]} for v in vals]
            ancestor = ancestor.parent
        for environment in environments:
            prefixes = path_values(args.named_children[0], data, environment)
            for argument in args.named_children[1:]:
                if argument.type == "call_expression" and text(argument.child_by_field_name("function"), data) == "require":
                    require_args = argument.child_by_field_name("arguments")
                    targets = path_values(require_args.named_children[0], data, environment) if require_args and require_args.named_children else []
                elif argument.type == "identifier":
                    targets = [i["source"] for i in module.metadata["imports"] if i["local"] == text(argument, data)]
                else:
                    targets = []
                for prefix in prefixes:
                    for target in targets:
                        module.metadata["router_mounts"].append({"prefix": prefix, "source": target,
                            "source_line_start": call.start_point.row + 1, "rule": "express_literal_router_mount"})
    for symbol in list(result.symbols):
        extract(symbol, nodes[symbol.id], module, file, data, result)
    module.metadata["summary"] = "\n".join([
        f"Module {file.path}", "Imports: " + ", ".join(i["local"] for i in module.metadata["imports"]),
        "Exports: " + ", ".join(e["exported"] for e in module.metadata["exports"]),
        "Defines: " + ", ".join(s.name for s in result.symbols if s.metadata.get("parent_id") == module.id),
    ])
    return result


def ancestors(node: Node):
    parent = node.parent
    while parent:
        yield parent
        parent = parent.parent
