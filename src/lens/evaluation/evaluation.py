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
        hits = retrieve(question["question"])[:k]
        retrieved = [h.chunk.name for h in hits]
        relevant = expected.intersection(retrieved)
        rows.append(
            {
                "question": question["question"],
                "expected": sorted(expected),
                "retrieved": retrieved,
                "recall_at_k": len(relevant) / len(expected),
                "precision_at_k": len(relevant) / k,
                "category": question.get("category", "uncategorized"),
                "missed_expected": sorted(expected - set(retrieved)),
            }
        )
    categories = {}
    for category in sorted({row["category"] for row in rows}):
        selected = [row for row in rows if row["category"] == category]
        categories[category] = {"count": len(selected),
                                "mean_recall_at_k": sum(r["recall_at_k"] for r in selected) / len(selected),
                                "mean_precision_at_k": sum(r["precision_at_k"] for r in selected) / len(selected)}
    return {
        "k": k,
        "questions": rows,
        "mean_recall_at_k": sum(r["recall_at_k"] for r in rows) / len(rows),
        "mean_precision_at_k": sum(r["precision_at_k"] for r in rows) / len(rows),
        "categories": categories,
        "retrieval_failures": sum(row["recall_at_k"] < 1 for row in rows),
        "zero_recall_questions": sum(row["recall_at_k"] == 0 for row in rows),
    }
