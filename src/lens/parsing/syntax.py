"""Shared tree traversal, literal extraction, and grammar selection."""

from collections.abc import Iterator
from functools import lru_cache
from tree_sitter import Language, Node, Parser

FUNCTIONS = {"function_declaration", "generator_function_declaration", "function_expression",
             "generator_function", "arrow_function", "method_definition"}


@lru_cache(maxsize=8)
def language(name: str) -> Language:
    if name in {"javascript", "jsx"}:
        import tree_sitter_javascript
        return Language(tree_sitter_javascript.language())
    if name in {"typescript", "tsx"}:
        import tree_sitter_typescript
        factory = tree_sitter_typescript.language_tsx if name == "tsx" else tree_sitter_typescript.language_typescript
        return Language(factory())
    if name == "html":
        import tree_sitter_html
        return Language(tree_sitter_html.language())
    if name == "css":
        import tree_sitter_css
        return Language(tree_sitter_css.language())
    if name == "dart":
        import tree_sitter_dart
        return Language(tree_sitter_dart.language())
    raise ValueError(f"No syntax grammar for {name}")


def tree(name: str, data: bytes) -> Node:
    root = Parser(language(name)).parse(data).root_node
    if name == "jsx" and root.has_error:
        # TSX's grammar handles valid SVG JSX keyword attributes that the JS
        # grammar can reject. Reuse unchanged bytes and exact source positions.
        alternative = Parser(language("tsx")).parse(data).root_node
        if not alternative.has_error:
            return alternative
    return root


def text(node: Node | None, data: bytes) -> str:
    return data[node.start_byte:node.end_byte].decode("utf-8") if node else ""


def walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from walk(child)


def owned(node: Node) -> Iterator[Node]:
    """Include anonymous callbacks, but do not attribute named nested functions to callers."""
    yield node
    for child in node.named_children:
        if child.type in {"function_declaration", "generator_function_declaration", "method_definition"}:
            continue
        if child.type == "variable_declarator":
            value = child.child_by_field_name("value")
            if value and value.type in FUNCTIONS:
                continue
        yield from owned(child)


def literal(node: Node | None, data: bytes) -> str | None:
    """Return only completely static strings, never interpolate or evaluate source."""
    if not node or node.type not in {"string", "template_string", "string_literal"}:
        return None
    if any(n.type == "template_substitution" for n in node.named_children):
        return None
    value = text(node, data)
    if "\\" in value:
        # Escaped identifiers/URLs are left uncertain instead of decoded incorrectly.
        return None
    return value[1:-1]


def parameters(node: Node, data: bytes) -> list[str]:
    params = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
    if not params:
        return []
    return [text(n, data) for n in params.named_children] if params.type == "formal_parameters" else [text(params, data)]


def bindings(node: Node | None, data: bytes) -> list[str]:
    """Binding names from destructuring/parameters, excluding object property keys and types."""
    if not node:
        return []
    if node.type in {"identifier", "shorthand_property_identifier_pattern"}:
        return [text(node, data)]
    if node.type in {"required_parameter", "optional_parameter"}:
        return bindings(node.child_by_field_name("pattern"), data)
    if node.type in {"assignment_pattern", "object_assignment_pattern"}:
        return bindings(node.child_by_field_name("left"), data)
    if node.type == "pair_pattern":
        return bindings(node.child_by_field_name("value"), data)
    if node.type in {"formal_parameters", "object_pattern", "array_pattern", "rest_pattern"}:
        return [name for child in node.named_children for name in bindings(child, data)]
    return []
