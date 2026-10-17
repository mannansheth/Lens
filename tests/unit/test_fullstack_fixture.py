"""Permanent representative sources exercise cross-language application evidence."""
from pathlib import Path
from lens.commands.cli import main
from lens.repository.indexing import parse_repository
from lens.repository.knowledge import chunks

FIXTURE = Path(__file__).resolve().parents[2] / 'examples' / 'fullstack-app'


def test_fullstack_fixture_has_complementary_resolved_evidence():
    knowledge = parse_repository(FIXTURE)
    assert not knowledge.warnings
    assert {file.language for file in knowledge.files} >= {'javascript', 'csharp', 'python', 'razor', 'mysql', 'configuration'}
    by_id = {entity.id: entity for entity in knowledge.entities}
    resolved = [edge for edge in knowledge.relationships if edge.target_id]
    assert any(edge.kind == 'REQUESTS' and edge.target_name == 'POST /api/journal' for edge in resolved)
    assert any(edge.kind == 'REQUESTS' and 'analyze' in edge.target_name for edge in resolved)
    assert any(edge.kind == 'CALLS' and edge.target_name == 'Save' for edge in resolved)
    assert any(edge.kind == 'CALLS' and by_id[edge.target_id].name == 'score' for edge in resolved)
    assert any(edge.kind == 'WRITES' and by_id[edge.target_id].name == 'journal_entries' for edge in resolved)
    assert any(edge.kind == 'USES_MODEL' and by_id[edge.target_id].name == 'JournalEntry' for edge in resolved)
    assert any(edge.kind == 'REQUESTS' and edge.metadata.get('rule') == 'razor_form_controller_action' for edge in resolved)
    documents = chunks(knowledge, 'fullstack-app')
    assert {document.language for document in documents} >= {'javascript', 'csharp', 'python', 'razor', 'mysql', 'configuration'}
    assert all(document.metadata['line_start'] >= 1 for document in documents)
    assert 'fixture-sentiment-stub' not in repr(documents)


def test_fullstack_fixture_cli_parse(capsys):
    assert main(['parse', str(FIXTURE)]) == 0
    output = capsys.readouterr().out
    assert 'journal_entries' in output
    assert 'fixture-sentiment-stub' not in output


def test_configuration_resource_limits(tmp_path):
    import json
    from lens.parsing import configuration
    from lens.core.models import CodeFile
    source = json.dumps({f'key{index}': index for index in range(2100)}).encode()
    parsed = configuration.parse(CodeFile('config', 'settings.json', 'configuration', '', len(source)), source)
    assert len(parsed.symbols) == 2000
    assert any('truncated' in warning for warning in parsed.warnings)
    (tmp_path / 'oversized-settings.json').write_bytes(b' ' * 1_000_001)
    (tmp_path / 'package-lock.json').write_text('{"lockfileVersion": 3}')
    (tmp_path / 'ok.py').write_text('def ok():\n    return 1\n')
    knowledge = parse_repository(tmp_path)
    assert [file.path for file in knowledge.files] == ['ok.py']
    assert any('exceeds 1 MB' in warning for warning in knowledge.warnings)


def test_json_datasets_are_ignored_but_configuration_is_indexed(tmp_path):
    (tmp_path / 'archive').mkdir()
    (tmp_path / 'archive' / 'data.json').write_text('{"records": [{"id": 1}]}')
    (tmp_path / 'appsettings.json').write_text('{"Database": {"Host": "placeholder"}}')
    (tmp_path / 'custom-settings.json').write_text('{"Feature": true}')
    (tmp_path / 'array-settings.json').write_text('[{"id": 1}]')
    knowledge = parse_repository(tmp_path)
    assert {file.path for file in knowledge.files} == {'appsettings.json', 'custom-settings.json', 'array-settings.json'}
    assert {symbol.name for symbol in knowledge.symbols if symbol.kind == 'configuration'} == {'Database.Host', 'Feature'}
    assert any('data.json: JSON dataset' in warning for warning in knowledge.warnings)
    assert any('JSON root' in warning for warning in knowledge.warnings)
