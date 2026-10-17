"""Explicit knowledge objects shared by parsing, indexing, and storage."""

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any


def stable_id(*parts: str) -> str:
    return sha256("\0".join(parts).encode()).hexdigest()


@dataclass
class CodeFile:
    id: str
    path: str
    language: str
    hash: str
    size: int


@dataclass
class CodeSymbol:
    id: str
    name: str
    kind: str
    qualified_name: str
    file_id: str
    line_start: int
    line_end: int
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DatabaseObject(CodeSymbol):
    """Database entities use the same location and identity fields as code symbols."""


@dataclass
class Relationship:
    source_id: str
    kind: str
    target_name: str
    target_id: str | None = None
    evidence: str = ""
    confidence: str = "unresolved"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentChunk:
    id: str
    entity_id: str
    name: str
    kind: str
    file: str
    language: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding_text: str | None = None


@dataclass
class Embedding:
    chunk_id: str
    model: str
    vector: list[float]

    @property
    def dimension(self) -> int:
        return len(self.vector)


@dataclass
class Knowledge:
    files: list[CodeFile] = field(default_factory=list)
    symbols: list[CodeSymbol] = field(default_factory=list)
    database_objects: list[DatabaseObject] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)

    @property
    def entities(self) -> list[CodeSymbol]:
        return [*self.symbols, *self.database_objects]


@dataclass
class SearchHit:
    chunk: DocumentChunk
    keyword_score: float = 0.0
    vector_score: float = 0.0
    hybrid_score: float = 0.0
    rank: int = 0
    ranking_signals: dict[str, Any] = field(default_factory=dict)
