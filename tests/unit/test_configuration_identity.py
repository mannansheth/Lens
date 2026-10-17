"""Repeated YAML structures preserve distinct identities and evidence locations."""
from lens.core.models import CodeFile
from lens.parsing.configuration import parse


def test_repeated_yaml_steps_have_unique_ids_and_actual_lines():
    source = '''jobs:
  verify:
    steps:
      - name: First
        with:
          python-version: "3.13"
        env:
          PROJECT_TEST_PYTHON: first-private-value
      - name: Second
        with:
          python-version: "3.13"
        env:
          PROJECT_TEST_PYTHON: second-private-value
'''
    file = CodeFile('workflow', '.github/workflows/test.yml', 'configuration', '', len(source))
    knowledge = parse(file, source.encode())
    assert len({symbol.id for symbol in knowledge.symbols}) == len(knowledge.symbols)
    assert len({symbol.qualified_name for symbol in knowledge.symbols}) == len(knowledge.symbols)
    variables = [symbol for symbol in knowledge.symbols if symbol.name.endswith('PROJECT_TEST_PYTHON')]
    assert [symbol.line_start for symbol in variables] == [8, 13]
    assert [symbol.line_start for symbol in knowledge.symbols if symbol.name.endswith('.with')] == [5, 10]
    assert all(symbol.line_start == symbol.line_end for symbol in knowledge.symbols)
    assert 'private-value' not in repr(knowledge)
    assert [symbol.id for symbol in parse(file, source.encode()).symbols] == [symbol.id for symbol in knowledge.symbols]
