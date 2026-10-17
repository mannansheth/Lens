CREATE INDEX IF NOT EXISTS chunks_semantic_concepts_gin
    ON chunks USING gin ((metadata->'semantic'->'concepts'));
INSERT INTO lens_schema_version(version) VALUES (4) ON CONFLICT DO NOTHING;
