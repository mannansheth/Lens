# Lens documentation

This documentation describes the implementation in the repository. The source catalog at [`reference/source-map.md`](reference/source-map.md) is the detailed file-by-file guide to modules and exported functions.

## Current guides

- [Architecture and design](current/architecture.md)
- [How the RAG pipeline works](current/rag-pipeline.md)
- [Technical deep dive with source excerpts](current/technical-deep-dive.md)
- [Setup](current/setup.md)
- [CLI commands](current/commands.md)
- [Repository indexing](current/indexing.md)
- [Embeddings and index storage](current/embeddings-and-storage.md)
- [Retrieval and answer generation](current/retrieval.md)
- [Answer providers](current/providers.md)
- [Client navigation](current/client-navigation.md)
- [Application database questions](current/database-questions.md)
- [Implementation status](current/mvp-status.md)

## Implementation phases

The phase pages summarize the capabilities present in the current code, not future commitments or historical test runs:

1. [Foundation and persistence](phases/01-foundation/README.md)
2. [Parsing and indexing](phases/02-parsing-indexing/README.md)
3. [Relationships and data flow](phases/03-relationships/README.md)
4. [Retrieval and evidence selection](phases/04-retrieval/README.md)
5. [Grounded answer generation](phases/05-grounded-answers/README.md)
6. [Client navigation and SQL](phases/06-client-navigation-sql/README.md)

## Evaluation records

Only current, reproducible evaluation records belong under [`results/current/`](results/current/README.md). Historical raw runs and duplicated experiment snapshots were removed because they embedded unrelated source and did not represent current behavior.
