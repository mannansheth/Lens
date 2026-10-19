# Local deployment and release contract

## What is available now

Lens can be run as a local HTTP engine in Docker Compose beside a host application. The image contains the Lens Python engine and its parsers; Compose provides PostgreSQL with pgvector for Lens's index and persistent volumes for the index, embedding weights, and optional Ollama weights. The host application calls a versioned JSON API, so it does not need Python, PostgreSQL, or a Lens SDK written in its own language.

The developer CLI remains available for repository inspection and developer workflows. Its `lens sql` command retains the explicit confirmation step and can execute a validated read-only query. The SDK/API contract deliberately differs: `/v1/sql` returns validated SQL and database dialect only; it never executes the SQL or returns application rows. A host application must perform its own authorization, decide whether to execute the SQL, and use a least-privilege database account.

This repository currently provides the Dockerfile, Compose deployment, local HTTP API, OpenAPI contract, and independently installable Python and Node clients under `sdks/`. The Node SDK is a dependency-free wrapper for Node.js 18+ and targets server-side application code. Neither SDK is published to a package registry yet. Lens does not yet publish a prebuilt image to a registry or signed multi-platform release artifacts. The API contract remains the language-neutral integration surface; other SDKs can be thin wrappers around it.

## Host requirements and data placement

The deployment host needs Docker Engine and the Docker Compose plugin (Docker Desktop includes these on Windows and macOS). It does not need Python, PostgreSQL, the embedding runtime, or Node.js installed on the host. Docker downloads the base images and Python dependencies during the first build. The first model use downloads embedding weights into a persistent Docker volume. With the optional local Ollama profile, Ollama weights are also downloaded into a persistent volume. Internet access is needed for initial image/package/model downloads; inference can then run locally with Ollama. Gemini is optional and sends the question plus selected evidence to Google's API.

| Data | Where it lives | Persistence |
|---|---|---|
| Lens parsed project index, vectors, and metadata | PostgreSQL container | `lens_pgdata` volume |
| Embedding model weights | Lens engine container | `lens_models` volume |
| Optional local answer-model weights | Ollama container | `lens_ollama` volume |
| Source repository | Host, mounted read-only at `/workspace` | Host controls source files |
| Application database | Host application's configured database | Lens inspects metadata; API returns SQL without execution |

The embedding index is distinct from the application's MySQL/SQLite/PostgreSQL database. Deleting the Lens volumes deletes Lens's local index/model cache; it does not delete source files or the application database.

## First deployment

1. Install Docker with Compose and clone this repository.
2. Copy `.env.example` to `.env`. Set `LENS_PROJECT_PATH` to the absolute host path of the application repository, using forward slashes on Windows. Set a long random `LENS_API_KEY`. Set `LENS_LOCAL_DB_PASSWORD` to a password consisting of URL-safe alphanumeric characters. Keep `.env` private.
3. Choose an answer provider:
   - **Ollama (local):** retain `LLM_PROVIDER=ollama`; the Compose Ollama service is enabled with the `local-llm` profile. Configure `LENS_OLLAMA_HOST=http://ollama:11434` and `LLM_MODEL=qwen3:8b` (or another installed local model).
   - **Gemini (remote):** set `LLM_PROVIDER=gemini`, select a model supported by `lens list models`, and set `GEMINI_API_KEY`. Lens sends each question and selected evidence to Google when this provider is selected.
4. Build and start Lens:

   ```sh
   docker compose --profile sidecar --profile local-llm up -d --build
   ```

   For Gemini, start without the Ollama profile:

   ```sh
   docker compose --profile sidecar up -d --build
   ```

   Compose waits for PostgreSQL, applies Lens's idempotent schema migrations, then starts the API. The host port binds to `127.0.0.1` only. The engine itself listens on its private Compose network and requires `LENS_API_KEY` because that listener is non-loopback.
5. Download the local Ollama answer model when the local provider is selected:

   ```sh
   docker compose exec ollama ollama pull qwen3:8b
   ```

   For Gemini, skip this step.
6. Download/cache the local embedding model and build the initial index:

   ```sh
   docker compose run --rm engine model-download
   docker compose run --rm engine index /workspace
   ```

