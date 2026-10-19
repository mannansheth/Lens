# Implementation status

This page records implemented behavior in the current code. It is not a production-readiness certification. Run the documented checks against the current checkout before relying on a test count or performance figure.

## Implemented

- Lens scans repositories while filtering common generated, dependency, and version-control directories.
- Parsers extract language-specific symbols and relationships for C#, Dart/Flutter, JavaScript/TypeScript, Python, HTML/CSS, Razor, SQL, and configuration; project documentation formats are parsed into heading-based sections with source locations. Dart parsing is structural and does not execute Flutter or infer runtime widget behavior.
- Indexing builds semantic documents and local embeddings, persists a project-scoped snapshot, and can reuse embeddings when the embedding input, model, and dimensions match.
- Retrieval combines lexical and vector search, applies explicit ranking signals, and can expand evidence through statically discovered relationships.
- Hybrid ranking fuses lexical and vector result ranks with reciprocal-rank fusion, rather than averaging incomparable raw score scales. The candidate-union/ranking behavior is corpus-neutral; it does not include repository-specific synonym or title boosts.
- `lens ask` supports grounded answers with source citations. Client mode includes structured navigation details when the indexed source establishes a destination or application action; dynamic values remain route templates.
- `lens sql` is a developer CLI workflow: it can use detected application database configuration, introspect supported database metadata, generate read-only SQL, validate it, request confirmation, and display query results in a table. A single read-only SELECT, CTE, or set operation such as UNION is accepted; multiple statements and write operations are rejected.
- `lens serve` provides a local JSON HTTP API for host applications. `/v1/ask` returns structured grounded responses; `/v1/sql` returns validated SQL and dialect but never executes it or returns application rows; `/v1/index` indexes the service's configured repository root. Dependency-free Python and Node.js HTTP clients and an OpenAPI specification are included.
- Docker Compose can build and run the engine, PostgreSQL/pgvector index, and optional Ollama service. Database, embedding-cache, and Ollama model volumes persist across container restarts. The project is mounted read-only into the engine.
- Ollama and Gemini answer-provider integrations are available through the shared provider interface. Embeddings and provider selection are separate concerns.

## Limits to keep in mind

Static parsing cannot execute the application or prove runtime behavior. Relationship resolution is conservative and may leave ambiguous references unresolved. Navigation extraction finds source-declared links and routes, but does not guarantee a complete menu hierarchy or resolve runtime route values. SQL generation depends on the schema and relationships Lens can inspect; generated SQL should be reviewed. API SQL is not executed by Lens, but a host that chooses to execute it must apply its own authorization and safety policy. The developer CLI's separate execution path requires operator confirmation. Citation and answer validation reduce unsupported output but do not prove every natural-language statement is entailed by its evidence.

The Compose deployment is a single-engine local MVP, not a multi-tenant service. The Python and Node.js SDKs are installable from this checkout but are not published to package registries. Native NuGet and other language-specific SDKs, a published registry image, signed release artifacts, built-in role management, and automatic source watching are not implemented. The HTTP API is the cross-language integration surface today.

## Verification

Use the commands in [validation](validation.md) to check the checkout. Do not infer answer accuracy from retrieval recall, passing parser tests, or schema availability alone. Store reproducible, sanitized current results under `docs/results/current/` and record the command, configuration, and dataset description without embedding another project's source or name.
