# How Lens understands a project and answers questions

This guide follows one question from repository files to a cited response. It explains the general ideas behind retrieval-augmented generation (RAG), then maps each idea to Lens's implementation. The descriptions of Lens behavior are based on the source code; a stage marked as static cannot establish what a running or deployed application actually did.

## 1. What problem the pipeline solves

A language model does not automatically know the contents of a particular codebase. Sending an entire repository to a model is usually impractical: repositories can be large, the model has a finite context window, and unselected code makes it harder to focus on relevant facts. Lens therefore uses retrieval-augmented generation. It first builds a searchable local representation of a project, retrieves a small body of evidence for a question, and asks a configured language model to produce an answer from that evidence.

The pipeline has two distinct times:

1. **Index time:** discover project material, parse it, create searchable records and vectors, and persist a project snapshot.
2. **Question time:** interpret the question, retrieve and rank relevant records, follow selected known relationships where useful, build a bounded evidence context, generate a response, and validate its structure and citations.

The separation matters. Parsing and embedding are comparatively expensive but can be reused across questions. Question-time work is narrower and does not need to reread or resend the whole project. An unchanged index can reuse vectors when the chunk text, embedding model, and vector dimensions match.

**Index time — prepare a searchable project snapshot:**

Project files → discover and filter → parse supported file types → create entities and evidence-backed relationships → build retrieval chunks and compact embedding text → generate local vectors. Lens stores the parsed entities, relationships, chunks, and vectors as a project snapshot in PostgreSQL.

**Question time — retrieve evidence and answer:**

Question and selected project → interpret the question → retrieve lexical and vector candidates → merge and rank, with optional concept or relationship expansion → select source-backed evidence within the context limit → generate a structured response → validate the response and citation references → return a cited answer or abstain when evidence is insufficient.

**Figure 1.** Lens's two-stage RAG pipeline. PostgreSQL stores the Lens index; it is separate from an application's database used by the explicit SQL workflow.

## 2. Index time: from files to knowledge

### 2.1 File discovery is a relevance and cost decision

Lens starts with a deterministic directory traversal. The traversal applies built-in exclusions for common version-control metadata, virtual environments, dependency trees, generated outputs, caches, and build directories. It also reads `.gitignore` and `.lensignore`, with explicit exceptions for important application settings and project documentation in recognized documentation locations. The goal is to index first-party material that can explain behavior while avoiding large, repetitive third-party or generated trees.

Each candidate file is selected by extension or recognized filename and assigned a language/type adapter. A file's raw bytes are hashed for change detection; its content is normalized for parsing so common encodings such as UTF-8 BOM, UTF-16, and Windows legacy text can be handled. Unsupported files and excluded paths do not become evidence. A parser failure is recorded as a warning so one malformed file does not necessarily stop the entire repository scan.

This is an important boundary: indexing is not equivalent to copying every byte into the model. A file must pass discovery, filtering, and parser dispatch before it can become searchable. The index statistics distinguish scanned, ignored, skipped, and indexed files; these counts should not be treated as interchangeable.

### 2.2 Parsing turns text into typed structure

Parsing attempts to preserve useful program structure rather than splitting source at arbitrary character counts. The shared knowledge model contains:

| Record | Meaning in Lens |
|---|---|
| `CodeFile` | A path, language, content hash, and size. |
| `CodeSymbol` | A source-backed entity such as a class, method, function, component, endpoint, configuration item, or documentation section, with name, kind, file, line range, source, and metadata. |
| `DatabaseObject` | A database entity represented with the same identity/location shape as a symbol. |
| `Relationship` | A directed, typed statement between entities, with evidence, confidence, and metadata. |
| `DocumentChunk` | A retrieval unit derived from an entity, including searchable text, source location, metadata, and optional compact embedding text. |

Stable identifiers are derived from stable input parts rather than randomly assigned. This makes references and chunk identities reproducible when the same project structures are parsed again.

Different file types need different parsing strategies:

