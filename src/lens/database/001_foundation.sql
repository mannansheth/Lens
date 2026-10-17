CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS lens_schema_version (
    version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS projects (
    id text PRIMARY KEY, root text NOT NULL UNIQUE,
    model text NOT NULL, dimension integer NOT NULL, indexed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS files (
    id text PRIMARY KEY, project_id text NOT NULL REFERENCES projects ON DELETE CASCADE,
    path text NOT NULL, language text NOT NULL, hash text NOT NULL, size bigint NOT NULL,
    UNIQUE(project_id, path)
);
CREATE TABLE IF NOT EXISTS entities (
    id text PRIMARY KEY, project_id text NOT NULL REFERENCES projects ON DELETE CASCADE,
    file_id text NOT NULL REFERENCES files ON DELETE CASCADE,
    name text NOT NULL, kind text NOT NULL, qualified_name text NOT NULL,
    line_start integer NOT NULL, line_end integer NOT NULL, source text NOT NULL,
    metadata jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS relationships (
    id bigserial PRIMARY KEY, project_id text NOT NULL REFERENCES projects ON DELETE CASCADE,
    source_id text NOT NULL REFERENCES entities ON DELETE CASCADE,
    target_id text REFERENCES entities ON DELETE CASCADE,
    kind text NOT NULL, target_name text NOT NULL, evidence text NOT NULL, confidence text NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id text PRIMARY KEY, project_id text NOT NULL REFERENCES projects ON DELETE CASCADE,
    entity_id text NOT NULL REFERENCES entities ON DELETE CASCADE,
    name text NOT NULL, kind text NOT NULL, file text NOT NULL, language text NOT NULL,
    text text NOT NULL, metadata jsonb NOT NULL, search_text text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED
);
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id text PRIMARY KEY REFERENCES chunks ON DELETE CASCADE,
    model text NOT NULL, dimension integer NOT NULL CHECK(dimension > 0),
    vector vector NOT NULL, CHECK(vector_dims(vector) = dimension)
);
CREATE INDEX IF NOT EXISTS files_project_idx ON files(project_id);
CREATE INDEX IF NOT EXISTS entities_name_idx ON entities(qualified_name);
CREATE INDEX IF NOT EXISTS entities_project_kind_idx ON entities(project_id, kind);
CREATE INDEX IF NOT EXISTS relationships_source_idx ON relationships(source_id, kind);
CREATE INDEX IF NOT EXISTS relationships_target_idx ON relationships(target_id, kind);
CREATE INDEX IF NOT EXISTS chunks_project_idx ON chunks(project_id);
CREATE INDEX IF NOT EXISTS chunks_search_idx ON chunks USING gin(search_vector);
CREATE INDEX IF NOT EXISTS embeddings_model_idx ON embeddings(model, dimension);
-- Default-model HNSW index. Other dimensions remain correct with exact scans.
CREATE INDEX IF NOT EXISTS embeddings_cosine_384_idx ON embeddings
USING hnsw ((vector::vector(384)) vector_cosine_ops) WHERE dimension = 384;
INSERT INTO lens_schema_version(version) VALUES (1) ON CONFLICT DO NOTHING;
