# Retrieval and client-answer evaluation

> **Historical result:** This report measures pipeline revision `whole-application-v6-navigation-retrieval`. The current indexing pipeline and ranking implementation have changed since that run, so these figures must not be interpreted as current Lens quality metrics.

## Setup

Lens was evaluated against a locally indexed, multi-layer web application repository containing frontend, Node.js, Python, SQL, and Markdown material. The source remained on the local machine; only generated question prompts and retrieved context were sent to the configured answer provider for the answer checks. This report omits the repository identity, source excerpts, and source-specific expected-entity labels.

The active index reported 126 supported source files indexed, 2,135 chunks/entities, 5,784 relationships, 2,378 resolved relationships, and parser coverage of 0.913. The index used pipeline revision `whole-application-v6-navigation-retrieval`. The embedding model was `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions). Lens used its local PostgreSQL/pgvector store. Retrieval comparisons used top-k = 5 with the default hybrid alpha (0.5).

## Retrieval method and results

The manually labeled set contained 15 questions: eight development questions spanning architecture, documentation, configuration, client navigation, and cross-layer flow, plus seven separately held-out questions. Expected items were stable indexed entity names verified against the repository. Metrics are macro averages across questions. Recall@5 measures the fraction of expected items appearing in the top five; Precision@5 measures the fraction of those five that match an expected item. With few expected entities per question, precision is sensitive to how narrowly the labels are written.

| Partition | Retrieval mode | Questions | Recall@5 | Precision@5 | Questions with zero recall |
|---|---:|---:|---:|---:|---:|
| Development | Keyword | 8 | 0.427 | 0.275 | 2 |
| Development | Vector | 8 | 0.438 | 0.275 | 1 |
| Development | Hybrid | 8 | 0.635 | 0.400 | 0 |
| Held out | Keyword | 7 | 0.762 | 0.286 | 1 |
| Held out | Vector | 7 | 0.548 | 0.200 | 1 |
| Held out | Hybrid | 7 | 0.881 | 0.343 | 0 |
| All questions | Keyword | 15 | 0.583 | 0.280 | 3 |
| All questions | Vector | 15 | 0.489 | 0.240 | 2 |
| All questions | Hybrid | 15 | 0.750 | 0.373 | 0 |

Hybrid retrieval had the highest Recall@5 in both partitions. The held-out result is encouraging but not conclusive: seven questions are too few to support a broad reliability claim, and all questions come from one application. The lower Precision@5 indicates that some returned results were not among the narrow expected labels; those results may still be useful supporting context, but this evaluation does not credit them.

The development partition reproduced the earlier eight-question hybrid result (Recall@5 0.635, Precision@5 0.400). An earlier recorded run on those same eight labels had Recall@5 0.344 and Precision@5 0.225. That comparison is an observed before/after result for this small set, not an independent benchmark. The seven-question held-out partition was added after the retrieval changes and was not used to tune the ranking implementation.

## Client-mode answer checks

Four generated-answer checks used Gemini `gemini-3.5-flash-lite`, with client mode, the same local project index, and the normal hybrid retrieval configuration. They completed in 1.56-2.44 seconds total per request (mean 2.13 seconds); measured model time was 1.22-1.85 seconds (mean 1.58 seconds). These are four observations, not a latency benchmark.

| Question | Observed behavior | Evidence review |
|---|---|---|
| How do I change my password? | Gave ordered profile/password-change steps; returned the verified `/profile` destination. | Citation pointed to the profile settings component; the button label and form controls were confirmed in source. |
| How is a journal entry analyzed and saved? | Explained submitting the entry, sending it to the analysis service, and saving the result. | Citations pointed to the journal page and API route; the route validates analysis output and inserts the entry and mood fields. |
| Where can I find mental health resources? | Directed the user to the Resources section and described visible entry points. | Citations pointed to the Resources page, dashboard, and navigation component. |
| Can I sync appointments with Google Calendar? | Abstained with an insufficient-evidence response and no citations. | Correctly avoided asserting an integration unsupported by the indexed evidence. |

This was a manual spot-check for usefulness, citation/source alignment, navigation evidence, and abstention. It did not measure answer quality across paraphrases, adversarial prompts, user roles, or a large question set. The four answers were checked against source files; no independent human annotator or blinded grading was used.

## Provider and automated checks

The four answer requests above used Gemini. A local Ollama `qwen3:4b` request did not return within approximately one minute and was interrupted; no Ollama answer result is included. Retrieval evaluation and automated tests do not call a generative answer model. The Gemini sample was intentionally capped at four requests.

Automated checks run for the associated implementation change:

```powershell
python -m pytest -q
ruff check src tests
git diff --check
```

The recorded suite result was 280 passed and 12 skipped; Ruff and the whitespace check passed. Skips are environment-dependent test cases and do not contribute to the retrieval or answer metrics above.

## Reproduction notes and limitations

The retrieval results were produced with Lens's `evaluate --compare` command, top-k 5, and the development/held-out partition labels. The command form is:

```powershell
python -m lens.commands.cli evaluate <labels.json> --project <indexed-project-root> --compare --top-k 5
python -m lens.commands.cli evaluate <labels.json> --project <indexed-project-root> --compare --split held_out --top-k 5
```

Re-running requires the same source tree and an indexed project containing corresponding entities. Private, source-specific labels and raw output are kept in the ignored local `.local/evaluation/` directory, not committed here. The public metrics are rounded to three decimal places from the CLI JSON reports.

The evaluation only establishes retrieval behavior for a small, manually written set on one repository and a four-query answer spot-check. It does not establish production readiness, role-based access correctness, SQL safety, or general client-mode reliability. Broader testing should include multiple repositories, more paraphrases, independent answer grading, incorrect or stale documentation, and explicit authorization scenarios.