- **Tree-sitter language adapters** inspect syntax trees for supported JavaScript/TypeScript, JSX/TSX, HTML, CSS, C#, and Razor constructs. Syntax trees let an adapter distinguish a method declaration from a text occurrence of a similar name and retain source line locations. If a grammar reports recoverable errors, the parser can retain structures it recognizes and report the limitation.
- **Python parsing** extracts declarations and related source structure using Python's syntax facilities.
- **SQL parsing** recognizes supported DDL and routine constructs, creates database-object entities, and extracts table/procedure references. SQL dialect and statement coverage are necessarily bounded; warnings indicate statements the adapter could not understand reliably.
- **Configuration parsing** reads supported structured formats and flattens nested keys into searchable facts. Sensitive assignments are redacted from evidence intended for answers or traces. Configuration is evidence about declared settings, not proof that a deployment loaded or successfully used them.
- **Project-document parsing** treats Markdown and related text documents as authored knowledge. Markdown front matter can contribute version/date/status labels, and headings create hierarchical sections. Oversized sections are bounded at paragraph/line boundaries, with original source line ranges retained. Document freshness is marked unverified: a date written in a file is not an independent validation that the instructions are current.
- **.NET project metadata** contributes project/dependency and configuration facts without treating compiled output as source.

The parser's output is intentionally conservative. It can establish that syntax or a literal reference exists in a file; it does not execute code, evaluate arbitrary expressions, or prove that a conditional path ran. For example, a statically recognized URL expression with dynamic interpolation cannot safely be represented as a fully known literal URL.

### 2.3 Relationships provide a partial program graph

An entity list answers “what declarations exist?” Relationships add “what appears connected to what?” Lens uses typed directed edges such as calls, imports, reads, writes, renders, handles, requests, navigation, configuration use, and database references when a parser or resolver has supporting evidence. The relationship stores its source, target name, optional resolved target identity, evidence text, confidence, and parser/resolution metadata.

Resolution is a separate pass because a parser often sees only one file at a time. Once all files and symbols are known, the resolver can match some imports, declarations, routes, endpoints, and other references across files. Resolved edges have a target entity; unresolved references remain explicitly unresolved rather than silently becoming confirmed edges. The repository layer also extracts integration facts and creates compact synthetic evidence for recognized integrations.

The result is a **partial static graph**, not a complete runtime call graph. It is partial because dynamic dispatch, reflection, generated code, runtime dependency injection, computed routes, framework conventions, and unsupported syntax can hide connections. It is static because it is inferred from source and metadata without running the target application. Edge direction describes the recorded source relationship; reversing an edge for context does not imply reverse execution.

During chunk preparation, Lens attaches relevant incoming/outgoing relationship descriptions and bounded paths for selected entity kinds. This gives retrieval text cross-file vocabulary—for example, an endpoint can be represented alongside a statically discovered request or render relation—without sending the whole graph to the language model.

### 2.4 Chunking creates retrieval-sized evidence

In generic RAG, a document is divided into chunks small enough to retrieve and fit into a prompt. The choice of chunk boundary affects both retrieval and answer quality: a chunk that is too broad mixes unrelated topics, while one that is too small can lose the context needed to interpret a fact.

Lens's primary chunk boundary is structural: it creates a retrieval document for each non-namespace entity. For project documentation, the entity is a heading-based section (potentially subdivided when oversized). For code, it is generally a parsed symbol/entity, with source text and metadata. A chunk carries its entity kind, qualified name, path, language, line range, relationships, and domain-specific metadata. Specialized compact representations are used for some front-end, navigation, integration, Python-module, and documentation embedding inputs; the stored retrieval text and embedding text may therefore differ.

This entity-based approach keeps provenance precise and allows answers to cite a file and line range. Its trade-off is that a long method or section may be summarized/truncated for prompt context, while a small symbol may not contain enough surrounding architectural context by itself. Relationship evidence and related chunks help address that gap, within configured bounds.

## 3. Embeddings and persistent index

### 3.1 What an embedding represents

An embedding model maps a string to a numeric vector. Semantically related strings are intended to have vectors that are near one another in the model's vector space, even when they do not share many exact words. Lens uses FastEmbed to generate embeddings locally on CPU. A chunk can have a dedicated compact `embedding_text`; when present, that is embedded instead of the full retrieval text.

For a question vector `q` and a chunk vector `d`, cosine similarity is:

**cosine_similarity(q, d) = dot(q, d) / (norm(q) × norm(d))**

Here `dot(q, d)` is the sum of pairwise products of vector components, and `norm(v)` is the Euclidean length of vector `v`. This compares vector direction while accounting for magnitude. Lens's PostgreSQL vector query uses cosine distance and converts it to a similarity score as `1 - distance`.

The score reflects geometric similarity in the embedding space, not truth, authority, or proof that a chunk answers the question. Lens validates vector counts, expected dimensions, finite values, and nonzero norms before persisting them. The configured model and dimensions are part of compatibility: switching embedding models requires a compatible set of vectors and ordinarily a reindex.

