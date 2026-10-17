# Install and initialize Lens

These steps start Lens from a fresh clone on Windows with PowerShell. Lens needs Python 3.13, Git, and Docker Desktop with Docker Compose. An internet connection is needed for Python packages and the first download of the local embedding model.

## 1. Clone and install

```powershell
git clone https://github.com/mannansheth/Lens.git
cd Lens
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

If PowerShell blocks virtual-environment activation, run the environment's executables directly, such as `\.venv\Scripts\python.exe -m pip ...` and `\.venv\Scripts\lens.exe ...`.

## 2. Configure and start PostgreSQL

```powershell
Copy-Item .env.example .env
docker compose up -d --wait
docker compose ps
```

The supplied Compose file starts PostgreSQL 17 with pgvector in the `lens-postgres` container. The local database and role are both named `lens`; the database is stored in the persistent Docker volume `lens_pgdata`. The default database URL in `.env.example` matches these settings. Keep `.env` private. Change the database password in both Compose configuration and `DATABASE_URL` if the machine needs stronger local credentials.

Initialize Lens's index schema:

```powershell
lens db-init
```

## 3. Download the local embedding model

Lens uses FastEmbed with the default `sentence-transformers/all-MiniLM-L6-v2` model (384 dimensions) on CPU. Download its weights before indexing:

```powershell
lens model-download
```

Weights are cached under `.models` by default. The download is also triggered automatically on the first indexing run if this command is skipped. Subsequent runs reuse the cached files. Set `EMBEDDING_CACHE_DIR` in `.env` to choose another location.

## 4. Index a repository and query it

```powershell
lens index C:\path\to\your\repository
lens project list
lens ask "Explain the authentication flow"
```

Lens indexes the selected repository into its PostgreSQL database. Re-index after source changes to refresh the stored representation. Use `lens project use <directory>` to select a project when working across multiple indexed repositories.

## 5. Configure answer generation

Indexing and retrieval use local embeddings and do not require an answer-generation provider. `lens ask` needs one:

- **Local Ollama:** install Ollama from [the official download page](https://ollama.com/download), keep its service running, and download the configured model (the sample `.env` uses `qwen3:8b`): `ollama pull qwen3:8b`. Verify it with `lens model test`.
- **Gemini:** install the optional client with `python -m pip install -e ".[gemini]"`, set `LLM_PROVIDER=gemini`, `LLM_MODEL=gemini-3.8-flash`, and `GEMINI_API_KEY` in `.env`, then verify the provider with `lens model test`. Run `lens list models` to see the Gemini model catalogue available to Lens. Gemini sends the question and selected source evidence to Google and may incur usage charges.

Ollama sends prompts to the configured local Ollama host. Gemini sends the question and selected source evidence to Google's service. Review provider settings before asking questions about private source code.

## Configuration and database access

Lens reads its own `.env`; it does not load a scanned repository's environment file as Lens configuration. To inspect or query the application's separate database, pass its configuration explicitly, for example `lens db-schema --project C:\path\to\repository --env-file C:\path\to\repository\.env` or use `--config-file` for an `appsettings.json` file. `lens sql` only executes a generated, validated read-only query after confirmation.

For all commands and options, run `lens --help` or see the [CLI guide](commands.md). Never commit `.env`, API keys, or database credentials.
