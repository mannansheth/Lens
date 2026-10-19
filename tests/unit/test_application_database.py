import json
import sqlite3
from types import SimpleNamespace

import pytest
from lens.database.application import (Connection, discover, inspect_schema, validate_sql,
                                        execute, sql_workflow)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "app.db"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT); CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), amount REAL); INSERT INTO customers VALUES(1,'Ada'); INSERT INTO orders VALUES(1,1,20),(2,1,30);")
    (tmp_path / '.env').write_text('BASE=app.db\nAPP_DATABASE_URL=sqlite:///${BASE}\nAPI_KEY=not-a-real-secret\n')
    return discover(tmp_path)


def test_discovery_and_schema(database):
    assert 'app.db' not in repr(database)
    schema = inspect_schema(database)
    assert schema['dialect'] == 'sqlite'
    assert len(schema['tables']) == 2
    orders = next(t for t in schema['tables'] if t['name'] == 'orders')
    assert orders['foreign_keys'][0]['target_table'] == 'customers'
    assert orders['columns'][0]['primary_key']


def test_execution_and_cte(database):
    schema = inspect_schema(database)
    result = execute(database, 'WITH x AS (SELECT amount FROM orders) SELECT SUM(amount) AS total FROM x', schema)
    assert result['rows'] == [['50.0']]
    assert execute(database, 'SELECT * FROM orders', schema, 1)['truncated']


def test_read_only_union_is_one_valid_select_statement(database):
    schema = inspect_schema(database)
    query = ('SELECT amount AS value FROM orders WHERE id = 1 '
             'UNION ALL SELECT amount FROM orders WHERE id = 2')
    assert validate_sql(query, schema) == query
    assert execute(database, query, schema)['rows'] == [['20.0'], ['30.0']]
    with pytest.raises(ValueError, match='single read-only SELECT'):
        validate_sql(query + '; SELECT name FROM customers', schema)


@pytest.mark.parametrize('sql', [
    'DELETE FROM orders', 'SELECT * FROM orders; SELECT * FROM customers',
    'WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x',
    'SELECT load_extension(\'x\')', 'SELECT pg_sleep(100)',
    'SELECT * INTO x FROM orders', 'SELECT * FROM imaginary',
    'SELECT missing_column FROM orders', 'PRAGMA query_only=OFF',
    'SELECT * FROM orders FOR UPDATE', 'SELECT * FROM other.orders',
    "SELECT 'unterminated",
])
def test_reject_unsafe(database, sql):
    with pytest.raises(ValueError):
        validate_sql(sql, inspect_schema(database))


def test_confirmation(database):
    provider = SimpleNamespace(generate=lambda *a: SimpleNamespace(text=json.dumps({'sql': 'SELECT amount FROM orders', 'limitation': ''})))
    seen = []
    result = sql_workflow(database, 'amounts', provider, confirm=lambda _: 'no', display=seen.append)
    assert not result['executed'] and seen
    result = sql_workflow(database, 'amounts', provider, confirm=lambda _: 'yes', display=seen.append, row_limit=1)
    assert result['executed'] and len(result['rows']) == 1


def test_failures_redacted(tmp_path):
    with pytest.raises(ValueError, match='No unambiguous'):
        discover(tmp_path)
    config = Connection('sqlite', 'sqlite:///missing-secret.db', tmp_path)
    with pytest.raises(ValueError) as error:
        inspect_schema(config)
    assert 'missing-secret' not in str(error.value)


def test_cli_schema(database, capsys):
    from lens.commands.cli import main
    assert main(['db-schema', '--project', str(database.root)]) == 0
    assert 'customers' in capsys.readouterr().out


def test_credential_columns_and_qualified_functions(database):
    schema = inspect_schema(database)
    schema['tables'][0]['columns'].append({'name': 'password', 'type': 'TEXT'})
    assert validate_sql('SELECT COUNT(*) FROM customers', schema)
    assert validate_sql('SELECT * FROM orders', schema)
    for sql in ('SELECT * FROM customers', 'SELECT password AS value FROM customers',
                'SELECT public.abs(amount) FROM orders'):
        with pytest.raises(ValueError):
            validate_sql(sql, schema)


def test_timeout_and_output_budget(database):
    schema = inspect_schema(database)
    # A recursive CTE is allowed but execution still observes the deadline.
    from unittest.mock import patch
    with patch('lens.database.application.time.monotonic', side_effect=[0, 10]):
        with pytest.raises(ValueError, match='operation failed'):
            execute(database, 'WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT SUM(n) FROM x', schema)