### 3.2 Why Lens also stores lexical search data

Dense embeddings are useful for paraphrase and conceptual similarity, but can blur exact identifiers, route names, table names, and rare technical terms. Lexical retrieval uses PostgreSQL full-text search over chunk text, with technical-name handling. It is useful when the user says an exact symbol or domain term. The branches are complementary:

| Retrieval branch | Strong at | Typical weakness |
|---|---|---|
| Lexical | Exact words, identifiers, distinctive terms | Synonyms and paraphrases may not share tokens. |
| Vector | Similar meaning expressed with different words | Similarity can favor a broad neighbor over the exact entity. |
| Hybrid | Combines exact-token and semantic candidates | Still depends on candidate recall, ranking choices, and query phrasing. |

### 3.3 Project-scoped PostgreSQL storage

Lens stores projects, files, entities, relationships, chunks, and vectors in PostgreSQL with pgvector support. A project identifier is derived from its normalized root; search requires a selected project, which prevents accidental mixing of unrelated project evidence. Lexical and vector branches both receive the project scope. Vectors are also keyed by model/dimension compatibility. Index replacement is performed as a snapshot operation so readers do not observe a half-written project snapshot.

The index database is Lens's own store. It is not the same as an application's database. An application database is discovered/introspected only through the separate explicit database workflow described in [Application database questions](database-questions.md).

## 4. Question time: retrieval and selection

### 4.1 Query understanding

Before searching, Lens validates the query and normalizes whitespace. A bounded, provider-independent query-understanding step extracts technical-looking identifiers, known concept vocabulary, likely intents (such as dependency, integration, location, architecture, or data flow), requested layers/actions/entity kinds, and possible relationship requirements. This is deterministic pattern-based interpretation, not a general-purpose language model that knows the project.

The interpretation influences candidate breadth and later retrieval stages. Navigation-like requests use a wider initial candidate pool because labels and routes can be weakly represented by ordinary word overlap. Broad inventory or flow questions can also reserve more answer evidence. These choices improve the opportunity to find relevant evidence but cannot guarantee that it is found.

### 4.2 Independent lexical and vector branches

In hybrid mode, Lens asks PostgreSQL for lexical candidates and embeds the question for a separate vector search. Each branch returns a bounded candidate set. Lens merges candidates by chunk identity. The lexical score is normalized against the maximum lexical score in that result set; the vector score is bounded to the expected similarity range. For a candidate chunk `c`, the ordinary hybrid score is:

**hybrid_score(c) = alpha × lexical_score(c) + (1 − alpha) × vector_score(c)**

`lexical_score(c)` is the candidate's lexical score normalized by the maximum lexical score in that search result set. `vector_score(c)` is its cosine-similarity score, bounded to the supported score range. `alpha` is a weight from 0 through 1; Lens defaults it to `0.5`, giving the two branches equal weight in this component of ranking. This formula is only one part of final ordering. In non-vector modes, Lens also computes inspectable signals for exact identifiers, relationship matches, source scope, entity kind, document-topic matches, and navigation/route evidence. Some signals affect ordering as prioritized categories or bounded boosts, so do not interpret `hybrid_score` alone as the final rank. Vector-only mode intentionally stays a pure vector baseline.

Project documents receive an explicit opportunity in final selection: when the selected top results contain no directly topic-matched document and there is room, Lens reserves one such document while retaining code context. This is a selection safeguard, not a guarantee that the document is correct or current. For questions about project intent or documented procedures, relevant documentation is valuable evidence; for questions about implemented behavior or routes, code is stronger evidence of what the source currently defines. If the two disagree, the discrepancy should be exposed rather than silently resolved by assuming either is authoritative.

### 4.3 Concept retrieval and graph expansion

For some conceptual questions, deterministic concept interpretation can retrieve additional candidates from recognized integration facts and semantic evidence. This stage ranks evidence based on matched concepts, action evidence, integration/host facts, and query intent. It is bounded and traceable; it is not open-ended query rewriting by an LLM.

For questions likely to need a cross-layer explanation, Lens may build an evidence graph over the selected project's resolved relationships and traverse from retrieved anchors. It limits depth, path count, and resulting evidence, prioritizing relationship types relevant to the requested flow. The goal is to retrieve connected evidence—such as a UI/request/handler/data-operation path—rather than merely a set of individually similar files. Ordinary questions do not need graph expansion, and vector-only search deliberately remains a baseline without graph expansion.

