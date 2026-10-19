import pytest
from http.server import HTTPServer
import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from lens.service.api import LensService, _loopback, create_handler, serve


def test_non_loopback_service_requires_api_key(monkeypatch):
    monkeypatch.delenv("LENS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LENS_API_KEY is required"):
        serve("0.0.0.0", 8765)


def test_project_config_paths_are_confined_to_repository(tmp_path):
    service = LensService.__new__(LensService)
    service.root = tmp_path.resolve()
    config = tmp_path / "appsettings.json"
    config.write_text("{}", encoding="utf-8")
    assert service._project_file("appsettings.json") == config.resolve()
    with pytest.raises(ValueError, match="inside LENS_PROJECT_PATH"):
        service._project_file("../outside.json")
    with pytest.raises(ValueError, match="inside LENS_PROJECT_PATH"):
        service._project_file("missing.json")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_recognition(host):
    assert _loopback(host)


def test_other_bind_is_not_loopback():
    assert not _loopback("0.0.0.0")


def test_http_sql_route_returns_sql_without_execution():
    class FakeService:
        project = "opaque-project-id"

        def sql(self, question, variable=None, config_file=None, env_file=None, user_id=None, record_scope=None):
            assert question == "Count accounts"
            assert user_id == 17
            assert record_scope == "user_records_only"
            return {"sql": "SELECT COUNT(id) FROM accounts", "dialect": "mysql",
                    "execution": "not_performed", "validated_read_only": True}

    server = HTTPServer(("127.0.0.1", 0), create_handler(FakeService(), "api-secret"))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    body = json.dumps({"question": "Count accounts", "user_id": 17,
                       "record_scope": "user_records_only"}).encode()
    try:
        denied = Request(base + "/v1/sql", data=body, headers={"Content-Type": "application/json"})
        with pytest.raises(HTTPError) as error:
            urlopen(denied, timeout=2)
        assert error.value.code == 401
        request = Request(base + "/v1/sql", data=body,
                          headers={"Content-Type": "application/json", "Authorization": "Bearer api-secret"})
        with urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
        assert result == {"sql": "SELECT COUNT(id) FROM accounts", "dialect": "mysql",
                          "execution": "not_performed", "validated_read_only": True}
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_http_sql_route_rejects_invalid_user_id():
    class FakeService:
        project = "opaque-project-id"
        def sql(self, *args, **kwargs):
            raise AssertionError("invalid user IDs must be rejected before SQL generation")

    server = HTTPServer(("127.0.0.1", 0), create_handler(FakeService(), "api-secret"))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    body = json.dumps({"question": "Count accounts", "user_id": True}).encode()
    request = Request(f"http://127.0.0.1:{server.server_port}/v1/sql", data=body,
                      headers={"Content-Type": "application/json", "Authorization": "Bearer api-secret"})
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=2)
        assert error.value.code == 400
        invalid_scope = Request(
            f"http://127.0.0.1:{server.server_port}/v1/sql",
            data=json.dumps({"question": "Count accounts", "user_id": 17,
                             "record_scope": []}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer api-secret"},
        )
        with pytest.raises(HTTPError) as scope_error:
            urlopen(invalid_scope, timeout=2)
        assert scope_error.value.code == 400
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
