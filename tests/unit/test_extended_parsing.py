from lens.repository.indexing import parse_repository
from lens.core.models import CodeFile, stable_id
from lens.parsing import python, configuration, razor


def file(path, language):
    return CodeFile(stable_id(path), path, language, '', 0)


def test_python_ast_locations_calls_routes_and_environment():
    parsed = python.parse(file('service.py', 'python'), b'''import os
from flask import Flask
app = Flask(__name__)
@app.post("/analyze")
def analyze(request: dict) -> dict:
    model = os.getenv("MODEL_NAME")
    return predict(request)
def predict(value):
    return value
''')
    function = next(s for s in parsed.symbols if s.name == 'analyze')
    assert (function.line_start, function.line_end) == (5, 7)
    assert function.metadata['routes'] == [{'path': '/analyze', 'method': 'POST', 'renders': []}]
    assert function.metadata['environment_variables'] == ['MODEL_NAME']
    assert any(r.source_id == function.id and r.kind == 'CALLS' and r.target_name == 'predict' for r in parsed.relationships)
    assert function.metadata['parameters'] == [{'name': 'request', 'type': 'dict'}]


def test_configuration_never_retains_values():
    secret = 'sensitive-fixture-value'
    for path, source in [('.env.production', f'PASSWORD={secret}\nDATABASE_URL=${{DB_HOST}}\n'),
                         ('appsettings.json', '{"ConnectionStrings":{"Default":"' + secret + '"}}'),
                         ('settings.toml', '[db]\npassword="' + secret + '"')]:
        parsed = configuration.parse(file(path, 'configuration'), source.encode())
        assert parsed.symbols
        assert secret not in repr(parsed)
        assert all(s.metadata['value_redacted'] for s in parsed.symbols)
    parsed = configuration.parse(file('.env', 'configuration'), b'DATABASE_URL=${DB_HOST}\n')
    assert parsed.symbols[0].metadata['environment_variables'] == ['DB_HOST']


def test_razor_view_model_and_form_helpers():
    parsed = razor.parse(file('Views/Journal/Create.cshtml', 'razor'), b'''@model App.Models.JournalEntry
<form asp-controller="Journal" asp-action="Create" method="post">
<input asp-for="Content" />
</form>
<p>@Model.Content</p>''')
    view = parsed.symbols[0]
    assert view.metadata['model'] == 'App.Models.JournalEntry'
    assert view.metadata['forms'][0]['method'] == 'POST'
    assert view.metadata['model_references'] == ['Content']
    assert any(r.kind == 'SUBMITS_TO' and r.target_name == 'Journal.Create' for r in parsed.relationships)


def test_scan_env_and_parser_failure_isolation(tmp_path, monkeypatch):
    (tmp_path / '.env.local').write_text('PASSWORD=private-value')
    (tmp_path / 'broken.py').write_text('not valid python !!!')
    (tmp_path / 'good.py').write_text('def good():\n    return 1\n')
    knowledge = parse_repository(tmp_path)
    assert len(knowledge.files) == 3
    assert any(s.name == 'good' for s in knowledge.symbols)
    assert 'private-value' not in repr(knowledge)
    assert any('Python syntax error' in warning for warning in knowledge.warnings)
    import lens.repository.indexing as indexing
    monkeypatch.setitem(indexing.PARSERS, '.py', ('python', lambda *_: (_ for _ in ()).throw(RuntimeError('secret'))))
    parsed = parse_repository(tmp_path)
    assert sum(report['status'] == 'failed' for report in parsed.statistics['file_reports']) == 2
    assert 'secret' not in repr(parsed)