def test_cli_sql_confirm_decline_and_execute(database, monkeypatch, capsys):
    from lens.commands.cli import main
    import lens.llm.models as models
    provider = SimpleNamespace(generate=lambda *a: SimpleNamespace(text=json.dumps({
        'sql': 'SELECT SUM(amount) AS total FROM orders', 'limitation': ''})))
    monkeypatch.setattr(models, 'create_provider', lambda _: provider)
    monkeypatch.setattr(models, 'load_settings', lambda *a: None)
    # sql_workflow binds its default input at definition time; replace the workflow
    # at the module boundary to inject terminal replies while retaining all logic.
    import lens.database.application as application
    original = application.sql_workflow
    for reply, executed in [('no', False), ('yes', True)]:
        monkeypatch.setattr(application, 'sql_workflow',
            lambda *a, **kw: original(*a, confirm=lambda _: reply, **kw))
        assert main(['sql', 'total amount', '--project', str(database.root)]) == 0
        output = capsys.readouterr().out
        assert 'SELECT\n  SUM(amount)' in output
        if executed:
            assert '50' in output and '1 row' in output
        else:
            assert 'Query was not executed.' in output


def test_database_enforces_readonly_and_total_output_budget(database):
    from lens.database.application import connect
    with pytest.raises(ValueError, match='operation failed'):
        with connect(database) as db:
            db.execute('DELETE FROM orders')
    schema = inspect_schema(database)
    literal = 'x' * 900
    result = execute(database, f"WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<100) SELECT '{literal}' AS value FROM x", schema)
    assert result['truncated'] and len(result['rows']) < 100
    assert len(json.dumps(result['rows'])) < 51000
    assert execute(database, 'SELECT COUNT(id) FROM orders', schema)['rows'] == [['2']]


def test_generation_receives_actual_schema_without_configuration(database):
    from lens.database.application import generate_sql
    seen = []
    def generate(messages, schema):
        seen.append(messages)
        return SimpleNamespace(text=json.dumps({'sql': 'SELECT SUM(amount) FROM orders', 'limitation': ''}))
    query = generate_sql('total amount', inspect_schema(database), SimpleNamespace(generate=generate))
    assert 'orders' in json.dumps(seen) and 'customer_id' in json.dumps(seen)
    assert 'API_KEY' not in json.dumps(seen) and '.db' not in json.dumps(seen)
    assert query == 'SELECT SUM(amount) FROM orders'
    with pytest.raises(ValueError, match='invalid SQL JSON'):
        generate_sql('total amount', inspect_schema(database), SimpleNamespace(generate=lambda *a: SimpleNamespace(text='bad')))


def test_sql_generation_receives_authenticated_user_as_structured_context(database):
    from lens.database.application import generate_sql
    prompts = []
    def generate(messages, schema):
        prompts.append(messages[0]['content'])
        return SimpleNamespace(text=json.dumps({'sql': 'SELECT SUM(amount) FROM orders', 'limitation': ''}))
    provider = SimpleNamespace(generate=generate)
    actual_schema = inspect_schema(database)
    generate_sql('my total amount', actual_schema, provider, user_id=7,
                 record_scope='user_records_only')
    generate_sql('total amount', actual_schema, provider, user_id=2,
                 record_scope='all_records')
    assert '"authenticated_user": {"id": 7, "record_scope": "user_records_only"}' in prompts[0]
    assert '"authenticated_user": {"id": 2, "record_scope": "all_records"}' in prompts[1]
    with pytest.raises(ValueError, match='positive integer'):
        generate_sql('total amount', actual_schema, provider, user_id=True)


def test_sql_prefers_foreign_key_identity_over_matching_id_columns():
    from lens.database.application import validate_sql
    schema = {'dialect': 'mysql', 'tables': [
        {'schema': 'fixture', 'name': 'appointments', 'columns': [
            {'name': 'id', 'type': 'INT'}, {'name': 'therapistID', 'type': 'INT'}],
         'foreign_keys': [{'column': 'therapistID', 'target_table': 'users', 'target_column': 'ID'}]},
        {'schema': 'fixture', 'name': 'therapists', 'columns': [
            {'name': 'id', 'type': 'INT'}, {'name': 'userID', 'type': 'INT'}],
         'foreign_keys': [{'column': 'userID', 'target_table': 'users', 'target_column': 'ID'}]},
        {'schema': 'fixture', 'name': 'users', 'columns': [
            {'name': 'ID', 'type': 'INT'}, {'name': 'Name', 'type': 'TEXT'}], 'foreign_keys': []},
    ]}
    correct = ('SELECT u.Name FROM appointments a '
               'JOIN therapists t ON a.therapistID=t.userID '
               'JOIN users u ON t.userID=u.ID')
    accidental_id_join = ('SELECT u.Name FROM appointments a '
                          'JOIN therapists t ON a.therapistID=t.id '
                          'JOIN users u ON t.userID=u.ID')
    assert validate_sql(correct, schema) == correct
    with pytest.raises(ValueError, match='bypasses a declared foreign-key relationship'):
        validate_sql(accidental_id_join, schema)
