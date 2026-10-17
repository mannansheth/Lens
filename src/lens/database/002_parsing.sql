ALTER TABLE projects ADD COLUMN IF NOT EXISTS pipeline_revision text NOT NULL DEFAULT 'phase1';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS statistics jsonb NOT NULL DEFAULT '{}';
INSERT INTO lens_schema_version(version) VALUES (2) ON CONFLICT DO NOTHING;
