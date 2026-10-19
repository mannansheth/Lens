# Lens architecture

Lens has two interaction surfaces over one indexing and answer core: a developer CLI and a local HTTP service for host applications. Both use the repository parser, PostgreSQL/pgvector store, lexical/vector retrieval, context construction, model adapters, and shared answer validation. The HTTP service fixes one configured repository root; clients cannot switch projects per request. The complete stage-by-stage explanation is in [How Lens understands a project and answers questions](rag-pipeline.md).

Its main stages are:

```mermaid
flowchart LR
    A[Repository and project documents] --> B[File discovery and filters]
    B --> C[Language parsers]
    C --> D[Symbols and static relationships]
    D --> E[Retrieval documents]
    E --> F[Local embeddings]
    D --> G[Project snapshot in PostgreSQL]
    E --> G
    F --> G
    Q[Question] --> H[Lexical and vector retrieval]
    G --> H
    H --> I[Ranking and evidence expansion]
    I --> J[Bounded context with source locations]
    J --> K[Local Ollama or configured Gemini provider]
    K --> L[Validation and cited response]
    M[Host application / SDK] --> N[Local HTTP API]
    N --> H
    N --> O[SQL generation and validation]
    O --> P[SQL text returned to host; not executed]
```

Parsing, relationship discovery, retrieval, and provider integration are separate modules connected by typed knowledge objects and store/provider interfaces. Index updates are persisted as a project-scoped snapshot. Lexical and vector search produce independent candidate sets, which are merged and ranked using query-aware signals. Selected evidence can be expanded through known static relationships for cross-layer questions, then bounded and passed to the answer provider with source identifiers. Structural and citation checks validate the response shape, but do not prove semantic entailment of every claim.

Lens does not execute the indexed application. Application-data SQL generation is separate from repository RAG: Lens reads configured schema metadata and returns one validated read-only SQL statement. The SDK/API never executes SQL or returns application rows. The developer CLI retains a separate explicit-confirmation path for bounded read-only execution. Host applications remain responsible for authentication, authorization, and any SQL execution policy. The deployment, endpoint contract, setup, reindexing, persistence, and security model are documented in [Local deployment and release contract](../release/deployment.md).

See the [source map](../reference/source-map.md) for each source file and its public functions.
