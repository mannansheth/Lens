"""Finite SQL template alternatives proven by syntax, never runtime interpolation."""

from itertools import product
from tree_sitter import Node
from lens.parsing.syntax import literal, text


def alternatives(node: Node, data: bytes, limit: int = 8) -> list[str]:
    value = literal(node, data)
    if value is not None:
        return [value]
    if node.type == "ternary_expression":
        branches = [node.child_by_field_name(key) for key in ("consequence", "alternative")]
        if not all(branches):
            return []
        values = [alternatives(branch, data, limit) for branch in branches]
        return sorted(set(sum(values, []))) if all(values) and sum(map(len, values)) <= limit else []
    if node.type != "template_string":
        return []
    groups = []
    for child in node.named_children:
        if child.type == "string_fragment":
            groups.append([text(child, data)])
        elif child.type == "template_substitution" and len(child.named_children) == 1:
            values = alternatives(child.named_children[0], data, limit)
            if not values:
                return []
            groups.append(values)
        else:
            return []
    count = 1
    for group in groups:
        count *= len(group)
    return sorted(set("".join(parts) for parts in product(*groups))) if count <= limit else []
