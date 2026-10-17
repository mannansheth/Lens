# Validation

Run automated checks from the repository root with the project's configured Python environment:

```powershell
python -m pytest -q
ruff check src tests
```

The test suite covers unit and integration behavior, but database-backed tests may skip when their configured test service is unavailable. Record the exact command, exit status, environment, and skipped tests when publishing results. A passing suite demonstrates only the behaviors exercised by those tests; it is not a general answer-quality score.

For retrieval evaluation, use a labeled question set and report metrics such as Recall@k or MRR with the corpus, index revision, embedding model, and ranking configuration. For generated answers, separately judge evidence support, citation location, abstention behavior, and user usefulness. Keep raw private repository content and secrets out of committed result artifacts.
