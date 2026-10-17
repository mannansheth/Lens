# Lens technical deep dive

This document explains Lens as an implemented system, from installation and repository bytes through parsing, indexing, retrieval, generation, and SQL. Code excerpts below are copied from the linked source files at the time this document was written. They are deliberately short and appear beside the behavior they explain; surrounding code is omitted where it is not needed. Treat the links as the authoritative version if implementation changes.

For the shorter conceptual overview, see [How Lens understands a project and answers questions](rag-pipeline.md). For command usage and installation, see [CLI commands](commands.md) and [Setup](setup.md).

## 1. System boundaries and runtime layout

Lens is a Python package and command-line application. `pyproject.toml` names the distribution `lens-rag`, requires Python 3.13 through 3.13.x, and installs the `lens` command as the entry point `lens.commands.cli:main`. The package uses a `src/` layout. The Python process running the CLI performs repository discovery, parsing, relationship resolution, embedding, retrieval, prompt construction, and provider calls.

Docker Compose does **not** run the Lens Python application. It starts PostgreSQL 17 using the `pgvector/pgvector:pg17` image. The CLI normally runs on the host and connects to that service through a loopback-only published port. The container stores Lens's persistent index in the named volume `lens_pgdata`; deleting or replacing the container does not itself remove that volume. The embedding model files are a separate local cache, `.models` by default. The optional local answer provider is a third component (Ollama, installed and run separately); Gemini is an optional remote provider. Finally, a project's own application database is a fourth, separate data source used only by explicit database commands.

| Component | Runs/stores where | Purpose |
|---|---|---|
| Lens CLI and Python package | Host Python environment | Orchestrates indexing and questions. |
| FastEmbed model weights | Local cache directory, `.models` by default | Generates question and chunk vectors locally on CPU. |
| PostgreSQL + pgvector | Docker container `lens-postgres`; volume `lens_pgdata` | Stores projects, source records, relationships, chunks, and embeddings. |
| Ollama (optional) | Separately installed local service | Local language-model generation for answers. |
| Gemini (optional) | External Google API when explicitly configured | Remote language-model generation; question/evidence leave the machine. |
| Application database (optional) | Whatever host/server its project configuration describes | Live schema inspection and explicitly confirmed read-only SQL execution. |

The Compose excerpt below is the actual service definition. The loopback bind means the published PostgreSQL port is configured for access from the host, rather than all network interfaces. `pg_isready` is used as the container health check. The database password has a local development default; deployment operators should set `LENS_LOCAL_DB_PASSWORD` and the matching Lens `DATABASE_URL` appropriately.

```yaml
services:
  database:
    container_name: lens-postgres
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_USER: lens
      POSTGRES_PASSWORD: ${LENS_LOCAL_DB_PASSWORD:-lens_local}
      POSTGRES_DB: lens
    ports:
      - "127.0.0.1:55432:5432"
    volumes:
      - lens_pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U lens -d lens"]
```

