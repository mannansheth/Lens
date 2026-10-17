# CLI reference

Use `lens --help` and `lens <command> --help` for the complete options in the installed version.

```text
lens db-init
lens parse <directory>
lens index [directory] [--force | --refresh]
lens project list
lens project use <directory>
lens project show
lens project remove <directory> [--yes]
lens search <question> [--mode keyword|vector|hybrid] [--project <directory>]
lens ask <question> [--mode developer|user|client] [--project <directory>] [--json]
lens evaluate <dataset> [--project <directory>] [--compare]
lens db-check [--project <directory>] [--env-file <file>] [--config-file <file>]
lens db-schema [--project <directory>] [--env-file <file>] [--config-file <file>]
lens sql <question> [--project <directory>] [--row-limit <count>] [--executor driver|mysql]
lens list models
lens model show
lens model use <model-id>
lens model test [--generate]
```

`search` returns retrieved evidence; `ask` requests an answer grounded in retrieved context. `--mode client` requests user-facing explanation and structured navigation data when present in the source. `--json` returns machine-readable output. `--debug` includes retrieval/context details; `--trace` writes an opt-in, redacted trace that can still contain source excerpts. Review traces before sharing.

`index --refresh` reparses and rebuilds relationships while reusing compatible embeddings. `index --force` rebuilds without embedding reuse. Project selection persists outside indexed repositories; `--project` is a per-command override. `project remove` deletes Lens's index data for that root after confirmation; it does not delete the source tree.
