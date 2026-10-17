# Embeddings and index storage

Lens uses FastEmbed for local dense embeddings. The configured default is `sentence-transformers/all-MiniLM-L6-v2`, dimension 384, with CPU execution. Model weights are cached locally. Embedding vectors support semantic retrieval; lexical search is stored and queried separately, so answering does not depend on embeddings alone.

PostgreSQL with pgvector stores project metadata, files, entities, relationships, chunks, and vectors. Migrations under the source package's database directory create the schema and indexes. Projects are isolated by a stable identifier derived from the normalized root. Indexing validates vector count, dimensions, finite values, and nonzero norms before replacement.

Lens index storage is distinct from an application's own database. `DATABASE_URL` configures the Lens index. Application database credentials are only considered by explicit database operations and are not stored as indexed source content. Changing the answer provider does not alter stored embeddings. Changing the embedding model requires an index compatible with that model; use a deliberate reindex when model configuration changes.
