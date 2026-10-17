# Lens

Lens is a local-first codebase understanding and retrieval-augmented answer assistant. It parses application source and project documentation, indexes symbols and static relationships, combines lexical and vector retrieval, and returns evidence-cited answers. Its CLI also supports schema-aware SQL generation with read-only validation and explicit execution confirmation.

## Quick start on Windows

This walkthrough installs Lens from a clean clone, starts the local index database, downloads its embedding model, configures an answer provider, indexes the included example, and asks a real question. No separate application repository is needed for the smoke test.

### Prerequisites

Install Git, Python 3.13, and Docker Desktop with Docker Compose. Lens currently requires Python 3.13 (`>=3.13,<3.14`). Choose **one** answer provider: [Ollama](https://ollama.com/download) for local generation, or Gemini with a Google API key. The first Python-package, PostgreSQL-image, embedding-model, and (for Ollama) answer-model downloads require an internet connection; the local answer model also needs sufficient disk space and may run slowly on modest hardware.

### Install and run

Run these steps in PowerShell, in order. Each step says what it does.

1. **Clone the repository and enter its directory.**

   ```powershell
   git clone https://github.com/mannansheth/Lens.git
   cd Lens
   ```

2. **Create and activate an isolated Python 3.13 environment.**

   ```powershell
   py -3.13 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   If activation is blocked by the PowerShell execution policy, use the environment's executables directly as shown in the note below.

3. **Install Lens and its required Python dependencies, then verify the CLI is available.**

   ```powershell
   python -m pip install --upgrade pip
   python -m pip install -e .
   lens --help
   ```

4. **Create Lens's private local settings file from the safe template.**

   ```powershell
   Copy-Item .env.example .env
   ```

   `.env` configures the Lens index connection and model defaults; it is ignored by Git and should stay local.

5. **Start PostgreSQL 17 with pgvector in Docker and wait for its health check.**

   ```powershell
   docker compose up -d --wait
   docker compose ps
   ```

   Docker runs the index database only. The Lens CLI and embedding model run on the host; the database files persist in the `lens_pgdata` Docker volume. [Docker Compose documents `--wait` as waiting for services to become running or healthy](https://docs.docker.com/reference/cli/docker/compose/up/).

6. **Create Lens's database tables and indexes.**

   ```powershell
   lens db-init
   ```

7. **Download and cache the local FastEmbed model used for semantic search.**

   ```powershell
   lens model-download
   ```

   By default this downloads `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions) into `.models`; later runs reuse the cache.

8. **Choose and configure an answer provider; Ollama and Gemini are both supported.**

   **Option A — local Ollama:** install Ollama, keep its local service running, then download the model selected by the sample `.env`:

   ```powershell
   irm https://ollama.com/install.ps1 | iex
   ollama pull qwen3:8b
   ```

   This uses the official Windows installer and the sample `.env`'s local model. On macOS/Linux, follow the platform-specific steps on the [Ollama download page](https://ollama.com/download). Restart the terminal after installation if needed so `ollama` is on `PATH`.

   **Option B — Gemini API:** install Lens's optional Gemini dependency, then edit `.env` to set the provider/model and paste your own key:

   ```powershell
   python -m pip install -e ".[gemini]"
   notepad .env
   ```

   In the file opened by Notepad, set these values (replace the placeholder with your key), save, and close it:

   ```dotenv
   LLM_PROVIDER=gemini
   LLM_MODEL=gemini-3.8-flash
   GEMINI_API_KEY=your_key_here
   ```

   Lens currently accepts the Gemini models shown by `lens list models`; use that command to check the available model catalogue. Keep the key private. Gemini sends each question and its selected source evidence to Google's API, and provider use may incur charges.

9. **Check that Lens can reach the configured provider and model.**

   ```powershell
   lens model test
   ```

   This checks provider/model availability. For a generation smoke test, use `lens model test --generate`; a Gemini request may incur usage charges. The final step below exercises generation end to end.

10. **Index the included full-stack demo project and select it as the current project.**

    ```powershell
    lens index examples/fullstack-app
    lens project show
    ```

    Indexing extracts the fixture's source entities and static relationships, generates their embeddings, and stores the project in PostgreSQL.

11. **Ask Lens a question about the indexed demo and see the cited response.**

    ```powershell
    lens ask "How does a journal submission reach the database?"
    ```

    Lens retrieves evidence from the selected project and asks the configured provider (Ollama or Gemini) to answer from that evidence with source citations.

To index your own project, run `lens index C:\path\to\your\project` from the Lens directory. Indexing a project also selects it; use `lens project use <directory>` to switch later.

If PowerShell activation is blocked, replace activated commands with explicit paths, for example:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\lens.exe --help
.\.venv\Scripts\lens.exe db-init
```

For macOS and Linux, install the same prerequisites and Python 3.13, create the environment with `python3.13 -m venv .venv`, activate it with `source .venv/bin/activate`, and install Lens with the commands in step 3. Continue with steps 4–7 and 9–11. For Ollama, use the platform-specific installer linked in option A; the Gemini configuration is the same on all platforms.

## Privacy and data boundaries

FastEmbed runs locally. With the local Ollama provider, answer generation is also intended to run on your machine; confirm Ollama is using local model weights. The supplied Docker Compose file runs PostgreSQL for Lens's own index and does not run Lens itself. Gemini is an optional remote answer provider: it sends the question and selected source evidence to Google. Review [provider configuration](docs/current/providers.md) before using it with private source code. The indexed application is analyzed statically; Lens does not run it.

## Documentation

- [Documentation index](docs/README.md)
- [Setup and configuration](docs/current/setup.md)
- [CLI commands](docs/current/commands.md)
- [Architecture](docs/current/architecture.md)
- [How the RAG pipeline works](docs/current/rag-pipeline.md)
- [Technical deep dive with source excerpts](docs/current/technical-deep-dive.md)
- [Current implementation status and limits](docs/current/mvp-status.md)
- [File-by-file source and API reference](docs/reference/source-map.md)
- [Validation and tests](docs/current/validation.md)

## Development checks

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
ruff check src tests
```

The test suite includes isolated fixtures and opt-in database integrations. Test counts are implementation checks, not a general measure of answer accuracy.
