import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from lens_client import LensClient


def test_sql_client_sends_question_and_returns_only_server_contract():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["authorization"] = self.headers.get("Authorization")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.dumps({"sql": "SELECT id FROM users", "dialect": "mysql",
                                  "execution": "not_performed", "validated_read_only": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = LensClient(f"http://127.0.0.1:{server.server_port}", api_key="private")
        result = client.sql("Count users", user_id=17, record_scope="user_records_only")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert seen == {"path": "/v1/sql", "authorization": "Bearer private",
                    "body": {"question": "Count users", "user_id": 17,
                             "record_scope": "user_records_only"}}
    assert result["sql"] == "SELECT id FROM users"
    assert result["execution"] == "not_performed"