The example above is idempotent: unchanged inputs reuse embeddings. A successful index is required before `/v1/ask` can answer.
7. Check readiness and call the API:

   ```sh
   curl http://127.0.0.1:8765/health
   curl -H "Authorization: Bearer $LENS_API_KEY" -H "Content-Type: application/json" \
     -d '{"question":"How do users change their password?","mode":"client"}' \
     http://127.0.0.1:8765/v1/ask
   ```

   On Windows PowerShell, use `Invoke-RestMethod` or the Python SDK below. Do not put the API key in URLs, logs, browser code, or an end-user client. Keep calls server-to-server inside the host environment.

### Windows path note

Use a forward-slash absolute path in `.env`, for example `LENS_PROJECT_PATH=C:/work/product`. If Compose reports that the source path does not exist, verify Docker Desktop has permission to access the drive. Lens mounts the source read-only; the index and model cache remain in Docker volumes.

### Connecting a containerized host application

Compose creates separate named bridge networks. PostgreSQL, migrations, the engine, and Ollama join `lens-backplane`; only the engine and Ollama also join `lens-egress`, used for model downloads and optional Gemini API calls. The engine also joins `lens-api`, reserved for host-application callers. The application container joins only `lens-api`, so it cannot address PostgreSQL or Ollama by Docker service DNS. PostgreSQL's host port remains published on loopback for the developer CLI. In the host application's Compose file, attach the application service to the existing external API network and call the Lens service by its container DNS name:

```yaml
services:
  application:
    environment:
      LENS_BASE_URL: http://lens-engine:8765
      LENS_API_KEY: ${LENS_API_KEY}
    networks: [lens-api]

networks:
  lens-api:
    external: true
    name: lens-api
```

The app container should obtain the same API key from its own secret configuration. It joins only the API network and cannot address PostgreSQL through the private backplane. Native processes on the host use `http://127.0.0.1:8765` instead. Do not make the API port public to the internet.

## API and SDK usage

The complete machine-readable contract is [openapi.yaml](openapi.yaml). All POST requests accept JSON, have a 1 MiB request-body limit, and return JSON with `Cache-Control: no-store`. The API has no CORS allowance and is intended for trusted server-side callers. When `LENS_API_KEY` is configured, send it in `Authorization: Bearer ...` or `X-Lens-Key`. Non-loopback service binding refuses to start without a key. The readiness route is unauthenticated for container health checks and returns only a status plus opaque project identifier.

`POST /v1/ask` accepts `question` and optional `mode` (`client`, `developer`, or `user`). It returns Lens's structured result, including answer evidence/citations and, for client mode, source-backed navigation data. An abstention is a valid response and should remain visible to the host application.

`POST /v1/sql` accepts `question` and optional `variable`, `config_file`, `env_file`, positive integer `user_id`, and host-selected `record_scope` (`user_records_only` or `all_records`, requiring `user_id`). Configuration file paths must resolve to existing files under the configured repository root. User identity and scope are passed as structured generation context; they help Lens draft a relevant query but are not authorization or a guarantee that the query is scoped. Lens inspects schema/routine metadata, drafts SQL, validates the result as one supported read-only query, and returns `{sql, dialect, execution: "not_performed", validated_read_only: true}`. The route does not execute SQL. The host application must authorize the caller, independently enforce row-level access, and execute with a least-privilege account only if appropriate. Treat generated SQL as untrusted input despite Lens validation.

`POST /v1/index` indexes only the configured root. Use `refresh: true` to rebuild parsing/relationships while reusing unchanged embeddings; use `force: true` to regenerate all embeddings. The route is synchronous and should be called by a trusted deployment hook, not by an end-user request. Concurrent index requests are serialized inside the process.

### Python and Node clients

The separate `lens-client` package under `sdks/python` is a thin HTTP wrapper and has no third-party dependencies. Install it from a checkout using `python -m pip install ./sdks/python`; it does not install or start the engine, store credentials, or embed a second model. Example:

