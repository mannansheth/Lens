"""MySQL split configuration and native command behavior; no app data fixtures."""
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from lens.database.application import (discover, inspect_schema, execute, validate_sql,
    execute_mysql_command, check_database, sql_workflow)


def test_split_mysql_configuration_and_nested_env(tmp_path):
    (tmp_path / '.env').write_text('FRONTEND_PORT=3000\n')
    backend = tmp_path / 'backend'
    backend.mkdir()
    (backend / '.env').write_text('MYSQL_HOST = "localhost"\nMYSQL_USER = "root"\nMYSQL_PASS = ""\nMYSQL_DB = "fixture_db"\n')
    config = discover(tmp_path)
    assert config.dialect == 'mysql'
    assert config.parameters == dict(host='localhost', user='root', password='', database='fixture_db', port=3306)
    assert 'fixture_db' not in repr(config)


def test_aliases_interpolation_url_and_ambiguity(tmp_path):
    path = tmp_path / '.env'
    path.write_text('DB_HOST=localhost\nDB_USERNAME=fixture\nDB_PASSWORD=${PASS}\nPASS=fake-fixture-password\nDB_DATABASE=fixture\nDB_PORT=3307\n')
    config = discover(tmp_path)
    assert config.parameters['password'] == 'fake-fixture-password'
    assert config.parameters['port'] == 3307
    path.write_text('DATABASE_URL=mysql://fixture:fake%23pass@localhost:3307/fixture\n')
    assert discover(tmp_path).parameters['password'] == 'fake#pass'
    path.write_text('MYSQL_HOST=localhost\nMYSQL_USER=root\nMYSQL_DB=fixture\nMYSQL_PORT=oops\n')
    with pytest.raises(ValueError, match='port'):
        discover(tmp_path)


SCHEMA = {'dialect': 'mysql', 'tables': [{'schema': 'fixture', 'name': 'orders', 'columns': [
    {'name': 'id', 'type': 'int'}, {'name': 'amount', 'type': 'decimal(10,2)'}]}]}


@pytest.mark.parametrize('query', ['SELECT /*!50000 SLEEP(10) */ 1', 'SELECT /*M! SLEEP(10) */ 1',
    'SELECT /*+ MAX_EXECUTION_TIME(0) */ amount FROM orders', "SELECT amount INTO OUTFILE 'fixture.txt' FROM orders",
    'SELECT SLEEP(10)', 'SELECT GET_LOCK(\'x\',10)', 'DELETE FROM orders'])
def test_mysql_rejects_side_effects(query):
    with pytest.raises(ValueError):
        validate_sql(query, SCHEMA)


def test_native_command_credentials_off_arguments_and_file_cleanup(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('MYSQL_HOST=localhost\nMYSQL_USER=fixture\nMYSQL_PASS=fake-test-secret\nMYSQL_DB=fixture\n')
    config = discover(tmp_path)
    import lens.database.application as module
    monkeypatch.setattr(module, 'mysql_client', lambda: 'mysql.exe')
    monkeypatch.setattr(module, 'check_database', lambda _: {'server_version': '8.0.0'})
    seen = []
    def run(args, **kwargs):
        seen.append(args[1].split('=', 1)[1])
        assert Path(seen[-1]).exists()
        assert 'fake-test-secret' not in json.dumps(args)
        assert 'fake-test-secret' not in kwargs['input']
        assert kwargs['shell'] is False and kwargs['timeout'] == 12
        assert 'START TRANSACTION READ ONLY' in kwargs['input']
        return SimpleNamespace(returncode=0, stdout='total\n50\n')
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert execute_mysql_command(config, 'SELECT SUM(amount) AS total FROM orders', SCHEMA)['rows'] == [['50']]
    assert not Path(seen[0]).exists()


@pytest.fixture
def mysql_fixture(tmp_path):
    path = os.getenv('LENS_TEST_MYSQL_ENV_FILE')
    if not path:
        pytest.skip('Set LENS_TEST_MYSQL_ENV_FILE to enable disposable MySQL integration tests')
    import pymysql
    config = discover(tmp_path, env_file=Path(path))
    name = 'lens_test_' + uuid4().hex[:12]
    admin = pymysql.connect(**config.parameters, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute('CREATE DATABASE `' + name + '`')
        test_config = replace(config, parameters={**config.parameters, 'database': name})
        fixture = pymysql.connect(**test_config.parameters, autocommit=True)
        with fixture:
            with fixture.cursor() as cur:
                cur.execute('CREATE TABLE customers(id INT PRIMARY KEY, name VARCHAR(100))')
                cur.execute('CREATE TABLE orders(id INT PRIMARY KEY, customer_id INT, amount DECIMAL(10,2), FOREIGN KEY(customer_id) REFERENCES customers(id))')
                cur.execute("INSERT INTO customers VALUES(1,'Fixture')")
                cur.execute('INSERT INTO orders VALUES(1,1,20),(2,1,30)')
        yield test_config
    finally:
        with admin.cursor() as cur:
            cur.execute('DROP DATABASE IF EXISTS `' + name + '`')
        admin.close()


def test_live_mysql_schema_and_bounded_execution(mysql_fixture):
    schema = inspect_schema(mysql_fixture)
    assert len(schema['tables']) == 2
    orders = next(t for t in schema['tables'] if t['name'] == 'orders')
    assert orders['columns'][0]['primary_key'] and orders['foreign_keys'] and orders['indexes']
    assert check_database(mysql_fixture)['running']
    for runner in (execute, execute_mysql_command):
        result = runner(mysql_fixture, 'SELECT SUM(amount) AS total FROM orders', schema)
        assert result['rows'] == [['50.00']]
        result = runner(mysql_fixture, 'WITH x AS (SELECT amount FROM orders) SELECT amount FROM x', schema, 1)
        assert len(result['rows']) == 1 and result['truncated']


def test_live_mysql_requires_confirmation(mysql_fixture):
    provider = SimpleNamespace(generate=lambda *a: SimpleNamespace(text=json.dumps({'sql': 'SELECT SUM(amount) AS total FROM orders', 'limitation': ''})))
    for executor in ('driver', 'mysql'):
        assert not sql_workflow(mysql_fixture, 'total', provider, confirm=lambda _: 'no', display=lambda _: None, executor=executor)['executed']
        result = sql_workflow(mysql_fixture, 'total', provider, confirm=lambda _: 'yes', display=lambda _: None, executor=executor)
        assert result['executed'] and result['rows'] == [['50.00']]


def test_live_mysql_readonly_and_timeout(mysql_fixture):
    import time
    from lens.database.application import connect
    with pytest.raises(ValueError, match='operation failed'):
        with connect(mysql_fixture) as db:
            with db.cursor() as cur:
                cur.execute('DELETE FROM orders')
    started = time.monotonic()
    with pytest.raises(ValueError, match='operation failed'):
        with connect(mysql_fixture) as db:
            with db.cursor() as cur:
                cur.execute('SELECT SLEEP(10)')
    assert time.monotonic() - started < 8
    schema = inspect_schema(mysql_fixture)
    assert execute(mysql_fixture, 'SELECT COUNT(*) FROM orders', schema)['rows'] == [['2']]
