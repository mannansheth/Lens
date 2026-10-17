"""Source-located static evidence and bounded traversal of relational snapshots."""

from dataclasses import asdict, dataclass, field
from time import perf_counter
import re
from lens.core.models import Knowledge, Relationship, stable_id

FLOW_KINDS = {"CALLS", "HANDLES", "REQUESTS", "READS", "WRITES", "CALLS_PROCEDURE", "RENDERS", "QUERIES"}


def annotate(knowledge: Knowledge) -> None:
    entities = {entity.id: entity for entity in knowledge.entities}
    files = {file.id: file.path for file in knowledge.files}
    for edge in knowledge.relationships:
        source = entities[edge.source_id]
        target = entities.get(edge.target_id)
        details = edge.metadata
        details.setdefault("rule", "existing_" + edge.kind.lower())
        details.setdefault("location_precision", "syntax" if "source_line_start" in details else "entity_range")
        details.setdefault("source_line_start", source.line_start)
        details.setdefault("source_line_end", source.line_end)
        details.update(source_file=files.get(source.file_id), source_name=source.qualified_name,
                       static_only=True, resolution="resolved" if target else "unresolved")
        if target:
            details.update(target_file=files.get(target.file_id), target_line_start=target.line_start,
                           target_line_end=target.line_end, target_name=target.qualified_name)
        if source.kind == "endpoint" and source.metadata.get("inline_handlers") and edge.kind != "HANDLES":
            details["aggregate_route_evidence"] = True


def layer(kind: str, file: str, language: str = "") -> str:
    if language == "documentation":
        return "documentation"
    if kind in {"table", "column", "view", "procedure", "select", "insert", "update", "delete"} or language == "mysql":
        return "database"
    if kind == "endpoint":
        return "api"
    parts = file.lower().replace("\\", "/").split("/")
    if "backend" in parts or file.endswith(".cs"):
        return "backend"
    if kind in {"component", "component_class"} or file.endswith((".jsx", ".tsx")) or "frontend" in parts:
        return "frontend"
    return "code"


@dataclass(frozen=True)
class EvidenceNode:
    id: str | None
    name: str
    kind: str
    file: str | None
    line_start: int | None
    line_end: int | None
    layer: str


@dataclass
class EvidencePath:
    nodes: list[EvidenceNode]
    relationships: list[dict]
    termination: str

    def describe(self) -> dict:
        return asdict(self)


@dataclass
class EvidenceGraph:
    nodes: dict[str, EvidenceNode]
    edges: list[Relationship]
    outgoing: dict[str, list[Relationship]] = field(init=False)
    incoming: dict[str, list[Relationship]] = field(init=False)

    def __post_init__(self):
        self.outgoing, self.incoming = {}, {}
        seen = set()
        for edge in sorted(self.edges, key=self.edge_key):
            key = self.edge_key(edge)
            if key in seen or edge.source_id not in self.nodes:
                continue
            seen.add(key)
            if edge.target_id is not None and edge.target_id not in self.nodes:
                raise ValueError("Evidence graph contains a target outside its snapshot")
            self.outgoing.setdefault(edge.source_id, []).append(edge)
            if edge.target_id:
                self.incoming.setdefault(edge.target_id, []).append(edge)

    @staticmethod
    def edge_key(edge):
        return (edge.source_id, edge.kind, edge.target_id or "", edge.target_name,
                edge.metadata.get("source_line_start", 0), edge.evidence)

    @classmethod
    def from_knowledge(cls, knowledge: Knowledge):
        files = {file.id: file for file in knowledge.files}
        return cls({entity.id: EvidenceNode(entity.id, entity.qualified_name, entity.kind,
                    files[entity.file_id].path, entity.line_start, entity.line_end,
                    layer(entity.kind, files[entity.file_id].path, files[entity.file_id].language))
                    for entity in knowledge.entities}, knowledge.relationships)

    @staticmethod
    def eligible(edge: Relationship) -> bool:
        if edge.metadata.get("aggregate_route_evidence"):
            return False
        return edge.kind in FLOW_KINDS or (edge.kind == "REFERENCES" and (
            edge.evidence.startswith("JSX event") or edge.evidence == "SQL column reference"))

    def reconstruct(self, start: str, max_depth: int = 8, max_paths: int = 64,
                    max_expansions: int = 2000, upstream: bool = False,
                    preferred_terms: frozenset[str] = frozenset()) -> dict:
        if not 1 <= max_depth <= 16 or not 1 <= max_paths <= 256 or not 1 <= max_expansions <= 20000:
            raise ValueError("Invalid evidence traversal bounds")
        if start not in self.nodes:
            raise ValueError("Starting entity is not in this graph")
        begun = perf_counter()
        paths, signatures = [], set()
        expansions = 0
        truncated = False

        def append(nodes, edges, reason):
            signature = tuple((r["edge_id"], r["direction"]) for r in edges)
            if edges and signature not in signatures and len(paths) < max_paths:
                signatures.add(signature)
                paths.append(EvidencePath(nodes, edges, reason))

        def visit(current, nodes, trail, visited, can_reverse):
            nonlocal expansions, truncated
            if len(paths) >= max_paths or expansions >= max_expansions:
                truncated = True
                return
            if len(trail) == max_depth:
                append(nodes, trail, "depth_limit")
                return
            edges = [(edge, "forward") for edge in self.outgoing.get(current, []) if self.eligible(edge)]
            if can_reverse:
                edges += [(edge, "reverse") for edge in self.incoming.get(current, [])
                          if edge.kind in {"RENDERS", "HANDLES", "CALLS"} or edge.evidence.startswith("JSX event")]
            # Prefer boundary-crossing and database hops before ordinary helpers/renders.
            order = {"REQUESTS": 0, "HANDLES": 1, "READS": 2, "WRITES": 2, "CALLS_PROCEDURE": 3,
                     "CALLS": 4, "REFERENCES": 5, "RENDERS": 6}
            def preference(edge):
                name = re.sub(r"([a-z])([A-Z])", r"\1 \2", edge.target_name)
                words = {word.removesuffix("s") for word in re.findall(r"[a-z]+", name.lower())}
                return len(words & preferred_terms)
            edges.sort(key=lambda item: (-preference(item[0]), order.get(item[0].kind, 7), self.edge_key(item[0]), item[1]))
            traversed = False
            cycle = False
            for edge, direction in edges:
                if len(paths) >= max_paths or expansions >= max_expansions:
                    truncated = True
                    break
                target_id = edge.target_id if direction == "forward" else edge.source_id
                if target_id in visited:
                    cycle = True
                    continue
                expansions += 1
                record = {**asdict(edge), "direction": direction,
                          "edge_id": stable_id(*(str(part) for part in self.edge_key(edge)))}
                if target_id is None:
                    target = EvidenceNode(None, edge.target_name, "unresolved", None, None, None, "unknown")
                    append([*nodes, target], [*trail, record], "unresolved")
                else:
                    visit(target_id, [*nodes, self.nodes[target_id]], [*trail, record], visited | {target_id},
                          can_reverse and direction == "reverse" and len(trail) < 1)
                traversed = True
            if not traversed:
                append(nodes, trail, "cycle_prevented" if cycle else "leaf")

        visit(start, [self.nodes[start]], [], {start}, upstream)
        return {"start": asdict(self.nodes[start]), "paths": [path.describe() for path in paths],
                "candidate_paths": len(paths), "edge_expansions": expansions, "truncated": truncated,
                "max_depth": max_depth, "static_only": True,
                "preferred_terms": sorted(preferred_terms),
                "reconstruction_seconds": perf_counter() - begun}
