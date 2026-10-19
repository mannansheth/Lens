# CLI reference

Use `lens --help` and `lens <command> --help` for the complete options in the installed version.

```text
lens db-init
lens serve [--host <address>] [--port <port>] [--project <directory>]
lens parse <directory>
lens index [directory] [--force | --refresh]
lens project list
lens project use <directory>
lens project show
lens project remove <directory> [--yes]
lens search <question> [--mode keyword|vector|hybrid] [--candidate-limit <count>] [--project <directory>]
lens ask <question> [--mode developer|user|client] [--project <directory>] [--json]
lens evaluate <dataset> [--project <directory>] [--compare] [--diagnostics] [--candidate-limit <count>]
lens db-check [--project <directory>] [--env-file <file>] [--config-file <file>]
lens db-schema [--project <directory>] [--env-file <file>] [--config-file <file>]
lens sql <question> [--project <directory>] [--row-limit <count>] [--executor driver|mysql]
lens list models
lens model show
lens model use <model-id>
lens model test [--generate]
```

`lens serve` starts the local HTTP API used by host applications. Its default bind is `127.0.0.1:8765`. A non-loopback bind requires `LENS_API_KEY`. `/v1/sql` returns validated SQL only; the interactive CLI command `lens sql` retains its explicit execution confirmation. See the [deployment guide](../release/deployment.md) and [OpenAPI contract](../release/openapi.yaml).

`search` returns retrieved evidence; `ask` requests an answer grounded in retrieved context. `--mode client` requests user-facing explanation and structured navigation data when present in the source. `--json` returns machine-readable output. `--debug` includes retrieval/context details; `--trace` writes an opt-in, redacted trace that can still contain source excerpts. Review traces before sharing.

`index --refresh` reparses and rebuilds relationships while reusing compatible embeddings. `index --force` rebuilds without embedding reuse. Project selection persists outside indexed repositories; `--project` is a per-command override. `project remove` deletes Lens's index data for that root after confirmation; it does not delete the source tree.

`search --candidate-limit N` overrides the number of lexical/vector candidates fetched per branch (1–1000); the default remains intent-dependent. `evaluate --diagnostics` reports labeled-evidence coverage at candidate depths 20 and 50, before and after semantic-stage additions, plus whether expected items were retrieved, missed during candidate generation, ranked below the final cutoff, or dropped by final selection. Use `--candidate-limit` to investigate whether a wider pool helps; a higher candidate recall alone does not guarantee better top-k results.
