"""Concise deterministic embedding summaries; full source stays in entities/chunks."""

from lens.core.models import CodeSymbol


def summary_lines(entity: CodeSymbol, path: str, relationships: list[str], paths: list[str]) -> list[str]:
    metadata = entity.metadata
    lines = [f"{entity.kind} {entity.name}; file {path}"]
    if entity.kind == "endpoint":
        lines.append(f"HTTP endpoint {metadata['method']} {metadata['path']}")
    for route in metadata.get("incoming_routes", []):
        lines.append(f"Rendered at UI route {route['path']}; declared by {route['owner']}")
    if metadata.get("parameters"):
        lines.append("Parameters/props: " + ", ".join(metadata["parameters"]))
    for route in metadata.get("routes", []):
        lines.append(f"UI route {route['path']} renders " + ", ".join(route["renders"]))
    for field, label in (("props", "Props"), ("hooks", "React hooks"),
                         ("rendered_components", "Renders components"), ("ui_text", "UI text")):
        values = list(dict.fromkeys(metadata.get(field, [])))
        if values:
            lines.append(label + ": " + "; ".join(values[:16] if field == "rendered_components" else values[:8]))
    for request in metadata.get("requests", [])[:5]:
        lines.append(f"HTTP request: {request.get('method') or '?'} {request.get('resolved_path', request['path'])}")
    calls = list(dict.fromkeys(site["callee"] for site in metadata.get("call_sites", [])))
    if calls:
        lines.append("Calls: " + ", ".join(calls[:8]))
    for field, label in (("source_terms", "Source terms"), ("string_evidence", "Literal evidence")):
        values = metadata.get(field, [])
        if values:
            lines.append(label + ": " + "; ".join(values[:20]))
    if metadata.get("documentation"):
        lines.append(metadata["documentation"])
    # Include a bounded number of complete evidence lines, never arbitrary code slices.
    for relation in relationships[:8]:
        lines.append(relation.replace(entity.qualified_name, entity.name))
    lines.extend(paths[:2])
    return lines


def embedding_summary(entity: CodeSymbol, path: str, relationships: list[str], paths: list[str]) -> str:
    lines = summary_lines(entity, path, relationships, paths)
    source = entity.metadata.get("summary", entity.source)
    if len(source) <= 700:
        lines.append(source)
    selected = []
    size = 0
    for line in lines:
        if len(line) > 900:
            continue
        if size + len(line) + 1 <= 1000:
            selected.append(line)
            size += len(line) + 1
    return "\n".join(selected)


def retrieval_document(entity: CodeSymbol, path: str, relationships: list[str], paths: list[str]) -> str:
    """Keep source for retrieval; the independent embedding summary stays compact."""
    source = entity.source
    lines = [f"{entity.kind}: {entity.qualified_name}", *summary_lines(entity, path, relationships, paths)]
    lines.extend(relationships)
    lines.extend(paths)
    if source:
        lines.append("Source:\n" + source)
    return "\n".join(dict.fromkeys(lines))
