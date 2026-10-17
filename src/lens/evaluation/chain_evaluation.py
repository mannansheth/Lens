"""Reproducible chain metrics with explicit, bounded human-judgment coverage."""

from collections import Counter
from time import perf_counter
from lens.evaluation.evaluation import evaluate
from lens.search.evidence import layer


def expected_key(hop):
    if hop.get("direction", "forward") == "reverse":
        return hop["target"], hop["kind"], hop["source"]
    return hop["source"], hop["kind"], hop["target"]


def actual_hops(path):
    result = []
    for before, edge, after in zip(path["nodes"][:-1], path["relationships"], path["nodes"][1:], strict=True):
        result.append({"source": before["name"], "kind": edge["kind"], "target": after["name"],
                       "direction": edge["direction"], "resolved": edge["target_id"] is not None,
                       "literal_target": edge["target_name"]})
    return result


def sequence(hops):
    return [(h["source"], h["kind"], h["target"], h.get("direction", "forward")) for h in hops]


def contains(actual, expected):
    return any(actual[position:position+len(expected)] == expected
               for position in range(len(actual)-len(expected)+1))


def evaluate_chains(dataset, retrieve, k, graph=None):
    begun = perf_counter()
    responses = {row["question"]: retrieve(row["question"]) for row in dataset}
    report = evaluate(dataset, lambda query: responses[query].results, k)
    known = {expected_key(hop) for row in dataset for path in row.get("expected_paths", []) for hop in path}
    # Labels judge particular edges, not every unlisted relationship in the repository.
    judged, supported, unjudged = 0, 0, 0
    relationship_judgments = {}
    graph_by_source = {}
    if graph:
        for edge in graph.edges:
            graph_by_source.setdefault(graph.nodes[edge.source_id].name, []).append(edge)
    for label, row in zip(dataset, report["questions"], strict=True):
        response = responses[label["question"]]
        expansion = response.debug.get("cross_layer", {})
        paths = expansion.get("selected_paths", [])
        actual = [actual_hops(path) for path in paths]
        sequences = [sequence(hops) for hops in actual]
        expected = label.get("expected_paths", [])
        if any(not path for path in expected):
            raise ValueError("Expected chain paths must have at least one hop")
        recovered = sum(any(contains(values, sequence(path)) for values in sequences) for path in expected)
        recovered_hops = sum(sum(any(
            (hop["source"], hop["kind"], hop["target"], hop.get("direction", "forward")) in values
            for values in sequences) for hop in path) for path in expected)
        required_hops = sum(len(path) for path in expected)
        relevant_layers = {layer(hit.chunk.kind, hit.chunk.file, hit.chunk.language) for hit in response.results[:k]
                           if hit.chunk.name in set(label["expected"])}
        required_layers = {graph.nodes[node_id].layer for node_id in graph.nodes
                           if graph.nodes[node_id].name in label["expected"]} if graph else set()
        unsupported = label.get("expected_unresolved", [])
        correct_unresolved = 0
        for expected_edge in unsupported:
            matches = [edge for edge in graph_by_source.get(expected_edge["source"], [])
                       if edge.kind == expected_edge["kind"] and edge.target_name == expected_edge["target_name"]]
            correct_unresolved += bool(matches) and all(edge.target_id is None for edge in matches)
        path_judged, path_supported, path_unknown = 0, 0, 0
        for hops in actual:
            for hop in hops:
                key = expected_key(hop)
                negative = any(item["source"] == key[0] and item["kind"] == key[1] and
                               item["target_name"] == hop["literal_target"] for item in unsupported)
                if negative:
                    path_judged += 1
                    path_supported += int(not hop["resolved"])
                    relationship_judgments[(*key, hop["literal_target"])] = not hop["resolved"]
                elif key in known:
                    path_judged += 1
                    path_supported += int(hop["resolved"])
                    relationship_judgments[(*key, hop["literal_target"])] = hop["resolved"]
                else:
                    path_unknown += 1
        judged += path_judged
        supported += path_supported
        unjudged += path_unknown
        row.update(id=label.get("id"), split=label.get("split"),
            chain_recall=recovered / len(expected) if expected else None,
            hop_coverage=recovered_hops / required_hops if required_hops else None,
            evidence_diversity=len(relevant_layers & required_layers) / len(required_layers) if required_layers else None,
            unresolved_edge_accuracy=correct_unresolved / len(unsupported) if unsupported else None,
            judged_path_hops=path_judged, supported_path_hops=path_supported, unjudged_path_hops=path_unknown,
            selected_paths=paths, candidate_paths=expansion.get("candidate_paths", 0),
            retrieval_seconds=response.debug.get("retrieval_seconds", 0),
            reconstruction_seconds=expansion.get("reconstruction_seconds", 0))

    def mean(key):
        values = [row[key] for row in report["questions"] if row.get(key) is not None]
        return sum(values) / len(values) if values else None

    timings = sorted(row["retrieval_seconds"] for row in report["questions"])
    report.update(entity_recall_at_k=report["mean_recall_at_k"], chain_recall=mean("chain_recall"),
        hop_coverage=mean("hop_coverage"), evidence_diversity=mean("evidence_diversity"),
        unresolved_edge_accuracy=mean("unresolved_edge_accuracy"),
        path_precision=supported / judged if judged else None,
        false_relationship_rate=sum(not correct for correct in relationship_judgments.values()) / len(relationship_judgments) if relationship_judgments else None,
        judged_relationships=len(relationship_judgments),
        judged_path_hops=judged, unjudged_path_hops=unjudged,
        path_judgment_coverage=judged / (judged+unjudged) if judged+unjudged else None,
        metric_scope="Path precision covers labeled hop occurrences; false relationship rate covers distinct labeled relationships. Unjudged hops are excluded, never assumed correct.",
        split_counts=dict(Counter(row.get("split", "unspecified") for row in dataset)),
        performance={"evaluation_seconds": perf_counter()-begun,
            "mean_retrieval_seconds": sum(timings)/len(timings),
            "p95_retrieval_seconds": timings[min(len(timings)-1, int(0.95*len(timings)))],
            "mean_reconstruction_seconds": sum(row["reconstruction_seconds"] for row in report["questions"])/len(dataset),
            "candidate_paths": sum(row["candidate_paths"] for row in report["questions"])})
    return report
