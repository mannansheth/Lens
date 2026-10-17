import pytest
from lens.core.config import Settings
from lens.commands.cli import main
from lens.repository.knowledge import lexical_text
from lens.database.store import PostgresStore


def test_query_identifier_expansion():
    expanded = lexical_text(
        "Demo.InvoiceController.GetInvoices HTTPClient FetchAndInsertINumber_SP"
    )
    assert "Demo Invoice Controller Get Invoices" in expanded
    assert "HTTP Client" in expanded
    assert "SP" in expanded


def test_config_env_validation_and_secret_repr(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@localhost/lens")
    monkeypatch.setenv("HYBRID_ALPHA", "0.7")
    assert Settings.load().hybrid_alpha == 0.7
    assert "secret" not in repr(Settings.load())
    monkeypatch.setenv("HYBRID_ALPHA", "2")
    with pytest.raises(ValueError):
        Settings.load()


def test_missing_database_and_parse_directory(capsys, tmp_path):
    with pytest.raises(ValueError, match="DATABASE_URL"):
        PostgresStore("")
    assert main(["parse", str(tmp_path / "missing")]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_configuration_and_relative_cache_are_independent_of_project_cwd(tmp_path, monkeypatch):
    config = tmp_path / "lens.env"
    config.write_text("EMBEDDING_CACHE_DIR=.models\nDATABASE_URL=local-test\n")
    monkeypatch.setenv("LENS_ENV_FILE", str(config))
    monkeypatch.setenv("EMBEDDING_CACHE_DIR", ".models")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("EMBEDDING_CACHE_DIR=wrong\n")
    monkeypatch.chdir(repo)
    assert Settings.load().embedding_cache_dir == str((tmp_path / ".models").resolve())


def test_cli_projects_switch_search_override_cwd_and_json(tmp_path, monkeypatch, capsys):
    from lens.core.models import stable_id, DocumentChunk, SearchHit
    import lens.commands.cli as cli
    import json
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
    projects = [{"id": stable_id(str(root.resolve())), "root": str(root.resolve())} for root in roots]
    calls = []
    class Store:
        def projects(self):
            return projects
        def keyword_search(self, query, limit, project):
            calls.append(project)
            return [SearchHit(DocumentChunk("c", "e", "needle", "function", "code.js", "javascript", "source"), keyword_score=1)]
    monkeypatch.setattr(cli, "PostgresStore", lambda url: Store())
    monkeypatch.setenv("LENS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    assert main(["project", "use", str(roots[0])]) == 0
    capsys.readouterr()
    assert main(["search", "needle", "--mode", "keyword", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["item"] == "needle"
    assert calls[-1] == projects[0]["id"]
    monkeypatch.chdir(roots[1])
    assert main(["search", "needle", "--mode", "keyword"]) == 0
    assert "1. needle" in capsys.readouterr().out
    # Explicit project selection persists across working-directory changes.
    assert calls[-1] == projects[0]["id"]
    assert main(["search", "needle", "--mode", "keyword", "--project", str(roots[0])]) == 0
    assert calls[-1] == projects[0]["id"]
    capsys.readouterr()
    assert main(["project", "use", str(roots[1])]) == 0
    capsys.readouterr()
    assert main(["project", "show"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == projects[1]["id"]
    assert main(["project", "list"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert main(["search", "needle", "--mode", "keyword", "--project", str(tmp_path / "missing")]) == 1
    assert "not indexed" in capsys.readouterr().err
