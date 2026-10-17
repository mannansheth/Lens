# Lens source map

This reference documents the Python and SQL implementation files under `src/lens/`. It is intended as a code-reading guide, not as a substitute for the implementation. Names below are taken from the current source; private helpers beginning with `_` are described where they form an important part of a module's behavior. The Python package does not define curated `__all__` exports: the command-line entry points are the `lens` console script and `python -m lens`.

## Source tree

```text
src/lens/
+-- commands/
|   +-- __init__.py
|   `-- cli.py
+-- core/
|   +-- __init__.py
|   +-- config.py
|   +-- logging.py
|   +-- models.py
|   `-- project_context.py
+-- database/
|   +-- 001_foundation.sql
|   +-- 002_parsing.sql
|   +-- 003_cross_layer.sql
|   +-- 004_semantics.sql
|   +-- __init__.py
|   +-- application.py
|   `-- store.py
+-- evaluation/
|   +-- __init__.py
|   +-- chain_evaluation.py
|   `-- evaluation.py
+-- llm/
|   +-- __init__.py
|   +-- answering.py
|   +-- context.py
|   +-- diagnostics.py
|   +-- gemini.py
|   +-- models.py
|   +-- ollama.py
|   +-- prompts.py
|   +-- provider.py
|   `-- schemas.py
+-- parsing/
|   +-- __init__.py
|   +-- configuration.py
|   +-- csharp.py
|   +-- documents.py
|   +-- dotnet_project.py
|   +-- frontend.py
|   +-- frontend_evidence.py
|   +-- python.py
|   +-- razor.py
|   +-- sql.py
|   +-- sql_text.py
|   +-- static_sql.py
|   +-- syntax.py
|   `-- web.py
+-- repository/
|   +-- __init__.py
|   +-- documents.py
|   +-- indexing.py
|   +-- integrations.py
|   `-- knowledge.py
+-- search/
|   +-- __init__.py
|   +-- embeddings.py
|   +-- evidence.py
|   +-- frontend_resolution.py
|   +-- multihop.py
|   +-- ranking.py
|   +-- ranking_signals.py
|   +-- retrieval.py
|   +-- semantic_retrieval.py
|   `-- semantics.py
+-- __init__.py
`-- __main__.py
```

The package separates command handling (`commands/`), shared settings and data types (`core/`), repository ingestion (`repository/`), query-time retrieval (`search/`), evaluation (`evaluation/`), database access, model-provider integration, and language-specific parsing. The package root contains only package initialization and the `python -m lens` entry point. `__main__.py` delegates process execution to the CLI.

## Runtime and core modules

### `src/lens/__init__.py`

Defines `__version__`, the installed distribution version. No other public API is declared here.

### `src/lens/__main__.py`

Supports `python -m lens`: imports the CLI `main` function and exits with its return code. It contains no separate command logic.

### `src/lens/commands/cli.py`

The user-facing command dispatcher.

- `output(value)` prints JSON with indentation and a `str` fallback.
- `main(argv=None)` parses commands and options, loads settings, configures logging, resolves the selected project, then dispatches indexing, parsing, search, ask, evaluation, database, model, and project operations. It returns an exit status and handles expected operational errors at the CLI boundary.

The command parser exposes `index`, `parse`, `inspect`, `search`, `ask`, `evaluate`, `project`, `model`, `list models`, `db-init`, `db-check`, `db-schema`, and `sql`. SQL generation and execution are delegated to `database.application`; the CLI is responsible for arguments, confirmation flow, and displaying executed results.

### `src/lens/core/config.py`

- `Settings` is an immutable dataclass for application-level configuration: database URL, embedding model/device/cache, log level, and hybrid-ranking weight.
- `Settings.load()` loads the configured environment file, resolves the model cache path, applies defaults, and validates the ranking weight and log level. Database credentials are excluded from the dataclass representation.

### `src/lens/core/models.py`

Defines the internal data transfer objects used across parsing, indexing, retrieval, and persistence.

- `stable_id(*parts)` creates deterministic identifiers from string components.
- `CodeFile`: file identity, relative path, detected language, content hash, and byte size.
- `CodeSymbol`: extracted entity identity, name/kind/qualified name, owning file, source line interval, source text, and parser metadata.
- `DatabaseObject`: `CodeSymbol` specialization for schema entities.
- `Relationship`: source entity, relationship type, target name and optionally resolved target ID, evidence/confidence, and metadata.
- `DocumentChunk`: retrieval unit attached to an entity and source location; carries indexed text, metadata, and optional compact `embedding_text`.
- `Embedding`: chunk ID, model, vector; `dimension` derives the vector length.
- `Knowledge`: in-memory parsed project collection of files, symbols, database objects, relationships, warnings, and statistics; `entities()` returns the combined entity view used downstream.
- `SearchHit`: chunk plus lexical, vector, and hybrid scores, rank, and ranking-signal details.

### `src/lens/repository/indexing.py`

Repository discovery, parsing orchestration, and index refresh.

- `EXCLUDED` and `PARSERS` configure ignored directories/files and parser dispatch.
- `parser_for(path)` selects the parser based on extension/name.
- `_essential(path)` and `_project_document(path)` retain selected important configuration and project documents even when broad ignore rules would skip them.
- `_vendor_directory(relative)` detects dependency/vendor trees.
- `_normalize_source(data)` decodes and normalizes source bytes for stable parsing and hashing.
- `_ignore_spec(directory)` reads ignore rules; `_is_ignored(path, rules, is_directory=False)` applies them with directory awareness.
- `scan(root)` returns discovered eligible files and scan statistics without parsing all content.
- `parse_repository(root, scanned=None)` parses the discovered sources and assembles `Knowledge`, parser warnings, and statistics.
- `index(root, store, provider, force=False, refresh=False)` compares the current scan with stored project state, reuses valid unchanged embeddings where possible, generates missing vectors, builds chunks, and replaces the persisted project snapshot. `force` and `refresh` control reindex behavior.

### `src/lens/repository/knowledge.py`

Transforms parser output into a resolved project knowledge model and retrieval chunks.

- `lexical_text(text)` normalizes text for lexical matching.
- `resolve(knowledge)` connects parser-discovered references to known entities and updates relationship resolution/statistics.
- `_application_links(knowledge)` joins explicit routes, views, controller actions, and related application facts. Ambiguous matches are left unresolved; a statically matched path is not proof of deployment routing.
- `chunks(knowledge, application)` emits bounded `DocumentChunk` objects, including relevant relationship and application context.

## Analysis package

`src/lens/search/` contains semantic query interpretation, ranking, evidence graphs, graph expansion, and retrieval. The separate `repository/` package discovers and parses project content into indexed knowledge, while `evaluation/` measures retrieval performance.

### `src/lens/repository/documents.py`

Creates textual descriptions for code entities before chunk embedding and retrieval.

- `summary_lines(entity, path, relationships, paths)` produces readable entity summary lines, including location and selected linked evidence.
- `embedding_summary(...)` makes a compact representation for vector embedding.
- `retrieval_document(...)` retains richer source evidence in the document used during retrieval. The embedding summary and retrieval text are intentionally separable.

### `src/lens/search/embeddings.py`

- `EmbeddingProvider` protocol defines `model`, `dimension`, and batch `embed(texts)` behavior.
- `LocalEmbeddingProvider(settings)` loads the configured local sentence-transformer and computes normalized vectors in batches.
- `validate_vectors(vectors, count, dimension)` checks count, dimensions, and vector validity before persistence.

### `src/lens/search/evidence.py`

Builds a navigable directed evidence graph from parsed entities and relationships.

- `FLOW_KINDS` selects relationship kinds suitable for evidence-path traversal.
- `annotate(knowledge)` adds relationship-derived evidence annotations to knowledge.
- `layer(kind, file, language='')` classifies an entity into an application layer.
- `EvidenceNode` holds a graph node and its source location/layer.
- `EvidencePath` holds a node sequence, relationship sequence, and termination reason; `describe()` renders it for context.
- `EvidenceGraph` stores nodes/edges and adjacency indexes. `__post_init__()` builds indexes, `edge_key()` creates stable edge identity, `from_knowledge()` constructs the graph, `eligible()` filters traversable edges, and `reconstruct()` explores bounded forward or upstream paths with optional preferred terms.

### `src/lens/search/frontend_resolution.py`

Resolves front-end module imports and static request paths.

- `FRONTEND_LANGUAGES` and `EXTENSIONS` define supported module types and candidate extensions.
- `ModuleResolver` provides `module_target()` for import-path resolution, `exported()` and `imported()` for named/default/re-export tracing, `lookup()` for source-aware symbol lookup, and `shadowed_http()` to detect local identifiers masking HTTP APIs.
- `resolve(knowledge)` adds resolvable front-end module relationships.
- `path_matches(request, declared)` compares static route shapes while avoiding host inference or evaluation of expressions.

### `src/lens/repository/integrations.py`

Extracts explicit integration facts and provides source-line summaries.

- `extract(knowledge, contents)` parses explicit references such as request calls, routes, and integration wiring into relationship/evidence records.
- `documents(knowledge)` produces document-level integration summaries for retrieval.
- `fact_lines(entity)` returns line-oriented integration evidence for an entity.

### `src/lens/search/multihop.py`

Expands retrieval across graph relationships for queries that need evidence spanning multiple entities.

- `STOP` is the stop-word set.
- `terms(value)` extracts normalized query terms.
- `intent(query)` identifies query shapes that benefit from chain expansion.
- `expand(query, ranked, graph, get_chunks, top_k, max_depth=8)` traverses selected graph paths, retrieves linked chunks, and augments/reranks evidence within depth/result bounds.

### `src/lens/search/ranking.py`

- `rank(keyword, vector, alpha=0.5, mode='hybrid', query=None)` combines lexical and vector candidate lists according to the selected mode and weighting, then applies query-sensitive ranking signals and produces ordered `SearchHit` results.

### `src/lens/search/ranking_signals.py`

- `technical_names(query)` extracts likely literal technical identifiers from a query.
- `signals(query, chunk)` scores query/entity matches such as explicit names and navigation evidence, returning the signal breakdown used by ranking/debug output.

### `src/lens/search/retrieval.py`

Orchestrates query understanding, search backends, document diversity, graph expansion, and semantic evidence selection.

- `describe(hit)` serializes a hit and its scores/signals for CLI output.
- `select_with_document_evidence(ranked, top_k, candidates=None)` may reserve a strongly topic-matched project-document chunk while retaining code evidence.
- `SearchResponse` stores results and retrieval diagnostics.
- `search(query, store, provider, top_k=5, alpha=0.5, mode='hybrid', project=None, multi_hop=True, max_depth=8, semantic=True)` runs enabled lexical/vector retrieval and ranking, then optionally applies multi-hop expansion and semantic prioritization. The response includes ranked evidence and diagnostic counts.

### `src/lens/search/semantic_retrieval.py`

- `priority(hit)` determines semantic candidate precedence.
- `retrieve(query, understanding, ranked, store, project, top_k, provider=None)` adds candidates selected from concept/entity metadata and semantic query interpretation, then returns a bounded ranked set.

### `src/lens/search/semantics.py`

Rule-based query interpretation used to broaden retrieval rather than to generate answers.

- `CONCEPTS` maps domain-level concepts to vocabulary; `TRIGGERS` maps query wording to intent/action categories.
- `words(value)` tokenizes and normalizes text; `phrase(value, text)` tests phrase occurrence.
- `QueryUnderstanding` records original query, identifiers, concepts, intents, expansions, scopes, relationship kinds, actions, entity kinds, and whether semantic expansion applies. `describe()` serializes this interpretation.
- `understand(query)` constructs `QueryUnderstanding`.
- `evidence_concepts(text)` extracts semantic concept labels from source text.
- `relevance(understanding, chunk)` scores a chunk against interpreted concepts/scopes/actions.

### `src/lens/evaluation/evaluation.py`

- `evaluate(dataset, retrieve, k=5)` computes retrieval metrics (including Recall@K and Precision@K) over labeled expected evidence using the supplied retrieval callback. This evaluates retrieval matching, not generated-answer correctness.

### `src/lens/evaluation/chain_evaluation.py`

Evaluates whether retrieval returns expected multi-hop evidence chains.

- `expected_key(hop)` normalizes an expected hop identifier.
- `actual_hops(path)` converts a reconstructed path into comparable hop keys.
- `sequence(hops)` normalizes a complete expected chain.
- `contains(actual, expected)` checks whether expected chain evidence is present.
- `evaluate_chains(dataset, retrieve, k, graph=None)` measures expected-chain coverage for retrieved paths.

### `src/lens/core/project_context.py`

Persists and resolves the active indexed project selection.

- `state_path()` locates local application selection state.
- `active()` reads the stored selection.
- `select(projects, explicit=None, cwd=None)` resolves an explicit project, saved active project, or best matching current working directory using indexed roots.
- `save(project, projects)` writes a valid project selection.
- `clear_if(project_id)` clears persisted selection when its project is removed.

### `src/lens/core/logging.py`

- `JsonFormatter.format(record)` emits structured JSON log records.
- `configure(level)` configures the application logger and handler.
- `event(name, level=logging.INFO, **fields)` emits a named structured event.

## Database modules

### `src/lens/database/__init__.py`

Package marker for database code; no explicit re-exports.

### `src/lens/database/store.py`

PostgreSQL persistence, migrations, and retrieval queries.

- `PIPELINE_REVISION` identifies the indexing/retrieval pipeline version stored with each project.
- `RetrievalStore` protocol specifies `keyword_search()` and `vector_search()` for retrieval consumers.
- `PostgresStore(url)` validates and stores the PostgreSQL URL.
- `connection(vectors=False)` manages a database connection, optional pgvector adapter registration, and sanitized failure reporting.
- `initialize()` applies bundled idempotent SQL migrations.
- `cached_embeddings(project, model, dimension)` returns previously stored vectors keyed by source-content embedding hash.
- `snapshot(project)` reads indexed project metadata and file hashes.
- `replace(root, knowledge, chunks, embeddings, model, dimension)` validates data, serializes concurrent refreshes, and transactionally replaces a project index.
- `inspect(project=None, relations=False)` returns project/index counts and optional relationship details.
- `check_model(model, dimension, project)` verifies index compatibility.
- `evidence_graph(projects)` loads project relationships as an `EvidenceGraph`.
- `definition_hits(name, project)` finds exact definition candidates.
- `project_ids_for_entities(entities)` and `chunks_for_entities(entities, projects)` locate semantic/entity evidence.
- `_hit(row, branch)` constructs a `SearchHit` from a result row.
- `keyword_search(...)` performs PostgreSQL full-text search.
- `vector_search(...)` performs embedding similarity search with model/dimension/project constraints and optional integration evidence.
- `projects()` lists indexed projects; `remove_project(project)` deletes a project and dependent index rows.
- `semantic_candidates(concepts, project, limit=60, integrations=False)` retrieves candidates by stored semantic metadata.

### `src/lens/database/application.py`

Discovery, inspection, constrained SQL generation, and optionally confirmed MySQL execution against an application's database.

- `Connection` stores dialect, connection value, project root, configuration source, and parsed parameters.
- `_mysql_provider_evidence(root)` detects provider information in project files.
- `_ado_properties(value)` splits a .NET connection string while respecting quoted/braced values.
- `_mysql_ado_connection(...)` converts recognized .NET/MySQL connection settings to a MySQL connection configuration.
- `_appsettings_files(root, config_file)` locates .NET application settings.
- `discover(root, variable=None, env_file=None, config_file=None)` locates supported DB credentials/connection strings in explicit environment/configuration sources and returns a `Connection` without logging secrets.
- `connect(config)` opens the selected database connection.
- `mysql_schema(db, database)` reads MySQL table/column/key metadata.
- `mysql_routines(db, database, include_definitions=False)` reads stored routine signatures and optionally source definitions.
- `inspect_schema(config, include_routine_definitions=False)` returns schema metadata for supported application DB connections.
- `SENSITIVE_COLUMN` and `SAFE_FUNCTIONS` define sensitive result columns and SQL functions accepted by validation.
- `validate_sql(sql, schema)` parses and checks generated SQL against allowed read-only constructs, known schema, sensitive fields, function allowlist, and join constraints.
- `_validate_foreign_key_join_preference(tree, schema)` rejects a generated join when declared foreign-key relationships provide a clear alternative to an assumed join.
- `mysql_client()` locates the host MySQL command-line client.
- `check_database(config)` checks connectivity and reports client availability.
- `bounded_mysql_sql(sql, row_limit)` applies a maximum result bound to read queries.
- `format_results(columns, rows, row_limit)` creates structured, bounded result data.
- `_relevant_routines(...)` selects relevant routine signatures, including definitions only for close matches within a token budget.
- `_relevant_tables(...)` selects prompt schema tables while retaining nearby foreign-key targets.
- `_sql_terms(value, stop)` tokenizes SQL names and definitions using the caller's stop-word set; routine and table ranking share this helper.
- `validate_sql(...)` accepts one read-only SELECT, CTE, or set-operation query while rejecting multiple statements and unsafe operations.
- `render_table(result)` formats result rows for CLI display, selecting a horizontal or vertical layout by width.
- `execute_mysql_command(...)` executes a validated read query through the host's MySQL command where selected.
- `execute(...)` executes a validated read query through the configured database driver.
- `generate_sql(question, schema, provider)` asks an LLM provider for SQL grounded in schema evidence.
- `sql_workflow(...)` coordinates schema/routine context, SQL generation, validation, user confirmation, bounded execution, and result return.

## LLM modules

### `src/lens/llm/__init__.py`

Package marker only; does not re-export the provider or answer APIs.

### `src/lens/llm/provider.py`

- `Generation` carries generated text, optional token counts, and provider telemetry.
- `LLMProvider` protocol specifies `health()`, `model_info()`, and schema-aware `generate(messages, schema=None)`.

### `src/lens/llm/models.py`

Model selection and provider construction.

- `GEMINI_MODELS` lists supported Gemini model identifiers.
- `selection_path()` locates saved model selection.
- `identify(model, provider=None)` resolves a model/provider pair.
- `load_settings(model=None, provider=None)` reads selected model settings and applies overrides.
- `create_provider(settings)` instantiates the selected provider.
- `describe(settings)` returns provider/model capability and availability details.
- `save_selection(settings, projects=())` persists selection after validating against project embedding/model metadata as needed.
- `catalogue(settings)` lists available model choices and current selection.

### `src/lens/llm/ollama.py`

Local Ollama-compatible generation provider.

- `LLMSettings` contains provider/model/host, timeout, temperature, context/output limits, and context-character budget. `__post_init__()` validates settings; `load()` reads defaults and overrides.
- `NoRedirect` blocks HTTP redirects so local requests do not silently leave the configured endpoint.
- `OllamaProvider` builds requests, reports health/model information, and generates answers. `_request()` handles HTTP communication; `generate()` applies the public provider contract; `_generate()` maps messages/schema and captures usage telemetry.

### `src/lens/llm/gemini.py`

Remote Gemini provider.

- `RETRY_OPTIONS` configures bounded retry behavior.
- `gemini_schema(schema)` converts the internal output schema to the subset accepted by the provider; local validation remains authoritative.
- `request_timeout(settings)` selects a bounded request timeout.
- `GeminiProvider` handles client initialization, sanitized error conversion (`_error()`), health and model checks, and structured `generate()` requests with a limited retry policy.

### `src/lens/llm/prompts.py`

- `VERSION` versions prompt behavior.
- `SYSTEM`, `DEVELOPER`, `USER`, and `CLIENT` hold system/developer/user/client-mode instructions.
- `messages(query, context, mode, schema)` constructs role-separated provider messages and includes response-schema instructions.

### `src/lens/llm/schemas.py`

Defines structured answer/intent schemas and independent local validation.

- `STATUSES`, `INTENTS`, `TEXT`, `CITATIONS`, `STATUS`, `ANSWER_SCHEMA`, and `INTENT_SCHEMA` define allowed output vocabulary and shape.
- `obj(properties)` constructs JSON Schema object definitions.
- `generation_schema(mode, context)` chooses the generation schema for developer answers or user-facing intent output.
- `validate(value, schema)` validates data against the supported schema constraints.
- `_object(pairs)` rejects duplicate JSON keys; `decode(text, schema)` extracts and validates a JSON response.
- `Statement`, `DeveloperAnswer`, `Constraint`, and `UserIntent` are typed parsed response records.
- `citation_check(ids, evidence)` checks citation identifiers against supplied evidence.
- `template_forms(item)` performs bounded expansion of simple static string templates from cited source.
- `literal_support(statement, evidence)` checks explicit literal support, not semantic entailment.
- `subject_support(query, parsed, evidence)` checks required lexical subject coverage for narrow scopes.
- `parse_answer(text, evidence)` decodes and validates answer statements and citations.
- `parse_intent(text, evidence)` decodes and validates a structured user intent with entities and constraints.

### `src/lens/llm/context.py`

Evidence selection and bounded prompt context construction.

- `EvidenceContext` stores project, selected evidence items and paths, truncation status, and retrieved count. `evidence()` exposes evidence records; `render()` serializes them for prompting.
- `excerpt(text, budget=1800)` retains a bounded source excerpt with omission indication.
- `source_excerpt(text, budget, query)` chooses source lines relevant to the query while retaining context around decisive operations.
- `_relevant_navigation_links(links, query, limit=12)` selects links related to a navigation question.
- `build_context(response, project, max_chars=12000, database_schema=None, query='')` assembles citations, source excerpts, graph paths, optional schema, and navigation links within a character budget.

### `src/lens/llm/answering.py`

End-to-end grounded answer workflow.

- `ABSTENTION` is the standard insufficient-evidence response.
- `evidence_limit(query, expand=False)` selects a wider evidence budget for broad/system-inventory questions.
- `AskResponse` holds mode, project, structured output, citations, performance timings, and optional debug trace; `to_dict(debug=False)` creates a JSON-safe response.
- `synthesize(query, response, llm, project, mode='developer', context_chars=12000, database_schema=None)` constructs context, invokes the model, validates generation, and returns an `AskResponse`.
- `_synthesize(...)` is the shared internal generation/validation path with performance tracking.
- `ask(...)` retrieves evidence and calls synthesis, optionally enriching with live database schema.
- `preserve_ownership(query, parsed)` maintains explicit possessive/user ownership when translating a structured answer.
- `validate_generation(query, context, raw, mode)` applies schema, citation, evidence-grounding, and abstention checks.
- `client_navigation(context, citations, query='')` derives structured source-backed destination/action/steps/gaps for client-facing navigation responses.
- `definition_answer(...)` returns direct declaration evidence only when exact indexed definitions are globally unambiguous.

### `src/lens/llm/diagnostics.py`

- `redact(value)` recursively removes sensitive values from diagnostic data.
- `write_trace(path, value)` writes an explicitly requested redacted diagnostic trace; source excerpts can remain in the trace, so it is opt-in.

## Parsing modules

### `src/lens/parsing/__init__.py`

Package marker; parser implementations are imported explicitly by the indexing dispatcher.

### `src/lens/parsing/syntax.py`

Shared tree-sitter utilities.

- `FUNCTIONS` maps supported language identifiers to callable syntax categories.
- `language(name)` resolves a parser grammar.
- `tree(name, data)` parses source bytes into a syntax tree.
- `text(node, data)` safely extracts a node's source text.
- `walk(node)` iterates descendants.
- `owned(node)` yields nodes belonging to a symbol while excluding named nested functions and including anonymous callbacks.
- `literal(node, data)` returns only statically evaluable strings; it never executes/interpolates source.
- `parameters(node, data)` extracts callable parameter names.
- `bindings(node, data)` extracts bound identifiers from declarations/destructuring while excluding property keys and type names.

### `src/lens/parsing/python.py`

- `parse(file, data)` parses Python using the shared syntax layer and produces file symbols and discovered relationships.

### `src/lens/parsing/csharp.py`

- `KINDS` and `CALLABLES` map syntax node types to entity kinds and callable nodes.
- `descendants(node)` traverses a syntax subtree.
- `parse(file, data)` extracts C# namespaces, types, methods, properties, and statically recognized references from syntax trees into `Knowledge`.

### `src/lens/parsing/frontend.py`

- `DECLARATIONS` maps JavaScript/TypeScript-family syntax nodes to supported symbol declarations.
- `parse(file, data)` extracts front-end module symbols and relationships, plus Razor/JSX-compatible constructs represented in supported grammars.
- `ancestors(node)` yields syntax parents used to recover lexical and declaration context.

### `src/lens/parsing/frontend_evidence.py`

Static extraction of request and URL evidence from front-end source.

- `HTTP_METHODS` lists recognized request methods.
- `path_values(node, data, values=None, depth=0)` performs bounded symbolic path extraction and preserves uncertain environment fallback.
- `object_values(node, data)` extracts static object key/value information.
- `url_pattern(node, data)` converts a statically recoverable URL expression to a route template.
- `extract(symbol, node, module, file, data, result)` associates request/route evidence with the source symbol and knowledge graph.

### `src/lens/parsing/web.py`

- `HTML_TAGS` lists recognized markup tags.
- `parse(file, data)` extracts HTML elements, identifiers, links, forms, and source locations relevant to page/navigation evidence.

### `src/lens/parsing/razor.py`

- `parse(file, data)` extracts Razor view/page symbols, route/action links, Razor URL helper expressions, and markup evidence while retaining unresolved dynamic values as templates.

### `src/lens/parsing/documents.py`

Parses project documentation into bounded, redacted retrieval chunks.

- `HEADING`, `FRONT_MATTER`, and `CREDENTIAL_ASSIGNMENT` define section and sensitive-content patterns.
- `_redact(text)` masks credential-like assignments before indexing.
- `_front_matter(source)` handles document front matter.
- `_segments(body, line_offset)` splits Markdown on headings and paragraphs while bounding oversized sections.
- `parse(file, data)` emits document entities/chunks with headings and source locations.

### `src/lens/parsing/configuration.py`

Parses environment and application configuration files, with secret-aware handling.

- `ENV_REFERENCE` recognizes environment-variable references.
- `jsonc_loads(source)` accepts comments and trailing commas used by .NET configuration files.
- `is_environment(path)` identifies environment-like files.
- `parse(file, data)` extracts configuration keys and safe structural facts without indexing credential values as ordinary text.

### `src/lens/parsing/dotnet_project.py`

- `PROPERTIES` lists selected project properties.
- `parse(file, data)` extracts useful .NET project/configuration metadata (such as framework and package references) without treating build outputs as source.

### `src/lens/parsing/sql.py`

Parses SQL DDL and stored routines into database objects and relationships.

- `ROUTINE` recognizes routine declarations.
- `table_name(table)` normalizes a parsed SQL table reference.
- `statement_relationships(source_id, statement)` identifies table read/write relationships, distinguishing DML write targets from tables read by nested selects.
- `parse(file, data)` extracts schema entities, columns/constraints, routines, and source-grounded relationships from supported SQL syntax.

### `src/lens/parsing/sql_text.py`

- `masked_comments(text)` masks comments without shifting offsets or altering quoted strings/identifiers.
- `statements(text)` splits SQL at semicolons outside comments and quoted regions while preserving source offsets.

### `src/lens/parsing/static_sql.py`

- `alternatives(node, data, limit=8)` derives bounded alternative static SQL strings from source expressions, without executing application code.

## SQL migration reference

The migrations are applied in lexical/version order by `PostgresStore.initialize()` and are designed to be repeatable.

### `src/lens/database/001_foundation.sql`

Enables PostgreSQL `vector`; creates the schema-version ledger and core `projects`, `files`, `entities`, `relationships`, `chunks`, and `embeddings` tables with cascading ownership constraints. Adds project/name/source/search/model indexes, generated full-text search vectors, and the default 384-dimension cosine HNSW index.

### `src/lens/database/002_parsing.sql`

Adds `pipeline_revision` and aggregate `statistics` to project records, allowing the store to detect index-pipeline changes and report parse/index metrics.

### `src/lens/database/003_cross_layer.sql`

Adds JSON metadata to relationships for evidence and cross-layer facts.

### `src/lens/database/004_semantics.sql`

Adds a GIN index on semantic concept metadata stored with chunks, supporting metadata-driven candidate retrieval.

## Reading the implementation as a pipeline

`cli.main()` selects an operation. Indexing proceeds through `indexing.scan()` and `indexing.parse_repository()`, language-specific parsers, `knowledge.resolve()`, and `knowledge.chunks()`. The embedding provider vectorizes compact embedding text; `PostgresStore.replace()` stores project, source, relationships, retrieval chunks, and vectors. A question flows through `retrieval.search()`, lexical/vector ranking, optional graph and semantic expansion, `llm.context.build_context()`, and `llm.answering.synthesize()`/`validate_generation()`. Database questions use `database.application.sql_workflow()` and its SQL validator; executing a query requires the configured confirmation path and read-only validation.

The code performs static extraction, not runtime execution of the inspected application. A path or relationship can be unresolved or deployment-dependent; answer generation must be grounded in the available source evidence and may abstain when evidence is insufficient.
