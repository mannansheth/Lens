# Phase 07: Local service and integration boundary

## Objective

Expose Lens to a host application through a language-neutral local API, while keeping model/index operations in one sidecar process and leaving authentication, authorization, and generated-SQL execution to the host.

## Implemented in this phase

- `lens serve` runs a JSON API with health, indexing, grounded ask, and SQL-generation routes.
- One engine process is configured for one repository root. SDK requests cannot choose arbitrary repository paths.
- API access uses a bearer key when configured; non-loopback binds refuse to start without `LENS_API_KEY`. The supplied Compose port publishes only to host loopback.
- The `/v1/sql` API returns validated SQL plus dialect and an explicit `not_performed` execution status. It cannot return database rows or execute the SQL.
- The interactive developer CLI retains its separate explicit-confirmation SQL execution workflow.
- Compose builds the engine container, starts PostgreSQL/pgvector and one-shot migrations, persists the index and model caches in named volumes, and mounts source read-only.
- Dependency-free Python and Node.js HTTP clients are installable independently from `sdks/python` and `sdks/node`. Other languages can call the same OpenAPI contract without installing Python or Lens engine dependencies.
- Authored deployment instructions cover initial setup, providers, Docker networking, reindex hooks, persistence, backup, upgrades, privacy, and the pre-publication checklist.

## Constraints and future work

The engine is a local single-process MVP, not a hosted multi-tenant service. It does not provide end-user identity or role-based policy, a registry image, signed artifacts, or an automatic repository watcher. The Python and Node.js clients are available from this checkout; a NuGet client and SDKs for other languages are not yet provided. The host must enforce access by its authenticated principal and apply its own SQL execution policy. A future release should add contract-version tests and produce pinned, signed multi-platform artifacts before advertising prebuilt production packages.

See [Local deployment and release contract](../../release/deployment.md), [OpenAPI](../../release/openapi.yaml), and [current implementation checks](../../results/current/implementation-checks.md).