```python
from lens_client import LensClient

lens = LensClient("http://127.0.0.1:8765", api_key="read-from-server-secret-store")
answer = lens.ask("How do I update my contact details?", mode="client")
if answer["output"].get("abstention"):
    show_not_found(answer["output"]["abstention"])
else:
    render(answer["output"], answer["citations"])

sql_result = lens.sql("Count active customer accounts")
review_or_execute_with_host_policy(sql_result["sql"], sql_result["dialect"])
```

Other languages call the same JSON API over HTTP. Keep the API key on the server; do not compile it into a browser bundle or mobile app.

The dependency-free Node client is in [`sdks/node`](../../sdks/node/README.md). From a Lens checkout, install it into a Node application with `npm install ./sdks/node`. It provides `health()`, `ask()`, `sql()`, and `index()` with TypeScript declarations, and uses Node's built-in `fetch` (Node.js 18+). Keep it in the trusted backend; never expose its API key in React code. `sql()` returns generated SQL and dialect only. It does not execute SQL. See the SDK README for usage and error handling.

## Reindexing and deployment hooks

Lens does not monitor source changes or automatically reindex on every application build. The application build/deployment pipeline should call the authenticated index endpoint after the new source is available to Lens:

```sh
curl -H "Authorization: Bearer $LENS_API_KEY" -H "Content-Type: application/json" \
  -d '{"refresh":true}' http://127.0.0.1:8765/v1/index
```

For CI/CD, prefer a self-hosted runner on the same trusted machine/network as Lens. A hosted runner cannot reach a private local sidecar unless the deployment explicitly exposes a protected route. Keep the API key in the build system's secret store. The hook runs after build output/source deployment and before user traffic depends on updated answers. The application should handle indexing failure as a stale-index condition and expose the index age/status operationally.

An initial reindex can be slower because the embedding model and all document vectors must be generated. Later unchanged indexing reuses embeddings. `refresh=true` reparses and reconstructs relationship data; it is the recommended hook when Lens's parsing/relationship pipeline may have changed. `force=true` regenerates all vectors and is generally unnecessary for ordinary code changes.

## Updating, backup, and removal

Build and restart after updating Lens source:

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 engine
```

Back up the PostgreSQL volume using a database-aware dump before removing or migrating volumes. Model volumes can be downloaded again. Do not run `docker compose down -v` unless intentionally deleting the local Lens index and model caches. The source repository and application database are outside these Lens volumes.

To stop Lens while preserving data, run `docker compose down`. To remove all Lens state intentionally, first ensure the correct Compose project is selected, then run `docker compose down -v`.

## Security and operational boundaries

- The sample deployment publishes the host port only on loopback. Keep it that way unless a private network boundary and API key are in place.
- Lens does not authenticate end users, infer roles, or authorize API calls by application identity. The host application must authenticate each caller and enforce which calls are allowed before calling Lens.
- Use an application database account with metadata-only permissions for SQL generation. Lens sends filtered schema metadata and the question to the selected answer provider; it does not send result rows through `/v1/sql`.
- The API never executes generated SQL. The developer CLI separately preserves its confirmation-based, bounded read-only execution feature.
- Read-only source mounting reduces accidental writes; repositories can still contain secrets. Secret patterns are filtered/redacted by the indexing and evidence pipeline, but do not treat that as a substitute for secret scanning or repository access controls.
- Prompt injection cannot be made impossible by prompting alone. Retrieved repository text is untrusted, SQL is validated, and the host must keep authorization and execution policy outside the model.
- Gemini sends selected evidence and the question to Google's service. Ollama and FastEmbed run in the local Compose network once their weights are present.
- The current service is a single-process MVP: no multi-tenant isolation, distributed worker, request quota, or production metrics/exporter is included. Run one trusted engine per configured repository until those capabilities exist.

## Release checklist for a distributable build

Before publishing a prebuilt public image or SDK release, automate and verify: pinned base/dependency versions; multi-platform builds; SBOM and vulnerability review; signed image/artifacts; migration compatibility; API contract compatibility; clean-machine install and upgrade; model-cache and database backup/restore; loopback/API-key binding behavior; supported provider smoke checks; and a release note naming breaking changes. The repository currently supports a local source build; it does not claim that these publishing steps have already been completed.
