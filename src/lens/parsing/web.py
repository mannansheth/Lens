"""Basic HTML structures and lightweight CSS rules, without browser execution."""

from lens.core.models import CodeFile, CodeSymbol, Knowledge, Relationship, stable_id
from lens.parsing.syntax import text, tree, walk

HTML_TAGS = {"form", "h1", "h2", "h3", "h4", "button", "a", "input", "textarea", "select", "script", "title", "nav"}


def parse(file: CodeFile, data: bytes) -> Knowledge:
    root = tree(file.language, data)
    knowledge = Knowledge()
    if root.has_error:
        knowledge.warnings.append(f"{file.path}: {file.language} grammar could not fully parse source; recoverable structures only")
    occurrences: dict[str, int] = {}

    def add(node, name: str, kind: str, metadata: dict) -> CodeSymbol:
        occurrences[name] = occurrences.get(name, 0) + 1
        suffix = f"[{occurrences[name]}]" if occurrences[name] > 1 else ""
        entity = CodeSymbol(stable_id(file.id, kind, str(node.start_byte)), name, kind,
                            f"{file.path}::{name}{suffix}", file.id,
                            node.start_point.row + 1, node.end_point.row + 1, text(node, data), metadata)
        knowledge.symbols.append(entity)
        return entity

    if file.language == "css":
        for node in walk(root):
            if node.type == "rule_set":
                selectors = next((n for n in node.named_children if n.type == "selectors"), None)
                add(node, text(selectors, data), "css_rule", {"selectors": text(selectors, data)})
        if not knowledge.symbols:
            add(root, "stylesheet", "stylesheet", {})
        return knowledge
    document = add(root, "document", "html_document", {})
    meaningful = []
    for node in walk(root):
        if node.type not in {"element", "script_element", "style_element"}:
            continue
        opening = next((n for n in node.named_children if n.type in {"start_tag", "self_closing_tag"}), None)
        if not opening:
            continue
        tag_node = next((n for n in opening.named_children if n.type == "tag_name"), None)
        tag = text(tag_node, data)
        attributes = {}
        for attribute in opening.named_children:
            if attribute.type == "attribute" and attribute.named_children:
                key = text(attribute.named_children[0], data)
                value = text(attribute.named_children[-1], data).strip("'\"") if len(attribute.named_children) > 1 else "true"
                attributes[key] = value
        visible = " ".join(text(n, data).strip() for n in walk(node) if n.type == "text")
        if tag in HTML_TAGS or "id" in attributes:
            name = tag + ("#" + attributes["id"] if "id" in attributes else "")
            entity = add(node, name, "html_element", {"tag": tag, "attributes": attributes, "text": visible,
                                                      "summary": f"{tag} {attributes}\n{visible}"})
            knowledge.relationships.append(Relationship(document.id, "CONTAINS", entity.qualified_name,
                                                         entity.id, "HTML structure", "high"))
            meaningful.append(entity.metadata["summary"])
    document.metadata["summary"] = f"HTML document {file.path}\n" + "\n".join(meaningful)
    return knowledge
