# Application database questions

Lens's application-data SQL workflow is separate from repository RAG. It discovers connection information from explicitly selected project configuration, reads schema metadata (tables, columns, declared foreign keys, and supported routine definitions), selects relevant schema evidence, generates a single SQL statement, and validates the statement before returning or displaying it.

## SDK and HTTP API

`POST /v1/sql` is the integration contract for host applications. It returns validated SQL and its dialect in a structured response. It does not execute SQL, return database rows, or decide whether a user is permitted to run a query. The host application must enforce caller identity and role policy, apply any additional query policy, and execute only when its own authorization rules allow it. The endpoint is described in the [OpenAPI document](../release/openapi.yaml).

The request can optionally specify `variable`, `config_file`, or `env_file` when application configuration is not discovered from the configured project root. Explicit file paths must resolve within that root. Keep application credentials out of logs and do not return them through the host application API. A schema-only database account is recommended.

A trusted backend may also pass an authenticated numeric `user_id` (`userId` in the Node client) and host-selected `record_scope` (`user_records_only` or `all_records`). Lens includes these as structured generation context so the model can draft a query for the caller's scope. This is only a hint to generation: it is not authorization, and Lens does not know the host application's ownership rules. The host must enforce row access independently before execution. The host derives the identity from its verified authentication context and selects the scope according to its own authorization policy; Lens cannot verify either decision.

## Developer CLI

The developer CLI has additional operator-oriented commands:

- `lens db-check` verifies configured application database connectivity.
- `lens db-schema` inspects visible metadata.
- `lens sql "question"` generates and validates SQL, then asks for explicit confirmation before executing its bounded read-only query. The operator can decline. MySQL can use the Python driver or the installed `mysql` command with `--executor mysql`.

That execution permission belongs to the interactive developer CLI only. It is not available through `LensClient.sql` or `/v1/sql`; those return SQL only.

SQL validation rejects multiple statements, non-read-only operations, wildcard selection, unknown schema objects, and unsupported functions or clauses. It prefers declared foreign-key relationships when the schema contains them. Validation narrows risk but cannot guarantee that SQL represents the user's business intent. Stored-routine visibility depends on database permissions and server metadata; Lens treats routine definitions as context and does not call routines as part of SQL generation.

The SQL-generation model sees the question and filtered schema metadata, not query result rows. Under Gemini, that selected evidence is sent to Google's API; with local Ollama, generation stays on the local Compose network. Configuration secrets must not be included in model context. See [provider configuration](providers.md), [deployment and release contract](../release/deployment.md), and [validation](validation.md).
