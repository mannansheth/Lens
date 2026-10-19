# Validation

Run automated checks from the repository root with the project's configured Python environment:

```powershell
python -m pytest -q
ruff check src tests sdks/python/src
npm test --prefix sdks/node
docker compose config --quiet
```

The test suite covers unit and integration behavior, but database-backed tests may skip when their configured test service is unavailable. Record the exact command, exit status, environment, and skipped tests when publishing results. A passing suite demonstrates only the behaviors exercised by those tests; it is not a general answer-quality score.

For a container build smoke check, run `docker compose --profile sidecar build engine`, then `docker run --rm lens-engine:local --help`. These verify that the image builds and exposes the CLI entry point; they do not start the Compose stack, call a live model, or prove a complete deployment works on another machine.

For retrieval evaluation, use a labeled question set and report metrics such as Recall@k or MRR with the corpus, index revision, embedding model, and ranking configuration. For generated answers, separately judge evidence support, citation location, abstention behavior, and user usefulness. Keep raw private repository content and secrets out of committed result artifacts.

To distinguish search coverage from ranking quality, run `lens evaluate labels.json --project <root> --diagnostics`. Candidate Recall@20/@50 measures whether expected chunks occur in the union of each raw lexical/vector branch's first 20/50 results. `pre_expansion_ranked_recall` measures expected chunks present in the fused candidate list before semantic additions. `semantic_stage_candidate_recall` measures presence after semantic candidate augmentation. Per-item outcomes then distinguish candidate misses, ranking misses, final-selection drops, and retrieved evidence. Compare these values with final Recall@k before changing ranking weights. `--candidate-limit N` overrides each branch's pool size for controlled diagnostics; treat larger-pool measurements as an experiment because extra candidates can add latency and distract the final selector.
