"""C# syntax extraction with conservative, typed invocation evidence."""

from tree_sitter import Language, Node, Parser
import tree_sitter_c_sharp
from lens.core.models import CodeFile, CodeSymbol, Knowledge, Relationship, stable_id

KINDS = {
    "namespace_declaration": "namespace",
    "file_scoped_namespace_declaration": "namespace",
    "class_declaration": "class",
    "interface_declaration": "interface",
    "struct_declaration": "struct",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "method_declaration": "method",
    "constructor_declaration": "constructor",
    "property_declaration": "property",
    "field_declaration": "field",
    "enum_member_declaration": "enum_member",
}
CALLABLES = {"method", "constructor", "property"}


def descendants(node: Node):
    yield node
    for child in node.named_children:
        yield from descendants(child)


def parse(file: CodeFile, data: bytes) -> Knowledge:
    parser = Parser(Language(tree_sitter_c_sharp.language()))
    root = parser.parse(data).root_node
    result = Knowledge()
    if root.has_error:
        result.warnings.append(f"{file.path}: C# syntax errors; extracted recoverable declarations")

    def text(node: Node | None) -> str:
        return data[node.start_byte : node.end_byte].decode("utf-8") if node else ""

    scoped_namespace = next(
        (
            text(n.child_by_field_name("name"))
            for n in root.named_children
            if n.type == "file_scoped_namespace_declaration"
        ),
        "",
    )

    def visit(node: Node, parent: CodeSymbol | None, prefix: str) -> None:
        kind = KINDS.get(node.type)
        current = parent
        if kind:
            name_node = node.child_by_field_name("name")
            names = [text(name_node)] if name_node else []
            if kind == "field":
                names = [
                    text(n.child_by_field_name("name"))
                    for n in descendants(node)
                    if n.type == "variable_declarator"
                ]
            for name in filter(None, names):
                qualified = f"{prefix}.{name}" if prefix else name
                symbol = CodeSymbol(
                    stable_id(file.id, kind, qualified, str(node.start_byte)),
                    name,
                    kind,
                    qualified,
                    file.id,
                    node.start_point.row + 1,
                    node.end_point.row + 1,
                    text(node),
                )
                parameters = node.child_by_field_name("parameters")
                symbol.metadata["parameters"] = [
                    {
                        "name": text(p.child_by_field_name("name")),
                        "type": text(p.child_by_field_name("type")),
                    }
                    for p in (parameters.named_children if parameters else [])
                    if p.type == "parameter"
                ]
                symbol.metadata["declared_type"] = text(node.child_by_field_name("type"))
                if kind == "field":
                    declaration = next(
                        (n for n in node.named_children if n.type == "variable_declaration"), None
                    )
                    if declaration:
                        symbol.metadata["declared_type"] = text(
                            declaration.child_by_field_name("type")
                        )
                symbol.metadata["parent_id"] = parent.id if parent else None
                symbol.metadata["signature"] = text(node).split("{", 1)[0].strip()
                comments = []
                previous = node.prev_named_sibling
                while previous and previous.type == "comment":
                    comments.insert(0, text(previous))
                    previous = previous.prev_named_sibling
                symbol.metadata["documentation"] = "\n".join(comments)
                if kind in {"class", "interface", "struct", "record", "enum", "namespace"}:
                    # Summary excludes nested method bodies.
                    symbol.metadata["summary"] = symbol.metadata["signature"]
                result.symbols.append(symbol)
                if parent:
                    result.relationships.append(
                        Relationship(
                            parent.id,
                            "CONTAINS",
                            qualified,
                            symbol.id,
                            "syntax containment",
                            "high",
                        )
                    )
                base = next((n for n in node.named_children if n.type == "base_list"), None)
                if base:
                    for item in base.named_children:
                        result.relationships.append(
                            Relationship(
                                symbol.id, "INHERITS", text(item), evidence="declared base type"
                            )
                        )
                if kind in CALLABLES:
                    extract_calls(node, symbol, parent)
                current = symbol
            if current is not parent:
                prefix = current.qualified_name
        for child in node.named_children:
            visit(child, current, prefix)

    def extract_calls(node: Node, symbol: CodeSymbol, parent: CodeSymbol | None) -> None:
        bindings = {p["name"]: p["type"] for p in symbol.metadata["parameters"]}
        for item in descendants(node):
            if item.type == "variable_declaration":
                declared_type = text(item.child_by_field_name("type"))
                for var in item.named_children:
                    if var.type == "variable_declarator":
                        bindings[text(var.child_by_field_name("name"))] = declared_type
        symbol.metadata["bindings"] = bindings
        for item in descendants(node):
            if item.type == "invocation_expression":
                function = item.child_by_field_name("function")
                if not function:
                    continue
                receiver = text(function.child_by_field_name("expression"))
                name = text(function.child_by_field_name("name")) or text(function)
                name = name.split("<", 1)[0]
                relationship = Relationship(symbol.id, "CALLS", name, evidence=text(function))
                # Additional evidence is kept on the caller for later cross-file resolution.
                symbol.metadata.setdefault("calls", []).append(
                    {
                        "name": name,
                        "receiver": receiver,
                        "relationship_index": len(result.relationships),
                        "owner": parent.qualified_name if parent else "",
                    }
                )
                result.relationships.append(relationship)
                # Only known DB invocation forms with a literal first argument count.
                if name in {
                    "Query",
                    "QueryAsync",
                    "Execute",
                    "ExecuteAsync",
                    "ExecuteReader",
                    "CallProcedure",
                }:
                    args = item.child_by_field_name("arguments")
                    literals = (
                        [n for n in descendants(args) if n.type == "string_literal"] if args else []
                    )
                    if literals:
                        literal = text(literals[0]).strip('"')
                        invocation = text(item)
                        if "CommandType.StoredProcedure" in invocation or name == "CallProcedure":
                            result.relationships.append(
                                Relationship(
                                    symbol.id,
                                    "CALLS_PROCEDURE",
                                    literal,
                                    evidence="literal DB call",
                                )
                            )
                        elif (
                            literal.lstrip()
                            .upper()
                            .startswith(("SELECT", "INSERT", "UPDATE", "DELETE"))
                        ):
                            symbol.metadata.setdefault("sql_literals", []).append(literal)
            elif item.type == "identifier":
                symbol.metadata.setdefault("references", []).append(text(item))

    for child in root.named_children:
        prefix = scoped_namespace if child.type != "file_scoped_namespace_declaration" else ""
        visit(child, None, prefix)
    # Receiver fields may appear after methods; resolve after all declarations are collected.
    fields = [s for s in result.symbols if s.kind in {"field", "property"}]
    for symbol in result.symbols:
        owner_id = symbol.metadata.get("parent_id")
        bindings = symbol.metadata.get("bindings", {})
        for field in fields:
            if field.metadata.get("parent_id") == owner_id:
                bindings.setdefault(field.name, field.metadata["declared_type"])
        for call in symbol.metadata.get("calls", []):
            call["receiver_type"] = bindings.get(call["receiver"], "")
            call.pop("relationship_index", None)
    return result
