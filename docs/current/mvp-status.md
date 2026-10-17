# Implementation status

This page records implemented behavior in the current code. It is not a production-readiness certification. Run the documented checks against the current checkout before relying on a test count or performance figure.

## Implemented

- Lens scans repositories while filtering common generated, dependency, and version-control directories.
- Parsers extract language-specific symbols and relationships; project documentation formats are parsed into heading-based sections with source locations.
- Indexing builds semantic documents and local embeddings, persists a project-scoped snapshot, and can reuse embeddings when the embedding input, model, and dimensions match.
- Retrieval combines lexical and vector search, applies explicit ranking signals, and can expand evidence through statically discovered relationships.
- `lens ask` supports grounded answers with source citations. Client mode includes structured navigation details when the indexed source establishes a destination or application action; dynamic values remain route templates.
- `lens sql` can use detected application database configuration, introspect supported database metadata, generate read-only SQL, validate it, request confirmation, and display query results in a table. A single read-only SELECT, CTE, or set operation such as UNION is accepted; multiple statements and write operations are rejected.
- Ollama and Gemini answer-provider integrations are available through the shared provider interface. Embeddings and provider selection are separate concerns.

## Limits to keep in mind

Static parsing cannot execute the application or prove runtime behavior. Relationship resolution is conservative and may leave ambiguous references unresolved. Navigation extraction finds source-declared links and routes, but does not guarantee a complete menu hierarchy or resolve runtime route values. SQL generation depends on the schema and relationships Lens can inspect; generated SQL should be reviewed before confirmation. Citation and answer validation reduce unsupported output but do not prove every natural-language statement is entailed by its evidence.

## Verification

Use the commands in [validation](validation.md) to check the checkout. Do not infer answer accuracy from retrieval recall, passing parser tests, or schema availability alone. Store reproducible, sanitized current results under `docs/results/current/` and record the command, configuration, and dataset description without embedding another project's source or name.
