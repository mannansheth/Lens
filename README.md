# Lens

Lens is a local-first codebase understanding and retrieval-augmented answer assistant. It parses application source and project documentation, indexes symbols and static relationships, combines lexical and vector retrieval, and produces evidence-cited answers. The `lens` CLI also provides schema-aware SQL generation with read-only validation and explicit execution confirmation.

## Start here

- [Documentation index](docs/README.md)
- [Setup and configuration](docs/current/setup.md)
- [CLI commands](docs/current/commands.md)
- [Architecture](docs/current/architecture.md)
- [Current implementation status and limits](docs/current/mvp-status.md)
- [File-by-file source and API reference](docs/reference/source-map.md)
- [Validation and tests](docs/current/validation.md)

## Requirements

Lens requires Python 3.13, PostgreSQL with pgvector for its index, and local embedding model weights. Answer generation can use Ollama locally or the optional Gemini provider. Gemini sends selected prompts and evidence to Google; use Ollama for local answer generation.

```powershell
python -m pip install -e ".[dev]"
lens --help
lens db-init
lens index C:/path/to/your/project
lens project use C:/path/to/your/project
lens ask "Explain the authentication flow"
```

For setup details, database configuration, provider setup, reindexing, and SQL safeguards, follow the linked guides above. Lens does not execute application code. Static relationships can be incomplete, citations do not prove semantic entailment, and SQL must be reviewed before confirmation.

## Development checks

```powershell
python -m pytest -q
ruff check src tests scripts
```

The tests include isolated fixtures and opt-in integrations. Test counts and evaluation results are intentionally maintained with current reproducible records rather than presented as permanent product guarantees.
