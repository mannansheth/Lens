# Current implementation checks

Recorded on 2026-10-19 against the Lens 0.6.0 source checkout. No application repository was indexed for these checks and no answer-generation request was sent to a model.

## Results

| Check | Command | Result |
|---|---|---|
| Unit and integration-test suite | `.venv\Scripts\python.exe -m pytest -q` | 296 passed, 12 skipped; 11.11 seconds |
| Static lint | `.venv\Scripts\python.exe -m ruff check src tests sdks/python/src` | Passed |
| Compose schema/interpolation | `docker compose config --quiet` | Passed |
| Linux engine image build | `docker compose --profile sidecar build engine` | Passed; image tagged `lens-engine:local` |
| Container CLI startup | `docker run --rm lens-engine:local --help` | Passed; the installed entry point lists `serve` and CLI commands |
| Node SDK tests | `npm test --prefix sdks/node` | 3 passed, 0 failed |
| Node SDK package dry run | `npm pack --dry-run --prefix sdks/node` | Passed; package contents include runtime, type declarations, README, and manifest |

The twelve skipped tests depend on external database services. The suite includes unit and mocked HTTP-service/SDK coverage; it does not establish live model quality or latency. The Docker image was built and its CLI help was run, but the Compose stack and HTTP service were not started. No application database was connected and no retrieval-quality benchmark or answer-generation call was performed. These results verify exercised code paths, package metadata, and image construction only; they do not certify answer accuracy, citation entailment, SQL business correctness, production security, or a clean-machine deployment.

## Reproduction

Run from the repository root with the project environment installed:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests sdks/python/src
npm test --prefix sdks/node
npm pack --dry-run --prefix sdks/node
docker compose config --quiet
docker compose --profile sidecar build engine
docker run --rm lens-engine:local --help
```

For a live deployment smoke test, follow [the release guide](../../release/deployment.md) on a host where Docker can run the Compose services, a configured provider is reachable, and any required model weights are present. Do not infer it from an image build or Compose configuration check alone.