The graph helps only where parsers and resolvers found usable edges. It does not infer arbitrary joins or prove runtime behavior. Unresolved references remain gaps. Inspect `lens search --debug` for raw branch results, merged candidates, ranking signals, semantic retrieval state, graph-expansion intent, and final ordering.

## 5. Context construction: what the model actually receives

Retrieval can produce more text than is appropriate for one model request. Context construction selects unique evidence records under a bounded character budget (the ask path defaults to 12,000 characters, with configured bounds). It allocates space across entities, creates excerpts, and preserves line/source information. If a source is long, Lens prefers useful leading/trailing lines or query-relevant operation lines, visibly marking omitted material. A context can also include compact descriptions of selected relationship paths and explicitly allowed database schema fields for the SQL-generation use case.

Evidence items receive compact IDs such as `E-...`; these IDs map back to the chunk and its source path/lines. The provider receives the question and a JSON-rendered evidence context, not direct database access or a tool for running the project. For ordinary repository questions, the prompt marks source evidence as untrusted data, instructs the model to answer only from supplied evidence, distinguishes static facts from inference, and defines evidence statuses including sufficient, partial, insufficient, conflicting, and no evidence.

This is the grounding mechanism: the model's answer is conditioned on the selected evidence. It reduces the need for the model to rely on general knowledge, but it does not mathematically eliminate hallucinations. Retrieval can omit a decisive file, and a model can misread even correctly retrieved material.

## 6. Generation, validation, and citations

Lens has provider interfaces for local Ollama and the optional Gemini provider. Provider selection changes where generation runs; indexing and local embedding generation are separate. The prompt and schema vary by answer mode (developer, client, or user-intent). Structured generation asks for answer data rather than arbitrary terminal formatting. In client mode, instructions require plain language, source-backed UI labels/paths, and abstention when navigation is not established. A separate structured navigation result may expose a route template, controller/action, parameters, and gaps for the host application to use.

After generation, Lens decodes the response and checks its schema, evidence status, citation IDs, and selected support constraints. Citations are mapped to actual retrieved context records and returned with source paths and line ranges. The checks can reject malformed output, invented citation IDs, or certain unsupported subject claims. They do **not** constitute a complete proof that each natural-language sentence follows logically from its citations. This limitation is represented in traces as structural checks passed while semantic support is not proven.

If no usable evidence is available, the answer path can abstain without invoking the model. If evidence exists but is weak or unrelated, the answer schema should use an insufficient status and abstain rather than converting a retrieval score into confidence. A valid citation proves provenance (“this source was supplied”), not correctness (“this source is true or current”).

1. **User → Lens:** the user submits a question, selected project, and answer mode.
2. **Lens → project index:** Lens interprets the question and requests lexical candidates. It also embeds the question locally and requests vector candidates.
3. **Project index → Lens:** PostgreSQL returns ranked candidate chunks and their source records. If the query needs a cross-file explanation, Lens may fetch connected evidence from the static relationship graph.
4. **Lens → answer provider:** Lens ranks, deduplicates, and bounds the evidence, then sends the question with that evidence to the configured provider.
5. **Answer provider → Lens:** the provider returns a structured draft. Lens validates the response and citation IDs.
6. **Lens → user:** Lens returns the cited response, or abstains when the evidence is insufficient.

The project index is Lens's PostgreSQL database. The local embedding model creates the question vector; the configured answer provider generates the response.

**Figure 2.** A typical question path. The database branch is used only for the Lens index; a separate SQL workflow has its own schema introspection, query validation, confirmation, and execution steps.

## 7. Application database questions are a separate workflow

Repository RAG and application-data SQL solve different problems. Repository RAG asks what the source code and documents indicate. SQL asks what rows currently exist in an application's database. Lens's application database workflow discovers supported configuration, connects through the appropriate database driver, introspects schema and supported routine metadata, selects relevant tables/routines for the question, and asks the provider to draft a read-only query. It validates the query against safety and schema rules, presents it for explicit confirmation in the CLI, applies a row bound, executes it, and formats the result.

In this workflow, schema metadata may be supplied to generation but application rows are not sent as source evidence before execution. Live results are obtained only when the query is confirmed and executed. MySQL schema metadata can include defined foreign-key relationships; validation can prefer declared foreign-key pairs over arbitrary joins when both are available. This preference helps prevent plausible-looking but semantically incorrect joins, although it cannot establish the intended business meaning of every query. Stored-procedure support is subject to database permissions and introspection availability. See [Application database questions](database-questions.md) for configuration, permissions, and safety details.