Source: [compose.yaml](../../compose.yaml#L1-L18).

The Lens index schema is initialized by source migrations packaged with the Python distribution. The store executes the migration files in a fixed order; packaging configuration includes `database/*.sql` so these resources are present after installation.

```python
def initialize(self) -> None:
    with self.connection() as conn:
        for migration in ("001_foundation.sql", "002_parsing.sql", "003_cross_layer.sql", "004_semantics.sql"):
            conn.execute(files("lens.database").joinpath(migration).read_text())
    event("database_initialized", schema_version=4)
```

Source: [store.py](../../src/lens/database/store.py#L49-L53). The schema version here is the implementation's current migration sequence, not the version of the PostgreSQL server.

## 2. Configuration and model responsibilities

Lens loads its own `.env` file, not the `.env` of a repository being indexed. `LENS_ENV_FILE` can override the settings-file path. A relative `EMBEDDING_CACHE_DIR` is resolved relative to that settings file. `DATABASE_URL` in this Lens configuration points to the Lens index database. Application DB settings are discovered separately by an explicit project database command; this distinction prevents a scanned project's credentials from silently reconfiguring Lens itself.

The settings defaults include the embedding model, CPU device, cache directory, log level, and hybrid lexical/vector weight:

```python
@dataclass(frozen=True)
class Settings:
    app_name: str = "Lens"
    database_url: str = field(default="", repr=False)
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_device: str = "cpu"
    embedding_cache_dir: str = ".models"
    log_level: str = "INFO"
    hybrid_alpha: float = 0.5
```

Source: [config.py](../../src/lens/core/config.py#L9-L17). The `repr=False` on the database URL avoids displaying it in a dataclass representation.

### 2.1 Embedding model: exact implementation details

The default FastEmbed model identifier is `sentence-transformers/all-MiniLM-L6-v2`. Lens asks FastEmbed for the model catalogue and reads that model's declared `dim`; for the configured default this is **384 dimensions**. This is not a dimension hard-coded into the provider constructor. Lens currently permits CPU only, requests the ONNX Runtime `CPUExecutionProvider`, sets four threads, and passes the configured model cache directory to FastEmbed. Embedding inputs are sent in batches of 32. Progress events are emitted after each 256 vectors and at completion. Vector validation checks item count, dimensionality, finite components, and non-zero vectors.

```python
if settings.embedding_device != "cpu":
    raise ValueError("Lens supports EMBEDDING_DEVICE=cpu only")
supported = {m["model"]: m for m in TextEmbedding.list_supported_models()}
if settings.embedding_model not in supported:
    raise ValueError("Unsupported EMBEDDING_MODEL; choose a FastEmbed dense text model")
self.model = settings.embedding_model
self.dimension = supported[self.model]["dim"]
self._encoder = TextEmbedding(
    model_name=self.model,
    cache_dir=settings.embedding_cache_dir,
    providers=["CPUExecutionProvider"],
    threads=4,
)
```

Source: [embeddings.py](../../src/lens/search/embeddings.py#L17-L40). FastEmbed/ONNX model files are downloaded on first use or by `lens model-download`, and cached locally. They are not inside PostgreSQL and are not part of the Docker image.

The embedding function converts each model output to a Python list and validates the batch:

```python
for vector in self._encoder.embed(list(texts), batch_size=32):
    vectors.append(vector.tolist())
    if len(vectors) % 256 == 0 or len(vectors) == len(texts):
        event("embeddings_progress", completed=len(vectors), total=len(texts))
validate_vectors(vectors, len(texts), self.dimension)
```

Source: [embeddings.py](../../src/lens/search/embeddings.py#L42-L53).

An embedding is a learned numeric representation of text. For a query vector `q` and a chunk vector `d`, cosine similarity is:

**cosine_similarity(q, d) = dot(q, d) / (norm(q) × norm(d))**

`dot(q, d)` sums component-wise products; `norm(v)` is the Euclidean length of vector `v`. Similarity measures geometric closeness, not whether an answer is true. pgvector's `<=>` operator computes cosine distance; Lens converts it to a similarity-like score using `1 - distance` in the vector query.

### 2.2 Answer generation models are independent

Embeddings and answer generation are different inference tasks and different configurable components. The embedding model creates vectors used to find evidence. It does not write the final answer. The answer provider receives the selected context and generates a structured response. The sample environment config selects Ollama with a `qwen3:8b` model by default; Gemini support is an optional install extra. A configured remote provider receives the question and evidence context. A local embedding model does not by itself imply that answer generation is local.

The provider protocol exposes health, model information, and generation. Ollama's adapter sends HTTP requests to the configured host. Gemini's adapter uses the optional Google client. Provider choice does not change stored embeddings; changing the embedding model affects vector compatibility and generally requires reindexing.

## 3. Index time in detail

The top-level orchestration lives in `repository/indexing.py`. It scans files, checks whether the stored project snapshot can be reused unchanged, parses and resolves changed project content, constructs chunks, reuses cached embeddings where possible, embeds new inputs, validates them, and asks the store to replace the project snapshot.

### 3.1 File selection and boilerplate filtering

Lens uses `os.walk` without following directory symlinks. It prunes common vendor, virtual-environment, build, cache, package, generated, and tool-specific directories before descending. It reads `.gitignore` and `.lensignore`; project documentation in `docs/` or `documentation/` and selected essential configuration (such as `.env` and `appsettings*.json`) have special retention behavior. Several lockfiles, minified/bundled files, maps, generated C# suffixes, and unsupported JSON data files are excluded. Size limits apply to documentation and configuration files.

The parser dispatch table is the implementation's supported extension map:

```python
PARSERS = {".cs": ("csharp", csharp.parse), ".sql": ("mysql", sql.parse),
           ".js": ("javascript", frontend.parse), ".mjs": ("javascript", frontend.parse),
           ".cjs": ("javascript", frontend.parse), ".jsx": ("jsx", frontend.parse),
           ".ts": ("typescript", frontend.parse), ".tsx": ("tsx", frontend.parse),
           ".html": ("html", web.parse), ".css": ("css", web.parse),
           ".py": ("python", python.parse), ".cshtml": ("razor", razor.parse),
           ".csproj": ("dotnet_project", dotnet_project.parse), ".props": ("dotnet_project", dotnet_project.parse),
           ".targets": ("dotnet_project", dotnet_project.parse), ".sln": ("dotnet_project", dotnet_project.parse),
           **{suffix: ("configuration", configuration.parse) for suffix in
              (".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".env")},
           **{suffix: ("documentation", documents.parse) for suffix in
              (".md", ".mdx", ".rst", ".adoc", ".txt")}}
```

Source: [indexing.py](../../src/lens/repository/indexing.py#L22-L33). JSON has an additional filename policy: arbitrary JSON datasets are not all treated as application configuration. The extension map alone therefore does not mean every `.json` file is indexed.

The traversal computes SHA-256 over original bytes to detect changes, then normalizes bytes for parser input. This distinction means a file's identity/change check is based on its actual bytes while parser input can be normalized for BOM/encoding handling. File statistics separately record scanned, ignored, skipped, and indexed/parsed files.

### 3.2 Parsing and its failure boundary

Adapters return the shared knowledge structure rather than directly writing SQL. The repository parser accumulates each file's symbols, database objects, relationships, and warnings. An exception from one file becomes a per-file warning/status; the traversal can continue to later files. After file adapters complete, integration extraction runs, relationships are resolved across the whole project, and integration evidence documents are created.

```python
for file_number, file in enumerate(knowledge.files, 1):
    parser = parser_for(file.path)[1]
    try:
        parsed = parser(file, contents[file.id])
        knowledge.symbols.extend(parsed.symbols)
        knowledge.database_objects.extend(parsed.database_objects)
        knowledge.relationships.extend(parsed.relationships)
        knowledge.warnings.extend(parsed.warnings)
        knowledge.statistics["file_reports"].append({"path": file.path, "language": file.language,
                                                    "status": "partial" if parsed.warnings else "parsed",
                                                    "entities": len(parsed.entities), "warnings": parsed.warnings})
    except Exception as error:  # A malformed file must not abort repository indexing.
        knowledge.warnings.append(
            f"{file.path}: parser failed ({type(error).__name__}); skipped"
        )
        knowledge.statistics["file_reports"].append({"path": file.path, "language": file.language,
                                                    "status": "failed", "entities": 0})
```

Source: [indexing.py](../../src/lens/repository/indexing.py#L196-L213). A warning does not necessarily mean a whole repository failed; inspect file-level statuses and warnings in the index result. A parser may also return partial structures with warnings.

### 3.3 What parsing means for each family

The frontend adapter uses Tree-sitter grammars to inspect declarations, module imports/exports, components, and selected literal UI/request evidence. `syntax.py` supplies cached grammar selection and tree parsing. C# and Razor have corresponding adapters. Python extracts module/declaration information using Python syntax structures. SQL uses SQLGlot-backed parsing for recognized DDL and routine definitions. Configuration adapters turn supported structured settings into searchable evidence while redacting sensitive values. The project-document adapter divides Markdown and related documents at headings and retains source locations; those sections are authored descriptions, not proof of current runtime behavior.

The parser does not run the target application. It does not evaluate arbitrary expressions, execute SQL routines, follow runtime dependency injection, or observe which branch was taken. Literal-only extraction and unresolved references make uncertainty explicit. A partially recovered syntax tree is useful evidence but not a guarantee that every construct in that file was modeled.

### 3.4 Knowledge objects, relationships, and graph limits

The central records are:

- `CodeFile`: file identity, path, language, content hash, and byte size.
- `CodeSymbol`: a code or document entity with kind, qualified name, source location, excerpt, and metadata.
- `DatabaseObject`: a database entity using the symbol location/identity shape.
- `Relationship`: directed edge with a relation kind, target name, optional resolved target ID, evidence, confidence, and metadata.
- `DocumentChunk`: retrieval-ready text plus file/entity identity, source line range, metadata, and an optional separate embedding input.
- `Embedding`: chunk ID, model identity, and numeric vector.

After parsing, `resolve` can connect a reference to a known entity when the implementation has sufficient evidence. Resolved relationships may be used for bounded retrieval expansion. Unresolved names are not equivalent to confirmed calls. The resulting graph is a partial static graph: it is useful for source-backed paths through UI, API, code, and database objects, but dynamic behavior and unsupported framework conventions can leave gaps.

### 3.5 Chunk construction is structural, not fixed-size sliding windows

Lens usually creates one main retrieval chunk for each non-namespace entity. A documentation parser makes heading-based section entities; long documentation sections are subdivided with bounded lengths. Code chunks carry symbol kind/name, source text or summary, related edge descriptions, and selected short relationship paths. Some front-end, navigation, integration, Python-module, and documentation records get compact `embedding_text` that differs from the full text used as retrieval evidence.

This is why a chunk is more than a slice of source code. Its metadata preserves provenance and enriches search text with names and known relationships. The chunk still points back to the source file and lines, so Lens can return citations. Keeping chunks entity-oriented makes code citations interpretable, while potentially long methods still need excerpt/context budgeting at answer time.

## 4. Embedding, caching, and snapshot persistence

Each chunk's embedding input is the explicit `embedding_text` if present, otherwise its retrieval text. Lens hashes that exact input. Previously stored vectors are reusable when their input hash, model, and dimension match. A repository unchanged fast path additionally checks file hashes, model, dimensions, and indexing pipeline revision. If the fast path does not apply, Lens reparses the snapshot, then can still reuse vectors for unchanged embedding inputs. Therefore “embeddings reused” does not necessarily mean “parsing skipped.”

All chunks must have exactly one embedding, and the embedding model/dimension must agree with project metadata. Project replacement occurs through one database transaction, guarded by a PostgreSQL advisory transaction lock for concurrent writes to the same project. This avoids readers seeing a partially replaced index.

```python
if len(embeddings) != len(chunks) or {e.chunk_id for e in embeddings} != {c.id for c in chunks}:
    raise ValueError("Every chunk needs exactly one embedding")
if any(e.model != model or e.dimension != dimension for e in embeddings):
    raise ValueError("Embedding model/dimension differs from project metadata")
validate_vectors([e.vector for e in embeddings], len(chunks), dimension)
project = stable_id(str(root))
with self.connection(vectors=True) as conn:
    # Serialize concurrent replacement of the same project.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (project,))
    conn.execute("DELETE FROM projects WHERE id = %s", (project,))
    conn.execute(
        "INSERT INTO projects(id, root, model, dimension,pipeline_revision,statistics) VALUES (%s,%s,%s,%s,%s,%s)",
        (project, str(root), model, dimension, PIPELINE_REVISION, Jsonb(knowledge.statistics)),
    )
```

Source: [store.py](../../src/lens/database/store.py#L82-L95). The psycopg connection context manages the transaction; the delete and subsequent inserts are committed together or rolled back on failure.

## 5. Question-time retrieval in detail

### 5.1 Candidate generation

The `ask` path requires a selected project, validates query/mode bounds, chooses a broader result count for broad or navigation/flow questions, calls search, and then synthesizes an answer from the returned evidence. Exact definition questions can use a deterministic fast path when one globally unambiguous indexed definition is found. Otherwise search performs lexical and/or vector retrieval according to mode.

In hybrid mode, PostgreSQL full-text retrieval and question-vector retrieval are independent candidate branches. Search merges candidates by chunk ID. The default initial candidate limit is at least 60 per branch; navigation-like queries widen that to at least 150. These are candidate limits, not the number of chunks necessarily sent to the model. Subsequent ranking, expansion, final selection, and context budget narrow the evidence.

The vector branch is project-scoped and filters by embedding model and dimension. For the default 384-dimensional vector it casts to the partial 384-vector index expression. The current implementation disables PostgreSQL index scans within this query to preserve exact project-scoped cosine ordering; other vector dimensions use the vector column directly and exact ordering. This favors predictable exact ranking over approximate-index speed for this retrieval query.

### 5.2 Hybrid score and ranking rules

Lens normalizes lexical scores by the maximum lexical score in that keyword result set and clips vector similarities into `[0, 1]`. It then computes:

**hybrid_score(c) = alpha × keyword_score(c) + (1 − alpha) × vector_score(c)**

`alpha` is validated in `[0, 1]`; the configured default is `0.5`. The expression is not the whole ranking policy. In non-vector mode, candidate order first prioritizes a matching relationship/API path/UI route, then exact identifier, requested source scope, requested entity kind, then the hybrid score with document-topic and navigation boosts, and finally a stable chunk ID. Consequently, the displayed hybrid score alone does not fully explain final rank. Vector-only mode intentionally sorts by vector score and stable ID without those additional query-aware signals.

```python
item.hybrid_score = (
    item.keyword_score
    if mode == "keyword"
    else item.vector_score
    if mode == "vector"
    else alpha * item.keyword_score + (1 - alpha) * item.vector_score
)
```

Source: [ranking.py](../../src/lens/search/ranking.py#L20-L27).

### 5.3 Document, concept, and navigation retrieval

Documentation sections are first-class chunks. When the initially selected top results lack a directly topic-matched document and the result count permits, Lens can reserve one matching documentation result alongside code evidence. That improves the chance of using an obviously relevant document but does not establish that the document is up to date. Source implementation and project documentation answer related but distinct questions: code is evidence of current checked-in behavior; documentation may describe intent or usage. Contradictions should be surfaced rather than concealed.

For recognized concepts and intents, a deterministic semantic-retrieval layer can query integration evidence and related concepts. It uses explicitly implemented concept vocabulary and ranking dimensions; it is not an LLM-generated chain of speculative searches. Navigation questions use route/action evidence, broader candidate pools, and extra route-aware ranking signals to retrieve actual links, labels, controller/actions, and parameter templates where statically available.

### 5.4 Relationship expansion and data-flow retrieval

When query intent indicates a flow or cross-layer question, Lens can traverse a project-scoped graph of resolved relationships from high-ranking anchors. Traversal is bounded by maximum depth and number of paths, and selected paths are converted to evidence items. It prefers relationship kinds relevant to data flow. This improves the chance of retrieving connected stages rather than merely similar isolated symbols. It cannot compensate for a relationship that the parsers did not discover or resolve.

### 5.5 Context construction and the model's input

The context builder deduplicates evidence, assigns compact citation IDs, preserves file and line ranges, includes selected relationship/path descriptions, and makes source excerpts within a character limit. The default `LLM_CONTEXT_CHARS` is 12,000; the function accepts 2,000–24,000 characters. This is a character limit on the serialized evidence context, not an exact model-token limit. Character count and token count differ by text, tokenizer, and language. The provider's model context and output-token limits are configured separately.

For application schema mode, the context builder only forwards an allowlist of schema fields—such as table/column names and types, foreign keys, and indexes. It excludes connection strings, defaults, row values, comments, and arbitrary fields. Application records are not part of ordinary repository RAG context.

```python
def build_context(response, project: str, max_chars=12000, database_schema=None, query=""):
    if not project or not 2000 <= max_chars <= 24000:
        raise ValueError("Context requires a selected project and a bounded character budget")
    items, paths, seen = [], [], set()
    truncated = False

    def add(item):
        nonlocal truncated
        if item["id"] in seen:
            original = next(value for value in items if value["id"] == item["id"])
            if original["source_uid"] != item["source_uid"]:
                raise ValueError("Compact evidence identifier collision")
            return True
        proposal = EvidenceContext(project, tuple([*items, item]), tuple(paths), True, len(response.results))
        if len(proposal.render().encode("utf-8")) > max_chars:
            truncated = True
            return False
        items.append(item)
        seen.add(item["id"])
        return True
```

Source: [context.py](../../src/lens/llm/context.py#L99-L118). The builder records truncation rather than silently treating an omitted excerpt as complete source.

## 6. Generation, structured outputs, and trust boundaries

Lens builds a system prompt for the selected mode and sends the question plus JSON-rendered evidence. Retrieved source is marked as untrusted data, not instructions. The prompt prohibits inventing symbols, routes, relationships, providers, tables, or runtime success. Client mode requests plain, user-facing steps and structured navigation details only when source establishes them. Developer mode explains implementation. User mode classifies intent for application integrations and does not ask the model to execute a query.

The answer parser validates response structure, evidence status, and citation IDs. Each answer statement must cite supplied evidence. Additional checks reject citations not present in the context, empty citations where grounding is required, mismatched subject claims, and certain contradictions. These are useful mechanical and heuristic safeguards. They do not prove semantic entailment: a real citation can still be attached to a sentence that over-interprets the source. Human evaluation and good retrieval remain necessary for high-stakes client-facing use.

The generation flow calls the search pipeline before synthesis, and adds timing data for retrieval, context building, and inference. Lens does not give the repository RAG model arbitrary tools to execute source code or SQL. SQL has its own explicit workflow.

## 7. Application database and SQL workflow

Application-data questions are separate from repository RAG. When explicitly requested, Lens discovers supported project configuration (including common environment settings and .NET application settings), connects using the database driver, introspects tables/columns/foreign keys/indexes and supported routines, selects relevant schema/routine details, and asks the provider to draft a read-only query. The query is validated before execution; the CLI requires confirmation, applies a row limit, executes, and formats the result. MySQL is supported through PyMySQL. Stored-routine visibility depends on the database server's metadata and the user's permissions. Lens's PostgreSQL index credentials must never be confused with application database credentials.

SQL generation is not equivalent to unrestricted model access to the database. The provider sees filtered schema metadata, not live rows, until an explicitly confirmed query is run. The host application or operator remains responsible for access control around who is allowed to invoke these operations.

## 8. Security, privacy, and operational limits

- **Local index:** project chunks and embeddings reside in the configured Lens PostgreSQL database. With the supplied Compose setup, that means the named Docker volume on the host.
- **Local embeddings:** FastEmbed runs on CPU locally and stores its model cache separately. The configured local embedding model does not by itself send project content to a hosted embedding API.
- **Answer provider:** Ollama is intended as a local provider when configured to a local service. Choosing Gemini sends the question and selected evidence to the external provider; this is an explicit privacy boundary.
- **Credential hygiene:** Lens redacts sensitive values in relevant parser/evidence/diagnostic paths, and does not display the Lens database URL in `Settings` representation. Redaction is a safeguard, not a substitute for excluding secrets or limiting DB permissions.
- **Static analysis:** source indexes cannot show which branches executed, which user roles were active, whether a deployed app is healthy, or what current DB rows contain.
- **Retrieval and generation:** missing chunks, ambiguous vocabulary, context truncation, and provider variability can lead to incomplete or wrong answers despite valid citations.
- **Docker:** Compose only provides the index database. It does not package Python, the embedding model, Ollama, the indexed application, or an application database server.

## 9. Configuration and setup reference

The main environment values and their current defaults are:

| Variable | Default / meaning |
|---|---|
| `APP_NAME` | `Lens` |
| `DATABASE_URL` | `postgresql://lens:lens_local@127.0.0.1:55432/lens` for the sample local Compose setup |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions from FastEmbed catalogue) |
| `EMBEDDING_DEVICE` | `cpu` (only supported device in this implementation) |
| `EMBEDDING_CACHE_DIR` | `.models`, resolved relative to the Lens settings file when relative |
| `HYBRID_ALPHA` | `0.5`; lexical share in the hybrid-score formula |
| `LLM_PROVIDER` / `LLM_MODEL` | Sample values select Ollama / `qwen3:8b` |
| `LLM_CONTEXT_CHARS` | `12000` character evidence-context budget |
| `LLM_CONTEXT_TOKENS` | `32768` provider/model setting; separate from the character budget |
| `LLM_OUTPUT_TOKENS` | `1024` output bound in the sample configuration |
| `GEMINI_API_KEY` | Empty unless the optional Gemini provider is deliberately enabled |

The setup guide covers exact commands. A concise fresh-clone sequence is: create a Python 3.13 virtual environment, install the editable package, copy `.env.example`, start Compose, initialize the Lens DB schema, download the embedding weights, configure an answer provider, then index/select/query a project. The model download and first embedding generation require network access unless the model weights are already cached. PostgreSQL must be running for indexing and retrieval.

## 10. Formulas and how to interpret them

There are three mathematical expressions central to this implementation:

1. **Cosine similarity:** **cos(q, d) = dot(q, d) / (norm(q) × norm(d))**. `q` is the question vector and `d` the candidate chunk vector. PostgreSQL's cosine distance is converted to a similarity score by **similarity = 1 − cosine_distance**.
2. **Hybrid fusion:** **H(c) = alpha × K(c) + (1 − alpha) × V(c)**. `K(c)` is the normalized keyword score, `V(c)` is the bounded vector similarity, and `alpha` lies between 0 and 1 (default 0.5). This is the score component before categorical priority and other ranking boosts.
3. **Retrieval recall at k:** **recall@k = relevant results in the first k positions / all labeled relevant results**. It measures whether a known relevant source was retrieved near the top. It does not measure answer correctness or entailment.

None of these values is a calibrated probability that an answer is correct. The embedding similarity and hybrid score are ranking signals only.

## 11. Representative command sequence

These are real CLI command forms documented for Lens; paths and provider settings are user-specific:

```powershell
Copy-Item .env.example .env
docker compose up -d
lens db-init
lens model-download
lens index C:\path\to\repository
lens project use C:\path\to\repository
lens search "authentication flow" --debug
lens ask "Explain the authentication flow" --mode developer --json
```

To inspect an application's database separately, use the database commands with the explicit project/configuration option described in [Application database questions](database-questions.md). Do not paste real credentials into documentation, traces, or public reports.

## 12. Source excerpt index

| Topic | Main implementation |
|---|---|
| Settings and environment loading | [core/config.py](../../src/lens/core/config.py) |
| File discovery and indexing orchestration | [repository/indexing.py](../../src/lens/repository/indexing.py) |
| Language and document adapters | [parsing/](../../src/lens/parsing/) |
| Relationship resolution and retrieval chunks | [repository/knowledge.py](../../src/lens/repository/knowledge.py) |
| Local vector generation | [search/embeddings.py](../../src/lens/search/embeddings.py) |
| Lexical/vector store and persistence | [database/store.py](../../src/lens/database/store.py) |
| Search candidate selection | [search/retrieval.py](../../src/lens/search/retrieval.py) |
| Score fusion and priorities | [search/ranking.py](../../src/lens/search/ranking.py) |
| Evidence context | [llm/context.py](../../src/lens/llm/context.py) |
| Answer orchestration, validation, client navigation | [llm/answering.py](../../src/lens/llm/answering.py) |
| Prompt policy | [llm/prompts.py](../../src/lens/llm/prompts.py) |
| Docker DB service | [compose.yaml](../../compose.yaml) |
