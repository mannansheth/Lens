"""Repository scan, hash detection, parsing, chunking, and atomic persistence."""

import os
import logging
from pathlib import Path
from pathspec import GitIgnoreSpec
from hashlib import sha256
from time import perf_counter
from lens.database.store import PIPELINE_REVISION, PostgresStore
from lens.search.embeddings import EmbeddingProvider, validate_vectors
from lens.repository.knowledge import chunks, resolve
from lens.core.models import CodeFile, Embedding, Knowledge, stable_id
from lens.parsing import csharp, frontend, sql, web, python, configuration, razor, dotnet_project, documents
from lens.core.logging import event

EXCLUDED = {".git", ".venv", "node_modules", "bin", "obj", ".models", ".local", "vendor",
            "dist", "build", "coverage", ".cache", ".next", ".nuxt", ".vite", "__pycache__",
            "venv", "virtualenv", "site-packages", "__pypackages__", ".tox", ".nox",
            ".pytest_cache", ".ruff_cache", ".mypy_cache", ".bower_components", "bower_components",
            "third_party", "third-party", "__generated__", ".turbo", ".parcel-cache", ".angular",
            "publish", "testresults", ".vs", "packages"}
PARSERS = {".cs": ("csharp", csharp.parse), ".sql": ("mysql", sql.parse),
           ".js": ("javascript", frontend.parse), ".mjs": ("javascript", frontend.parse),
           ".cjs": ("javascript", frontend.parse), ".jsx": ("jsx", frontend.parse),
           ".ts": ("typescript", frontend.parse), ".tsx": ("tsx", frontend.parse),
           ".html": ("html", web.parse), ".css": ("css", web.parse),
           ".py": ("python", python.parse), ".cshtml": ("razor", razor.parse),
           ".csproj": ("dotnet_project", dotnet_project.parse), ".props": ("dotnet_project", dotnet_project.parse),
           ".targets": ("dotnet_project", dotnet_project.parse), ".sln": ("dotnet_project", dotnet_project.parse),
           **{suffix: ("configuration", configuration.parse) for suffix in
              (".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".env")},
           **{suffix: ("documentation", documents.parse) for suffix in
              (".md", ".mdx", ".rst", ".adoc", ".txt")}}


def parser_for(path):
    path = Path(path)
    if path.suffix.lower() == ".json" and not any(
        marker in path.stem.lower() for marker in
        ("config", "settings", "package", "manifest", "launch", "global", "host", "composer")
    ) and path.name.lower() not in {"angular.json", "app.json", "firebase.json", "vercel.json", "components.json"}:
        return None
    if configuration.is_environment(str(path)):
        return ("configuration", configuration.parse)
    return PARSERS.get(Path(path).suffix.lower())


def _essential(path: Path) -> bool:
    name = path.name.lower()
    suffix = path.suffix.lower()
    return (name.startswith("appsettings") and suffix == ".json" or
            configuration.is_environment(str(path)) or _project_document(path))


def _project_document(path: Path) -> bool:
    """Project documentation is useful even when a repo-wide gitignore excludes docs/."""
    return (parser_for(path) is not None and parser_for(path)[0] == "documentation" and
            any(part.lower() in {"docs", "documentation"} for part in path.parts))


def _vendor_directory(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    if any(part in {"third_party", "third-party", "bower_components"} for part in parts):
        return True
    if "wwwroot" in parts:
        index = parts.index("wwwroot")
        tail = parts[index + 1:]
        if tail and tail[0] in {"plugins", "lib", "vendor"}:
            return True
        if tail and tail[0] in {"docs", "pages"}:
            return True
    return False


def _normalize_source(data: bytes) -> bytes:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16").encode("utf-8")
    try:
        return data.decode("utf-8-sig").encode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252").encode("utf-8")


def _ignore_spec(directory: Path):
    lines = []
    for filename in (".gitignore", ".lensignore"):
        path = directory / filename
        if path.is_file():
            try:
                lines.extend(path.read_text(encoding="utf-8-sig").splitlines())
            except (OSError, UnicodeError):
                continue
    return GitIgnoreSpec.from_lines(lines) if lines else None


def _is_ignored(path: Path, rules, is_directory=False) -> bool:
    ignored = False
    parts = path.parts
    for end in range(1, len(parts) + 1):
        prefix = Path(*parts[:end])
        is_dir = end < len(parts) or is_directory
        for base, spec in rules:
            try:
                relative = prefix.relative_to(base)
            except ValueError:
                continue
            candidate = relative.as_posix() + ("/" if is_dir else "")
            match = spec.check_file(candidate)
            if match is not None:
                ignored = bool(match.include)
    return ignored


def scan(root: Path) -> tuple[Knowledge, dict[str, bytes]]:
    knowledge = Knowledge()
    knowledge.statistics = {"files_scanned": 0, "files_ignored": 0, "files_skipped": 0,
                            "directories_ignored": 0, "file_reports": []}
    contents = {}
    rules_by_directory = {"": []}
    for directory, dirs, names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(root).as_posix()
        relative_directory = "" if relative_directory == "." else relative_directory
        parent_directory = str(Path(relative_directory).parent).replace("\\", "/")
        inherited = rules_by_directory.get("" if parent_directory == "." else parent_directory, [])
        rules = [*inherited]
        local_spec = _ignore_spec(directory_path)
        if local_spec:
            rules.append((Path(relative_directory) if relative_directory else Path("."), local_spec))
        rules_by_directory[relative_directory] = rules
        before = len(dirs)
        kept_dirs = []
        for d in dirs:
            child = Path(relative_directory) / d
            absolute = Path(directory, d)
            if (d.lower() not in EXCLUDED and not absolute.is_symlink()
                    and not (absolute / "pyvenv.cfg").is_file()
                    and not _vendor_directory(child)
                    and (not _is_ignored(child, rules, is_directory=True) or
                         d.lower() in {"docs", "documentation"})):
                kept_dirs.append(d)
        dirs[:] = sorted(kept_dirs)
        knowledge.statistics["directories_ignored"] += before - len(dirs)
        for name in sorted(names):
            path = Path(directory, name)
            knowledge.statistics["files_scanned"] += 1
            relative = path.relative_to(root)
            ignored = _is_ignored(relative, rules)
            if ignored and not _essential(relative):
                knowledge.statistics["files_ignored"] += 1
                continue
            if path.suffix.lower() == ".json" and parser_for(path) is None:
                knowledge.warnings.append(f"{path.relative_to(root).as_posix()}: JSON dataset/non-configuration file ignored")
            lower_name = name.lower()
            if (lower_name in {"package-lock.json", "npm-shrinkwrap.json", "composer.lock", "yarn.lock", "pnpm-lock.yaml"}
                    or parser_for(path) is None or path.is_symlink()
                    or lower_name.endswith((".min.js", ".min.css", ".bundle.js", ".map", ".g.cs", ".generated.cs"))):
                knowledge.statistics["files_ignored"] += 1
                continue
            relative = relative.as_posix()
            try:
                language = parser_for(path)[0]
                max_size = 2_000_000 if language == "documentation" else 1_000_000 if language == "configuration" else None
                if max_size and path.stat().st_size > max_size:
                    knowledge.warnings.append(f"{relative}: {language} exceeds {max_size // 1_000_000} MB; skipped")
                    knowledge.statistics["files_skipped"] += 1
                    continue
                data = path.read_bytes()
                parse_data = _normalize_source(data)
            except (OSError, UnicodeError):
                knowledge.warnings.append(f"{relative}: unreadable or not UTF-8; skipped")
                knowledge.statistics["files_skipped"] += 1
                continue
            language = parser_for(path)[0]
            file = CodeFile(
                stable_id(str(root), relative),
                relative,
                language,
                sha256(data).hexdigest(),
                len(data),
            )
            knowledge.files.append(file)
            contents[file.id] = parse_data
    event("repository_scanned", files=len(knowledge.files),
          directories_ignored=knowledge.statistics["directories_ignored"],
          source_bytes=sum(f.size for f in knowledge.files))
    return knowledge, contents


def parse_repository(root: Path, scanned: tuple[Knowledge, dict[str, bytes]] | None = None) -> Knowledge:
    root = root.resolve()
    knowledge, contents = scanned if scanned is not None else scan(root)
    event("parsing_started", files=len(knowledge.files))
    parse_begun = perf_counter()
    last_progress = parse_begun
    for file_number, file in enumerate(knowledge.files, 1):
        parser = parser_for(file.path)[1]
        try:
            parsed = parser(file, contents[file.id])
            knowledge.symbols.extend(parsed.symbols)
            knowledge.database_objects.extend(parsed.database_objects)
            knowledge.relationships.extend(parsed.relationships)
            knowledge.warnings.extend(parsed.warnings)
            knowledge.statistics["file_reports"].append({"path": file.path, "language": file.language,
                                                        "status": "partial" if parsed.warnings else "parsed",
                                                        "entities": len(parsed.entities), "warnings": parsed.warnings})
            event("file_parsed", path=file.path, entities=len(parsed.entities), level=logging.DEBUG)
        except Exception as error:  # A malformed file must not abort repository indexing.
            knowledge.warnings.append(
                f"{file.path}: parser failed ({type(error).__name__}); skipped"
            )
            knowledge.statistics["file_reports"].append({"path": file.path, "language": file.language,
                                                        "status": "failed", "entities": 0})
        if perf_counter() - last_progress >= 5:
            event("parsing_progress", files_parsed=file_number, files_total=len(knowledge.files))
            last_progress = perf_counter()
    event("parsing_completed", files=len(knowledge.files), entities=len(knowledge.entities),
          seconds=round(perf_counter() - parse_begun, 3))
    from lens.repository.integrations import extract, documents
    event("relationships_started", entities=len(knowledge.entities))
    extract(knowledge, {f.id: contents[f.id] for f in knowledge.files
                        if f.language not in {"configuration", "documentation"}})
    resolve(knowledge)
    documents(knowledge)
    event("relationships_completed", relationships=len(knowledge.relationships))
    knowledge.statistics["files_indexed"] = len(knowledge.files)
    knowledge.statistics["files_parsed"] = sum(r["status"] != "failed" for r in knowledge.statistics["file_reports"])
    knowledge.statistics["parser_coverage"] = sum(r["status"] == "parsed" for r in knowledge.statistics["file_reports"]) / len(knowledge.files) if knowledge.files else 0
    knowledge.statistics["parser_recovery_coverage"] = knowledge.statistics["files_parsed"] / len(knowledge.files) if knowledge.files else 0
    knowledge.statistics["files_partially_parsed"] = sum(r["status"] == "partial" for r in knowledge.statistics["file_reports"])
    knowledge.warnings = list(dict.fromkeys(knowledge.warnings))
    knowledge.statistics["files_without_warnings"] = sum(r["status"] == "parsed" for r in knowledge.statistics["file_reports"])
    knowledge.statistics["files_by_language"] = {language: sum(f.language == language for f in knowledge.files)
                                                for language in sorted({f.language for f in knowledge.files})}
    knowledge.statistics["retrieval_profile"] = "frontend" if any(f.language in {"javascript", "jsx", "typescript", "tsx", "html", "css"} for f in knowledge.files) else "foundation"
    return knowledge


def index(
    root: Path, store: PostgresStore, provider: EmbeddingProvider, force: bool = False,
    refresh: bool = False
) -> dict:
    begun = perf_counter()
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Index directory does not exist or is not a directory")
    scanned, contents = scan(root)
    if not scanned.files:
        raise ValueError("No readable supported source files found; existing index preserved")
    snapshot = store.snapshot(stable_id(str(root)))
    unchanged = sum(snapshot["hashes"].get(f.path) == f.hash for f in scanned.files)
    hashes = {f.path: f.hash for f in scanned.files}
    previous = snapshot["project"]
    if (
        not force
        and not refresh
        and previous
        and previous["model"] == provider.model
        and previous["dimension"] == provider.dimension
        and previous.get("pipeline_revision") == PIPELINE_REVISION
        and hashes == snapshot["hashes"]
    ):
        event("index_unchanged", files=unchanged)
        previous_stats = previous.get("statistics", {})
        previous_warnings = [warning for report in previous_stats.get("file_reports", []) for warning in report.get("warnings", [])]
        return {"status": "unchanged", "files": unchanged, "embeddings_generated": 0,
                "embeddings_reused": 0, "warnings": [*scanned.warnings, *previous_warnings],
                **{key:value for key,value in previous_stats.items() if key != "file_reports"},
                **{key:value for key,value in scanned.statistics.items() if key != "file_reports"},
                "indexing_seconds": perf_counter() - begun}
    parse_started = perf_counter()
    knowledge = parse_repository(root, (scanned, contents))
    documents = chunks(knowledge, root.name)
    parsing_and_documents_seconds = perf_counter() - parse_started
    if not documents:
        raise ValueError(
            "No knowledge units parsed; existing index preserved. " + " ".join(knowledge.warnings)
        )
    # Revisions can change parsing/relationship metadata without changing every
    # embedding input. The store keys cached vectors by a hash of the exact text
    # plus model/dimension, so unchanged inputs remain safe to reuse across
    # revisions. This avoids needlessly re-embedding an entire large repository
    # when only a subset of documents changed.
    cached = store.cached_embeddings(stable_id(str(root)), provider.model, provider.dimension) if previous and not force else {}
    cached_keys = set(cached)
    new_inputs: dict[str, str] = {}
    for document in documents:
        embedding_input = document.embedding_text if document.embedding_text is not None else document.text
        key = sha256(embedding_input.encode()).hexdigest()
        document.metadata["embedding_hash"] = key
        if key not in cached:
            new_inputs[key] = embedding_input
    embedding_started = perf_counter()
    # Quantized local models can depend on batch composition. Keep the existing
    # structural-document batch separate from newly added integration documents.
    integration_hashes = {c.metadata["embedding_hash"] for c in documents if c.kind == "integration"}
    ordered_keys = [key for key in new_inputs if key not in integration_hashes] + [key for key in new_inputs if key in integration_hashes]
    new_inputs = {key: new_inputs[key] for key in ordered_keys}
    ordinary = [value for key, value in new_inputs.items() if key not in integration_hashes]
    integrations = [value for key, value in new_inputs.items() if key in integration_hashes]
    generated = (provider.embed(ordinary) if ordinary else []) + (provider.embed(integrations) if integrations else [])
    embedding_seconds = perf_counter() - embedding_started
    validate_vectors(generated, len(new_inputs), provider.dimension)
    cached.update(zip(new_inputs, generated, strict=True))
    vectors = [cached[document.metadata["embedding_hash"]] for document in documents]
    validate_vectors(vectors, len(documents), provider.dimension)
    embeddings = [
        Embedding(c.id, provider.model, v) for c, v in zip(documents, vectors, strict=True)
    ]
    knowledge.statistics.update({"entities": len(knowledge.entities), "relationships": len(knowledge.relationships),
                                 "chunks": len(documents), "unresolved_relationships": sum(r.target_id is None for r in knowledge.relationships),
                                 "resolved_relationships": sum(r.target_id is not None for r in knowledge.relationships)})
    persistence_started = perf_counter()
    store.replace(root, knowledge, documents, embeddings, provider.model, provider.dimension)
    return {
        "status": "indexed",
        "files": len(knowledge.files),
        "unchanged_files": unchanged,
        "added_files": len(hashes.keys() - snapshot["hashes"].keys()),
        "deleted_files": len(snapshot["hashes"].keys() - hashes.keys()),
        "changed_files": sum(path in snapshot["hashes"] and snapshot["hashes"][path] != hash_value for path,hash_value in hashes.items()),
        "entities": len(knowledge.entities),
        "relationships": len(knowledge.relationships),
        "chunks": len(documents),
        "embeddings_generated": len(generated),
        "embeddings_reused": sum(c.metadata["embedding_hash"] in cached_keys for c in documents),
        "embedding_inputs_deduplicated": len(documents) - len(new_inputs) - sum(c.metadata["embedding_hash"] in cached_keys for c in documents),
        "unresolved_relationships": sum(r.target_id is None for r in knowledge.relationships),
        **{key: value for key, value in knowledge.statistics.items() if key != "file_reports"},
        "warnings": knowledge.warnings,
        "performance": {"indexing_seconds": perf_counter()-begun,
                        "parsing_and_documents_seconds": parsing_and_documents_seconds,
                        "embedding_seconds": embedding_seconds,
                        "persistence_seconds": perf_counter()-persistence_started},
    }
