"""Local CLI, with JSON inspection and a complete retrieval debug trace."""

import argparse
import json
import sys
from pathlib import Path
from lens.core.config import Settings
from lens.database.store import PostgresStore
from lens.search.embeddings import LocalEmbeddingProvider
from lens.evaluation.evaluation import evaluate
from lens.repository.indexing import index, parse_repository
from lens.core.models import stable_id
from lens.search.retrieval import describe, search
from lens.core.logging import configure, event


def output(value: object) -> None:
    print(json.dumps(value, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lens", description="Lens application intelligence and grounded retrieval"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("db-init", help="Apply the idempotent schema migrations")
    serve_parser = commands.add_parser("serve", help="Run the local Lens HTTP API for host applications and SDKs")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--project", type=Path, help="Repository root; defaults to LENS_PROJECT_PATH or the current directory")
    commands.add_parser(
        "model-download", help="Download/load public weights; source is never uploaded"
    )
    index_parser = commands.add_parser("index", help="Index supported source and project documentation")
    index_parser.add_argument("directory", type=Path, nargs="?", default=Path("."))
    index_parser.add_argument("--force", action="store_true")
    index_parser.add_argument("--refresh", action="store_true", help="Reparse and rebuild relationships while reusing unchanged embeddings")
    parse_parser = commands.add_parser("parse", help="Inspect parsing without a database or model")
    parse_parser.add_argument("directory", type=Path)
    inspect_parser = commands.add_parser(
        "inspect", help="List knowledge and optionally relationships"
    )
    inspect_parser.add_argument("--relationships", action="store_true")
    inspect_parser.add_argument("--project", type=Path)
    search_parser = commands.add_parser("search", help="Retrieve evidence with transparent scores")
    search_parser.add_argument("query")
    search_parser.add_argument("--debug", action="store_true")
    search_parser.add_argument("--json", action="store_true", help="Machine-readable search results")
    search_parser.add_argument("--no-semantic", action="store_true", help="Use Phase 3 candidate/ranking behavior")
    search_parser.add_argument("--no-multi-hop", action="store_true", help="Use ordinary Phase 2 retrieval")
    search_parser.add_argument("--max-depth", type=int, default=8, help="Maximum static evidence-chain hops (1-16)")
    search_parser.add_argument("--candidate-limit", type=int,
                               help="Override the lexical/vector candidate count per branch (1-1000)")
    ask_parser = commands.add_parser("ask", help="Grounded developer/client Q&A or unexecuted user intent")
    ask_parser.add_argument("query")
    ask_parser.add_argument("--mode", choices=["developer", "user", "client"], default="developer")
    ask_parser.add_argument("--model", help="Answer model override, e.g. qwen3:4b or gemini/gemini-3.8-flash")
    ask_parser.add_argument("--provider", choices=["ollama", "gemini"])
    ask_parser.add_argument("--trace", type=Path, help="Opt-in redacted prompt/raw-response JSON; contains source excerpts")
    ask_parser.add_argument("--project", type=Path)
    ask_parser.add_argument("--database", action="store_true", help="Include live application database metadata; never executes generated SQL")
    ask_parser.add_argument("--env-file", type=Path)
    ask_parser.add_argument("--config-file", type=Path, help="Application appsettings.json file for live database metadata")
    ask_parser.add_argument("--variable", help="Application database URL variable")
    ask_parser.add_argument("--json", action="store_true")
    ask_parser.add_argument("--debug", action="store_true", help="JSON with selected context and retrieval trace")
    ask_parser.add_argument("--timings", action="store_true", help="Source-free performance JSON on stderr, including failures")
    eval_parser = commands.add_parser("evaluate", help="Measure Recall@K and Precision@K")
    eval_parser.add_argument("dataset", type=Path)
    eval_parser.add_argument("--compare", action="store_true", help="Compare keyword, vector, and hybrid on identical labels")
    eval_parser.add_argument("--output", type=Path, help="Save evaluation JSON to a file")
    eval_parser.add_argument("--diagnostics", action="store_true",
                             help="Measure candidate-pool coverage and classify retrieval misses")
    eval_parser.add_argument("--candidate-limit", type=int,
                             help="Override the lexical/vector candidate count per branch (1-1000)")
    eval_parser.add_argument("--no-multi-hop", action="store_true")
    eval_parser.add_argument("--no-semantic", action="store_true")
    project_parser = commands.add_parser("project", help="List, select, show or remove indexed projects")
    project_commands = project_parser.add_subparsers(dest="project_command", required=True)
    project_commands.add_parser("list")
    project_commands.add_parser("show")
    project_use = project_commands.add_parser("use")
    project_use.add_argument("directory", type=Path)
    project_remove = project_commands.add_parser("remove", help="Delete a project's Lens index")
    project_remove.add_argument("directory", type=Path)
    project_remove.add_argument("--yes", action="store_true", help="Skip the deletion confirmation")
    list_parser = commands.add_parser("list", help="Discover local and supported remote answer models")
    list_parser.add_subparsers(dest="list_command", required=True).add_parser("models")
    model_parser = commands.add_parser("model", help="Configure the answer model without reindexing")
    model_commands = model_parser.add_subparsers(dest="model_command", required=True)
    model_commands.add_parser("show")
    model_use = model_commands.add_parser("use")
    model_use.add_argument("model_id")
    model_use.add_argument("--provider", choices=["ollama", "gemini"])
    model_use.add_argument("--yes", action="store_true", help="Confirm after reviewing model privacy/trade-offs")
    model_test = model_commands.add_parser("test")
    model_test.add_argument("--generate", action="store_true", help="Also send a minimal request (Gemini may incur cost)")
    eval_parser.add_argument("--split", choices=["development", "held_out"], help="Select a declared evaluation partition")
    for command in (search_parser, eval_parser):
        command.add_argument("--top-k", type=int, default=5)
        command.add_argument("--alpha", type=float)
        command.add_argument("--mode", choices=["keyword", "vector", "hybrid"], default="hybrid")
        command.add_argument("--project", type=Path)
    for name, help_text in (("db-check", "Check application database connectivity and local mysql client"),
                            ("db-schema", "Inspect an application database (metadata only)"),
                            ("sql", "Generate SQL from live schema; execution requires confirmation")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--project", type=Path)
        command.add_argument("--env-file", type=Path)
        command.add_argument("--config-file", type=Path, help="Application appsettings.json file")
        command.add_argument("--variable", help="Database URL variable or ConnectionStrings key")
        if name == "sql":
            command.add_argument("query")
            command.add_argument("--provider", choices=["ollama", "gemini"])
            command.add_argument("--model")
            command.add_argument("--row-limit", type=int, default=100)
            command.add_argument("--executor", choices=["driver", "mysql"], default="driver", help="Execute confirmed MySQL queries using the local mysql command")
    args = parser.parse_args(argv)
    try:
        settings = Settings.load()
        configure(settings.log_level)
        if args.command == "serve":
            from lens.service.api import serve
            serve(args.host, args.port, args.project)
            return 0
        project = stable_id(str(args.project.resolve())) if getattr(args, "project", None) else None
        if args.command == "parse":
            if not args.directory.is_dir():
                raise ValueError("Parse directory does not exist")
            knowledge = parse_repository(args.directory.resolve())
            from dataclasses import asdict

            output(asdict(knowledge))
            return 0
        if args.command == "model-download":
            provider = LocalEmbeddingProvider(settings)
            output({"model": provider.model, "dimension": provider.dimension})
            return 0
        if args.command in {"list", "model"}:
            from lens.llm.models import catalogue, create_provider, describe as describe_model, load_settings, save_selection
            selected = load_settings(args.model_id, args.provider) if args.command == "model" and args.model_command == "use" else load_settings()
            if args.command == "list":
                output(catalogue(selected))
            elif args.model_command == "show":
                output(describe_model(selected))
            elif args.model_command == "use":
                output(describe_model(selected))
                print("4B historically faster but weaker on complete intent; 8B slower. Gemini quality/latency unmeasured.")
                try:
                    output({"availability": create_provider(selected).model_info()})
                except (ValueError, RuntimeError, OSError) as error:
                    print(f"Warning: {error}", file=sys.stderr)
                if not args.yes and input("Save this model selection? [y/N] ").strip().lower() not in {"y", "yes"}:
                    return 0
                # Consult indexed roots before writing application state.
                save_selection(selected, PostgresStore(settings.database_url).projects())
                output({"saved": True, "provider": selected.provider, "model": selected.model})
            else:
                llm = create_provider(selected)
                status = {"model": llm.model_info()}
                if args.generate:
                    result = llm.generate([{"role": "user", "content": 'Return JSON {"ok":"yes"} only.'}],
                                          {"type": "object", "properties": {"ok": {"type": "string", "enum": ["yes"]}}, "required": ["ok"]})
                    if json.loads(result.text) != {"ok": "yes"}:
                        raise ValueError("Model smoke test returned invalid JSON")
                    status.update(generation_verified=True, performance=result.telemetry)
                output(status)
            return 0
        if args.command in {"db-check", "db-schema", "sql"}:
            from lens.database.application import (
                discover,
                inspect_schema,
                sql_workflow,
                check_database,
                render_table,
                format_sql,
            )
            if args.project is not None:
                database_root = args.project.resolve()
            elif args.env_file is not None:
                database_root = args.env_file.resolve().parent
            else:
                from lens.core.project_context import select
                database_root = Path(select(PostgresStore(settings.database_url).projects())["root"])
            connection = discover(database_root, args.variable, args.env_file, args.config_file)
            event("application_database_selected", project=str(database_root), dialect=connection.dialect)
            if args.command == "db-check":
                output(check_database(connection))
            elif args.command == "db-schema":
                output(inspect_schema(connection))
            else:
                from lens.llm.models import load_settings, create_provider
                provider = create_provider(load_settings(args.model, args.provider))
                result = sql_workflow(
                    connection,
                    args.query,
                    provider,
                    row_limit=args.row_limit,
                    executor=args.executor,
                    display=lambda sql: print(
                        f"\nGenerated SQL\n-------------\n{format_sql(sql, connection.dialect)}"
                    ),
                )
                if result.get("executed"):
                    print(f"\nResults\n-------\n{render_table(result)}")
                else:
                    print("\nQuery was not executed.")
            return 0
        store = PostgresStore(settings.database_url)
        from lens.core.project_context import select, save
        if args.command == "project":
            projects = store.projects()
            if args.project_command == "list":
                output(projects)
            elif args.project_command == "remove":
                chosen = select(projects, args.directory)
                if not args.yes and input(f"Delete Lens index for {chosen['root']}? [y/N] ").strip().lower() not in {"y", "yes"}:
                    return 0
                removed = store.remove_project(chosen["id"])
                if not removed:
                    raise ValueError("Project index no longer exists")
                from lens.core.project_context import clear_if
                clear_if(chosen["id"])
                output({"removed": True, "root": chosen["root"]})
            else:
                chosen = select(projects, args.directory if args.project_command == "use" else None)
                if args.project_command == "use":
                    save(chosen, projects)
                output(chosen)
            return 0
        if args.command in {"search", "evaluate", "ask"}:
            project = select(store.projects(), getattr(args, "project", None))["id"]
        if args.command == "db-init":
            store.initialize()
            print("Lens database schema ready (version 4).")
        elif args.command == "inspect":
            output(store.inspect(project, args.relationships))
        elif args.command == "index":
            if not args.directory.is_dir():
                raise ValueError("Index directory does not exist")
            # Check database before spending time loading model weights.
            store.snapshot(stable_id(str(args.directory.resolve())))
            provider = LocalEmbeddingProvider(settings)
            report = index(args.directory, store, provider, args.force, refresh=args.refresh)
            projects = store.projects()
            save(select(projects, args.directory), projects)
            output(report)
        elif args.command == "ask":
            from lens.llm.models import load_settings, create_provider
            from lens.llm.answering import ask
            llm_settings = load_settings(args.model, args.provider)
            llm = create_provider(llm_settings)
            if llm_settings.provider == "gemini":
                event("answer_provider_check_started", provider="gemini", model=llm_settings.model,
                      request_timeout_seconds=getattr(llm, "request_timeout", None))
            llm.model_info()  # Fail early before loading embedding weights.
            embeddings = LocalEmbeddingProvider(settings)
            store.check_model(embeddings.model, embeddings.dimension, project)
            database_schema = None
            if args.database:
                from lens.database.application import discover, inspect_schema
                root = next(Path(item["root"]) for item in store.projects() if item["id"] == project)
                database_schema = inspect_schema(discover(root, args.variable, args.env_file, args.config_file),
                                                 include_routine_definitions=True)
            response = ask(args.query, store, embeddings, llm, project, args.mode,
                           llm_settings.context_chars, settings.hybrid_alpha,
                           database_schema=database_schema)
            if args.trace:
                from lens.llm.diagnostics import write_trace
                write_trace(args.trace, response.to_dict(True))
            if args.timings:
                print(json.dumps({"performance": response.performance}), file=sys.stderr)
            if args.json or args.debug or args.mode == "user":
                output(response.to_dict(args.debug))
            else:
                print("\nAnswer\n======")
                citation_numbers = {citation["id"]: number for number, citation in enumerate(response.citations, 1)}
                if response.output["abstention"]:
                    print(response.output["abstention"])
                for statement in response.output["answer"]:
                    prefix = "Inference: " if statement["basis"] == "inference" else ""
                    citation_refs = " ".join(
                        f"[{citation_numbers[cid]}]"
                        for cid in statement["citations"]
                        if cid in citation_numbers
                    )
                    print(f"\n{prefix}{statement['text']} {citation_refs}".rstrip())
                if response.citations:
                    print("\nSources\n-------")
                    for number, citation in enumerate(response.citations, 1):
                        print(
                            f"[{number}] {citation['file']}:{citation['line_start']}–{citation['line_end']}"
                        )
        else:
            alpha = settings.hybrid_alpha if args.alpha is None else args.alpha
            if not 0 <= alpha <= 1:
                raise ValueError("alpha must be between 0 and 1")
            if not 1 <= args.top_k <= 100:
                raise ValueError("top-k must be between 1 and 100")
            provider = None if args.mode == "keyword" and not getattr(args, "compare", False) else LocalEmbeddingProvider(settings)
            if provider:
                store.check_model(provider.model, provider.dimension, project)
            if args.command == "search":
                response = search(
                    args.query, store, provider, args.top_k, alpha, args.mode, project,
                    multi_hop=not args.no_multi_hop, max_depth=args.max_depth, semantic=not args.no_semantic,
                    candidate_limit=args.candidate_limit
                )
                if args.debug or args.json:
                    output(response.debug if args.debug else [describe(h) for h in response.results])
                else:
                    for hit in response.results:
                        print(f"{hit.rank}. {hit.chunk.name}\n   {hit.chunk.file}:{hit.chunk.metadata.get('line_start', '?')} ({hit.chunk.kind}; score {hit.hybrid_score:.3f})")
                        facts = hit.chunk.metadata.get("semantic", {})
                        if facts.get("hosts"):
                            print("   Literal hosts: " + ", ".join(facts["hosts"]))
                        elif facts.get("integrations"):
                            print("   Integration references: " + ", ".join(facts["integrations"][:3]))
            else:
                dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
                if args.split:
                    dataset = [row for row in dataset if row.get("split") == args.split]
                modes = ["keyword", "vector", "hybrid"] if args.compare else [args.mode]
                if any("expected_paths" in row for row in dataset):
                    if args.diagnostics:
                        raise ValueError("Item-level diagnostics require expected chunk labels, not expected paths")
                    if not project:
                        raise ValueError("Chain evaluation requires --project for source-isolated metrics")
                    from lens.evaluation.chain_evaluation import evaluate_chains
                    graph = store.evidence_graph([project])
                    reports = {mode: evaluate_chains(dataset, lambda q, mode=mode: search(q, store, provider,
                        args.top_k, alpha, mode, project, multi_hop=not args.no_multi_hop,
                        semantic=not args.no_semantic, candidate_limit=args.candidate_limit), args.top_k, graph)
                        for mode in modes}
                else:
                    reports = {}
                    for mode in modes:
                        def retrieve_for_evaluation(query, selected_mode=mode):
                            response = search(query, store, provider, args.top_k, alpha, selected_mode, project,
                                               multi_hop=not args.no_multi_hop, semantic=not args.no_semantic,
                                               candidate_limit=args.candidate_limit)
                            return response if args.diagnostics else response.results
                        reports[mode] = evaluate(dataset, retrieve_for_evaluation, args.top_k)
                report = {"k": args.top_k, "alpha": alpha, "model": provider.model if provider else None,
                          "modes": reports} if args.compare else reports[args.mode]
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                    output({"output": str(args.output), "summary": {mode: {key: value for key,value in item.items()
                                                                            if key not in {"questions", "categories"}}
                                                                    for mode,item in reports.items()}})
                else:
                    output(report)
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, EOFError) as error:
        if args.command == "ask" and getattr(args, "trace", None):
            from lens.llm.diagnostics import write_trace
            try:
                write_trace(args.trace, {"error": str(error), "performance": getattr(error, "performance", None),
                                        "debug": getattr(error, "trace", None)})
            except OSError:
                print("Lens: diagnostic file could not be created; choose a new writable path", file=sys.stderr)
        if args.command == "ask" and (args.timings or args.debug):
            performance = getattr(error, "performance", None)
            if performance is None:
                performance = {"inference": {"model": getattr(args, "model", None), "status": "error",
                                             "error": str(error), "timeout": "timed out" in str(error).lower()},
                               "llm_seconds": None}
            print(json.dumps({"performance": performance}), file=sys.stderr)
        print(f"Lens: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
