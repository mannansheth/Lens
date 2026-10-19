"""Transactions and queries intentionally remain visible SQL."""

from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Iterator, Protocol
import re
import numpy as np
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pgvector.psycopg import register_vector
from lens.repository.knowledge import lexical_text
from lens.core.models import DocumentChunk, Embedding, Knowledge, SearchHit, stable_id
from lens.core.logging import event
from lens.search.embeddings import validate_vectors

PIPELINE_REVISION = "whole-application-v8-boilerplate-filter"


class RetrievalStore(Protocol):
    def keyword_search(self, query: str, limit: int, project: str | None) -> list[SearchHit]: ...
    def vector_search(
        self, vector: list[float], model: str, limit: int, project: str | None
    ) -> list[SearchHit]: ...


class PostgresStore:
    def __init__(self, url: str):
        if not url:
            raise ValueError("DATABASE_URL is missing. Copy .env.example to .env and configure it.")
        self._url = url

    @contextmanager
    def connection(self, vectors: bool = False) -> Iterator[psycopg.Connection]:
        try:
            with psycopg.connect(self._url, connect_timeout=5, row_factory=dict_row) as conn:
                if vectors:
                    register_vector(conn)
                yield conn
        except psycopg.Error as error:
            event("database_failure", error_type=type(error).__name__)
            # Driver messages can include connection credentials; do not echo them.
            raise RuntimeError(
                "PostgreSQL operation failed. Check DATABASE_URL, the local server, "
                "and run 'lens db-init' with pgvector installed."
            ) from error

    def initialize(self) -> None:
        with self.connection() as conn:
            for migration in ("001_foundation.sql", "002_parsing.sql", "003_cross_layer.sql", "004_semantics.sql"):
                conn.execute(files("lens.database").joinpath(migration).read_text())
        event("database_initialized", schema_version=4)

    def cached_embeddings(self, project: str, model: str, dimension: int) -> dict[str, list[float]]:
        with self.connection(vectors=True) as conn:
            rows = conn.execute(
                "SELECT c.metadata->>'embedding_hash' AS hash,e.vector FROM chunks c "
                "JOIN embeddings e ON e.chunk_id=c.id WHERE c.project_id=%s "
                "AND e.model=%s AND e.dimension=%s AND c.metadata ? 'embedding_hash'",
                (project, model, dimension),
            ).fetchall()
        return {row["hash"]: row["vector"].tolist() for row in rows}

    def snapshot(self, project: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = %s", (project,)).fetchone()
            hashes = conn.execute(
                "SELECT path, hash FROM files WHERE project_id = %s", (project,)
            ).fetchall()
        return {"project": row, "hashes": {r["path"]: r["hash"] for r in hashes}}

    def replace(
        self,
        root: Path,
        knowledge: Knowledge,
        chunks: list[DocumentChunk],
        embeddings: list[Embedding],
        model: str,
        dimension: int,
    ) -> None:
        if len(embeddings) != len(chunks) or {e.chunk_id for e in embeddings} != {c.id for c in chunks}:
            raise ValueError("Every chunk needs exactly one embedding")
        if any(e.model != model or e.dimension != dimension for e in embeddings):
            raise ValueError("Embedding model/dimension differs from project metadata")
        validate_vectors([e.vector for e in embeddings], len(chunks), dimension)
        project = stable_id(str(root))
        with self.connection(vectors=True) as conn:
            # Serialize concurrent replacement of the same project.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (project,))
            conn.execute("DELETE FROM projects WHERE id = %s", (project,))
            conn.execute(
                "INSERT INTO projects(id, root, model, dimension,pipeline_revision,statistics) VALUES (%s,%s,%s,%s,%s,%s)",
                (project, str(root), model, dimension, PIPELINE_REVISION, Jsonb(knowledge.statistics)),
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO files VALUES (%s,%s,%s,%s,%s,%s)",
                    [(f.id, project, f.path, f.language, f.hash, f.size) for f in knowledge.files],
                )
                cursor.executemany(
                    "INSERT INTO entities VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            e.id,
                            project,
                            e.file_id,
                            e.name,
                            e.kind,
                            e.qualified_name,
                            e.line_start,
                            e.line_end,
                            e.source,
                            Jsonb(e.metadata),
                        )
                        for e in knowledge.entities
                    ],
                )
                cursor.executemany(
                    "INSERT INTO relationships(project_id,source_id,target_id,kind,target_name,evidence,confidence,metadata) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            project,
                            r.source_id,
                            r.target_id,
                            r.kind,
                            r.target_name,
                            r.evidence,
                            r.confidence,
                            Jsonb(r.metadata),
                        )
                        for r in knowledge.relationships
                    ],
                )
                cursor.executemany(
                    "INSERT INTO chunks(id,project_id,entity_id,name,kind,file,language,text,metadata,search_text) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            c.id,
                            project,
                            c.entity_id,
                            c.name,
                            c.kind,
                            c.file,
                            c.language,
                            c.text,
                            Jsonb(c.metadata),
                            lexical_text(c.text),
                        )
                        for c in chunks
                    ],
                )
                cursor.executemany(
                    "INSERT INTO embeddings VALUES (%s,%s,%s,%s)",
                    [(e.chunk_id, e.model, e.dimension, np.array(e.vector)) for e in embeddings],
                )
        event("snapshot_saved", files=len(knowledge.files), chunks=len(chunks))

    def inspect(self, project: str | None = None, relations: bool = False) -> dict:
        with self.connection() as conn:
            counts = {}
            for table in ("files", "entities", "relationships", "chunks"):
                # Table is an internal constant, never user input.
                counts[table] = conn.execute(
                    f"SELECT count(*) AS n FROM {table} "
                    "WHERE (%s::text IS NULL OR project_id = %s)",
                    (project, project),
                ).fetchone()["n"]
            kinds = conn.execute(
                "SELECT kind, count(*) AS count FROM entities "
                "WHERE (%s::text IS NULL OR project_id = %s) GROUP BY kind ORDER BY kind",
                (project, project),
            ).fetchall()
            projects = conn.execute(
                "SELECT root,model,dimension,indexed_at,pipeline_revision,statistics FROM projects "
                "WHERE (%s::text IS NULL OR id = %s)",
                (project, project),
            ).fetchall()
            languages = conn.execute(
                "SELECT language,count(*) AS count FROM files "
                "WHERE (%s::text IS NULL OR project_id=%s) GROUP BY language ORDER BY language", (project, project)
            ).fetchall()
            unresolved = conn.execute(
                "SELECT kind,count(*) AS count FROM relationships WHERE target_id IS NULL "
                "AND (%s::text IS NULL OR project_id=%s) GROUP BY kind ORDER BY kind", (project, project)
            ).fetchall()
            items = conn.execute(
                "SELECT id,qualified_name,kind,line_start FROM entities "
                "WHERE (%s::text IS NULL OR project_id = %s) ORDER BY qualified_name",
                (project, project),
            ).fetchall()
            edges = (
                conn.execute(
                    "SELECT s.qualified_name AS source,r.kind,r.target_name,r.target_id,r.confidence,r.evidence,r.metadata "
                    "FROM relationships r JOIN entities s ON s.id=r.source_id "
                    "WHERE (%s::text IS NULL OR r.project_id = %s) ORDER BY source,r.kind,r.target_name",
                    (project, project),
                ).fetchall()
                if relations
                else []
            )
        return {
            "counts": counts,
            "kinds": kinds,
            "projects": projects,
            "items": items,
            "relationships": edges,
            "languages": languages,
            "unresolved_relationships": unresolved,
        }

    def check_model(self, model: str, dimension: int, project: str | None) -> None:
        if not project:
            raise ValueError("Retrieval requires exactly one indexed project")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT model,dimension FROM projects WHERE (%s::text IS NULL OR id = %s)",
                (project, project),
            ).fetchall()
        if not rows:
            raise ValueError("No indexed project found. Run 'lens index <directory>' first.")
        if any(r["model"] != model or r["dimension"] != dimension for r in rows):
            raise ValueError(
                "Embedding model differs from indexed data. Re-index or select a matching --project."
            )

    def evidence_graph(self, projects: list[str]):
        if len(projects) != 1:
            raise ValueError("Evidence retrieval requires exactly one project")
        from lens.search.evidence import EvidenceGraph, EvidenceNode, layer
        from lens.core.models import Relationship
        with self.connection() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            nodes = conn.execute("SELECT e.*,f.path,f.language FROM entities e JOIN files f ON f.id=e.file_id "
                                 "WHERE e.project_id=ANY(%s) ORDER BY e.id", (projects,)).fetchall()
            edges = conn.execute("SELECT source_id,kind,target_name,target_id,evidence,confidence,metadata "
                                 "FROM relationships WHERE project_id=ANY(%s) ORDER BY id", (projects,)).fetchall()
        return EvidenceGraph({row["id"]: EvidenceNode(row["id"], row["qualified_name"], row["kind"],
            row["path"], row["line_start"], row["line_end"], layer(row["kind"], row["path"], row["language"]))
            for row in nodes}, [Relationship(**row) for row in edges])

    def definition_hits(self, name: str, project: str) -> list[SearchHit]:
        """All exact snapshot declarations: no candidate limit can hide ambiguity."""
        if not project:
            raise ValueError("Definition lookup requires one selected project")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT c.*,0.0 AS score FROM chunks c JOIN entities e ON e.id=c.entity_id "
                "WHERE c.project_id=%s AND e.kind NOT IN ('module','namespace','integration') "
                "AND (lower(e.name)=lower(%s) OR lower(e.qualified_name)=lower(%s)) ORDER BY c.id",
                (project, name, name)).fetchall()
        return [self._hit(row, "keyword") for row in rows]

    def project_ids_for_entities(self, entities: list[str]) -> list[str]:
        with self.connection() as conn:
            return [row["project_id"] for row in conn.execute(
                "SELECT DISTINCT project_id FROM entities WHERE id=ANY(%s) ORDER BY project_id", (entities,))]

    def chunks_for_entities(self, entities: list[str], projects: list[str]) -> list[SearchHit]:
        if len(projects) != 1:
            raise ValueError("Evidence retrieval requires exactly one project")
        with self.connection() as conn:
            rows = conn.execute("SELECT *,0.0 AS score FROM chunks WHERE entity_id=ANY(%s) "
                                "AND project_id=ANY(%s) ORDER BY id", (entities, projects)).fetchall()
        return [self._hit(row, "keyword") for row in rows]

    @staticmethod
    def _hit(row: dict, branch: str) -> SearchHit:
        chunk = DocumentChunk(
            row["id"],
            row["entity_id"],
            row["name"],
            row["kind"],
            row["file"],
            row["language"],
            row["text"],
            row["metadata"],
        )
        hit = SearchHit(chunk)
        setattr(hit, branch + "_score", float(row["score"]))
        return hit

    def keyword_search(self, query: str, limit: int, project: str | None = None) -> list[SearchHit]:
        if not project:
            raise ValueError("Retrieval requires exactly one indexed project")
        tokens = sorted(set(re.findall(r"[\w]+", lexical_text(query).lower())))
        if not tokens:
            return []
        tsquery = " | ".join(tokens)
        # Only technical-looking original tokens qualify for exact-name boosting.
        # A lowercase natural-language word like "invoice" must not boost class Invoice.
        identifiers = [
            token.lower()
            for token in re.findall(r"[\w.]+", query)
            if "_" in token or re.search(r"[a-z][A-Z]", token) or "." in token
        ]
        with self.connection() as conn:
            rows = conn.execute(
                "WITH q AS (SELECT to_tsquery('simple', %s) AS terms) "
                "SELECT c.*, ts_rank_cd(c.search_vector,q.terms,CASE WHEN p.statistics->>'retrieval_profile'='frontend' THEN 32 ELSE 0 END) + "
                "CASE WHEN lower(c.name) = ANY(%s) OR "
                "lower(COALESCE(c.metadata->>'entity_name',regexp_replace(c.name,'^.*[.]',''))) = ANY(%s) THEN 1.0 ELSE 0 END AS score "
                "FROM chunks c JOIN projects p ON p.id=c.project_id CROSS JOIN q WHERE c.search_vector @@ q.terms "
                "AND c.kind <> 'integration' AND (%s::text IS NULL OR c.project_id = %s) ORDER BY score DESC,c.id LIMIT %s",
                (tsquery, identifiers, identifiers, project, project, limit),
            ).fetchall()
        return [self._hit(row, "keyword") for row in rows]

    def vector_search(
        self, vector: list[float], model: str, limit: int, project: str | None = None,
        include_integrations: bool = False,
    ) -> list[SearchHit]:
        if not project:
            raise ValueError("Retrieval requires exactly one indexed project")
        with self.connection(vectors=True) as conn:
            # Exact project-scoped cosine ordering: adding integration documents must not
            # change legacy candidates through an approximate global HNSW search budget.
            conn.execute("SET LOCAL enable_indexscan = off")
            # The 384 cast matches the partial HNSW expression index. Other model
            # dimensions use an exact cosine scan, without interpolating dimensions.
            distance = "(e.vector::vector(384)) <=> %s" if len(vector) == 384 else "e.vector <=> %s"
            rows = conn.execute(
                f"SELECT c.*, 1 - ({distance}) AS score FROM embeddings e "
                "JOIN chunks c ON c.id=e.chunk_id WHERE e.model = %s AND e.dimension = %s "
                "AND (%s OR c.kind <> 'integration') AND (%s::text IS NULL OR c.project_id = %s) "
                f"ORDER BY {distance},c.id LIMIT %s",
                (np.array(vector), model, len(vector), include_integrations, project, project, np.array(vector), limit),
            ).fetchall()
        return [self._hit(row, "vector") for row in rows]

    def projects(self) -> list[dict]:
        with self.connection() as conn:
            return conn.execute("SELECT id,root,model,dimension,pipeline_revision,indexed_at FROM projects ORDER BY root").fetchall()

    def remove_project(self, project: str) -> bool:
        """Remove one project and its dependent index rows transactionally."""
        with self.connection() as conn:
            result = conn.execute("DELETE FROM projects WHERE id = %s", (project,))
            return result.rowcount == 1

    def semantic_candidates(self, concepts: list[str], project: str, limit: int = 60, integrations: bool = False) -> list[SearchHit]:
        if not project or not 1 <= limit <= 100:
            raise ValueError("Semantic candidates require one project and bounded limit")
        with self.connection() as conn:
            rows = conn.execute("SELECT *,0.0 AS score FROM chunks WHERE project_id=%s AND "
                "(metadata->'semantic'->'concepts' ?| %s OR (%s AND (kind='integration' OR "
                "jsonb_array_length(COALESCE(metadata->'semantic'->'integrations','[]'::jsonb))>0))) "
                "ORDER BY (jsonb_array_length(COALESCE(metadata->'semantic'->'hosts','[]'::jsonb))>0) DESC, "
                "CASE WHEN kind IN ('function','method','constant') THEN 0 WHEN kind='integration' THEN 1 ELSE 2 END,id LIMIT %s",
                (project, concepts, integrations or not bool(concepts), limit)).fetchall()
        return [self._hit(row, "keyword") for row in rows]
