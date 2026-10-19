"""Small labeled retrieval evaluation, independent of an answer generator."""

from collections.abc import Callable
from lens.core.models import SearchHit


def evaluate(dataset: list[dict], retrieve: Callable[[str], list[SearchHit]], k: int) -> dict:
    if k < 1 or not dataset:
        raise ValueError("Evaluation needs a non-empty dataset and positive k")
    rows = []
    for question in dataset:
        if not isinstance(question, dict) or not isinstance(question.get("question"), str) or not isinstance(question.get("expected"), list) or any(not isinstance(name, str) for name in question["expected"]):
            raise ValueError("Evaluation entries need a question string and an expected list of stable names")
        expected = set(question["expected"])
        if not expected:
            raise ValueError("Every evaluation question needs expected relevant items")
        response = retrieve(question["question"])
        # SearchResponse is optional so older/lightweight callers can continue
        # evaluating a plain list of hits. With diagnostics enabled, retain the
        # raw branch pools and full ranked candidates to identify where gold
        # evidence was lost.
        debug = getattr(response, "debug", None)
        hits = (response.results if debug is not None else response)[:k]
        retrieved = [h.chunk.name for h in hits]
        relevant = expected.intersection(retrieved)
        row = {
                "question": question["question"],
                "expected": sorted(expected),
                "retrieved": retrieved,
                "recall_at_k": len(relevant) / len(expected),
                "precision_at_k": len(relevant) / k,
                "category": question.get("category", "uncategorized"),
                "missed_expected": sorted(expected - set(retrieved)),
            }
        if debug is not None:
            keyword = debug.get("keyword_results_raw", [])
            vector = debug.get("vector_results_raw", [])
            ranked = debug.get("normalized_and_hybrid_scores", [])
            expanded = debug.get("considered_candidates", [])
            branch_ranks = {}
            for branch in (keyword, vector):
                for position, hit in enumerate(branch, 1):
                    name = hit.get("item")
                    if name:
                        branch_ranks[name] = min(position, branch_ranks.get(name, position))
            ranked_positions = {hit.get("item"): position for position, hit in enumerate(ranked, 1)}
            expanded_names = {hit.get("item") for hit in expanded}
            rank_at = {}
            for depth in (20, 50):
                pool = {hit.get("item") for branch in (keyword, vector) for hit in branch[:depth]}
                rank_at[f"branch_candidate_recall_at_{depth}"] = len(expected & pool) / len(expected)
            row["retrieval_diagnostics"] = {
                **rank_at,
                "branch_candidate_recall_at_limit": len(expected & set(branch_ranks)) / len(expected),
                "pre_expansion_ranked_recall": len(expected & set(ranked_positions)) / len(expected),
                "semantic_stage_candidate_recall": len(expected & expanded_names) / len(expected),
                "expected_item_diagnosis": {
                    name: ("retrieved" if name in retrieved
                           else "candidate_miss" if name not in branch_ranks and name not in expanded_names
                           else "semantic_recovery" if name not in branch_ranks
                           else "ranking_miss" if ranked_positions.get(name, k + 1) > k
                           else "selection_drop")
                    for name in sorted(expected)
                },
            }
        rows.append(row)
    categories = {}
    for category in sorted({row["category"] for row in rows}):
        selected = [row for row in rows if row["category"] == category]
        categories[category] = {"count": len(selected),
                                "mean_recall_at_k": sum(r["recall_at_k"] for r in selected) / len(selected),
                                "mean_precision_at_k": sum(r["precision_at_k"] for r in selected) / len(selected)}
    report = {
        "k": k,
        "questions": rows,
        "mean_recall_at_k": sum(r["recall_at_k"] for r in rows) / len(rows),
        "mean_precision_at_k": sum(r["precision_at_k"] for r in rows) / len(rows),
        "categories": categories,
        "retrieval_failures": sum(row["recall_at_k"] < 1 for row in rows),
        "zero_recall_questions": sum(row["recall_at_k"] == 0 for row in rows),
    }
    diagnostic_rows = [row["retrieval_diagnostics"] for row in rows if "retrieval_diagnostics" in row]
    if diagnostic_rows:
        report["diagnostics"] = {
            key: sum(row[key] for row in diagnostic_rows) / len(diagnostic_rows)
            for key in diagnostic_rows[0] if key.endswith("recall_at_20") or
            key.endswith("recall_at_50") or key.endswith("recall_at_limit") or
            key in {"pre_expansion_ranked_recall", "semantic_stage_candidate_recall"}
        }
        outcomes = {}
        for row in diagnostic_rows:
            for outcome in row["expected_item_diagnosis"].values():
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
        report["diagnostics"]["expected_item_outcomes"] = outcomes
    return report
