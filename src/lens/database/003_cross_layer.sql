ALTER TABLE relationships ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}';
INSERT INTO lens_schema_version(version) VALUES (3) ON CONFLICT DO NOTHING;