## 8. Performance and evaluation: what a number means

Index-time cost includes file I/O, parsing, relationship resolution, chunk construction, and embedding generation. Reusing matching embeddings reduces the embedding portion; it does not necessarily mean parsing or snapshot persistence is skipped. Question-time cost includes query embedding, database retrieval, optional expansion, context creation, and provider inference. The selected provider/model and network/local hardware therefore affect answer latency independently of the repository parser.

Evaluation should separate the stages instead of using one “accuracy” number:

| Measure | What it can show | What it cannot show alone |
|---|---|---|
| Files/chunks/entities/relationships | Index coverage and output volume for a stated run | That important facts were parsed correctly or will be retrieved. |
| Retrieval recall@k | Whether labeled relevant sources appear among the first `k` retrieved items | That the answer is correct or citations entail it. |
| Citation validity | Whether cited IDs resolve to returned evidence and source locations | Whether the cited source supports the wording. |
| Answer support review | Whether a human finds each claim supported by cited evidence | General performance on unreviewed questions. |
| Index/query/provider timings | Runtime under a recorded setup | Performance on different hardware, models, or repositories. |
| SQL execution result | Rows returned by one query against one live database state | That the generated query represents every interpretation of the question. |

For reproducibility, record the project revision, repository state, Lens and model versions, provider, settings, exact questions, expected evidence, command, and measurement method. Keep development/fixture measurements distinct from production behavior. The current evaluation record, if one exists, belongs under [`results/current`](../results/current/README.md); this conceptual guide does not claim benchmark results.

When a question has a known set of relevant source items, recall at `k` can be calculated as:

**recall_at_k = relevant_items_found_in_top_k / total_relevant_items**

Here `relevant_items_found_in_top_k` counts labeled relevant items in the first `k` results, and `total_relevant_items` is the number of labeled relevant items for that question. Recall is undefined when no relevant items were labeled; such cases should be excluded or reported separately rather than treated as perfect retrieval.

## 9. Important limitations and safe interpretation

- **Parser coverage:** unsupported constructs, generated source, dynamic behavior, and syntax errors can reduce evidence coverage.
- **Static graph:** a discovered edge is a source-level relationship with a particular confidence and provenance; it does not prove a runtime call occurred. An unresolved edge is not a confirmed connection.
- **Retrieval:** exact terms, paraphrases, broad questions, and ambiguous terminology can produce different candidate sets. The debug trace is useful when an answer appears to miss an obvious file.
- **Documentation freshness:** a document can explain intent and workflows, but its date/status is authored metadata and Lens does not independently verify that it matches current behavior.
- **Context limits:** omitted excerpts and a bounded number of items can exclude necessary details. The context records truncation so it can be inspected.
- **Model dependence:** providers differ in instruction following, output-format reliability, latency, and answer quality. Schema validation does not make a weak model semantically reliable.
- **Citations:** citations establish where supplied evidence came from; semantic entailment remains a separate evaluation task.
- **Runtime truth:** static indexing cannot establish current permissions, deployed routes/settings, successful actions, or current database contents. Only the explicitly executed database workflow observes live rows.
- **Secrets:** configuration parsing and trace redaction reduce exposure, but projects should still use least-privilege credentials and avoid treating a local index or model prompt as a secret-management system.

## 10. How to inspect the stages

The focused guides cover setup, supported commands, indexing, storage, providers, client navigation, database questions, and validation. For investigation, `lens index` reports parser/indexing statistics and warnings; `lens search ... --debug` exposes retrieval decisions; `lens ask ... --json` exposes structured answer/citation/performance data; trace output, when enabled, should be handled as potentially sensitive even when redaction is applied. The [source map](../reference/source-map.md) links these concepts to their implementation modules.

## Source map for this guide

Key implementation modules include `repository/indexing.py` (discovery and orchestration), `parsing/` (format adapters), `repository/knowledge.py` (resolution and chunk creation), `repository/integrations.py` (integration evidence), `search/embeddings.py` (local vectors), `database/store.py` (PostgreSQL retrieval and snapshots), `search/retrieval.py` and `search/ranking.py` (candidate retrieval and fusion), `search/semantic_retrieval.py` and `search/multihop.py` (bounded semantic/graph expansion), `llm/context.py` (evidence budget and source excerpts), and `llm/answering.py`, `llm/prompts.py`, and `llm/schemas.py` (generation and validation). Application database behavior lives in `database/application.py`.
