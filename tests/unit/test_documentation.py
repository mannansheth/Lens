from lens.repository.indexing import parse_repository, scan
from lens.repository.knowledge import chunks
from lens.core.models import CodeFile
from lens.parsing.documents import parse
from lens.core.models import DocumentChunk, SearchHit
from lens.search.ranking import rank
from lens.llm.answering import evidence_limit
from lens.search.retrieval import select_with_document_evidence


def test_markdown_sections_keep_headings_lines_and_freshness_unverified(tmp_path):
    (tmp_path / "README.md").write_text(
        "---\nversion: 2.1\nlast_updated: 2025-04-03\nstatus: maintained\n---\n"
        "# Guide\n\nWelcome.\n\n## Change password\n\n"
        "1. Open Profile.\n2. Select Change password.\n",
        encoding="utf-8",
    )
    scanned = scan(tmp_path)
    assert [item.language for item in scanned[0].files] == ["documentation"]
    knowledge = parse_repository(tmp_path, scanned)
    sections = [symbol for symbol in knowledge.symbols if symbol.kind == "document_section"]
    password = next(symbol for symbol in sections if symbol.name == "Change password")
    assert "Select Change password" in password.source
    assert password.line_start == 10
    assert password.metadata["document_heading"] == "Guide > Change password"
    assert password.metadata["document_version"] == "2.1"
    assert password.metadata["document_date"] == "2025-04-03"
    assert password.metadata["freshness"] == "not_verified"

    chunk = next(item for item in chunks(knowledge, "sample") if item.name == password.qualified_name)
    assert chunk.metadata["source_type"] == "documentation"
    assert "Project documentation: README.md" in chunk.embedding_text


def test_documents_outside_supported_extensions_remain_ignored(tmp_path):
    (tmp_path / "notes.log").write_text("generated log noise", encoding="utf-8")
    (tmp_path / "guide.rst").write_text("Guide\n=====\n\nUseful setup notes.", encoding="utf-8")
    knowledge, _ = scan(tmp_path)
    assert [item.path for item in knowledge.files] == ["guide.rst"]


def test_project_docs_are_indexed_even_when_gitignored(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / ".gitignore").write_text("docs/\n", encoding="utf-8")
    (docs / "dependencies.md").write_text("# Dependencies\n\nUses Express and MySQL.", encoding="utf-8")
    knowledge, contents = scan(tmp_path)
    assert [file.path for file in knowledge.files] == ["docs/dependencies.md"]
    parsed = parse_repository(tmp_path, (knowledge, contents))
    section = next(symbol for symbol in parsed.symbols if symbol.kind == "document_section")
    assert "Express and MySQL" in section.source


def test_documentation_redacts_copied_credential_assignments():
    file = CodeFile("doc", "README.md", "documentation", "hash", 60)
    parsed = parse(file, b"# Setup\nMYSQL_PASS = \"copied-secret-value\"\n")
    section = parsed.symbols[0]
    assert "copied-secret-value" not in section.source
    assert "MYSQL_PASS = [REDACTED]" in section.source


def test_matching_document_filename_beats_generic_code_keyword_matches():
    code = SearchHit(DocumentChunk("code", "code", "scripts/test-all.js::path", "constant",
        "scripts/test-all.js", "javascript", 'require("node:path")'), keyword_score=0.35)
    guide = SearchHit(DocumentChunk("guide", "guide", "Dependencies and maintenance > Backend",
        "document_section", "docs/dependencies.md", "documentation", "Backend dependencies",
        {"source_type": "documentation", "document_heading": "Dependencies and maintenance > Backend"}),
        keyword_score=0.2)
    ranked = rank([code, guide], [], query="Explain all dependencies used")
    assert next(hit for hit in ranked if hit.chunk.id == "guide").ranking_signals["document_topic_match"]
    assert next(hit for hit in ranked if hit.chunk.id == "guide").ranking_signals["document_topic_boost"] == 0.2
    assert evidence_limit("Explain all dependencies used") == 12
    assert evidence_limit("Where is the login form defined?") == 5
    selected = select_with_document_evidence([code, code, code, code, code], 5, ranked)
    assert any(hit.chunk.id == "guide" for hit in selected)
    assert any(hit.chunk.id == "code" for hit in selected)
