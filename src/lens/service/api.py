"""Small, dependency-free JSON API for local Lens deployments.

The API intentionally delegates to the same indexing, retrieval, validation,
and SQL-generation functions used by the CLI. It never executes generated SQL.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlparse

from lens.core.config import Settings
from lens.core.models import stable_id
from lens.database.store import PostgresStore
from lens.repository.indexing import index
from lens.search.embeddings import LocalEmbeddingProvider


class LensService:
    def __init__(self, root: Path | None = None):
        self.settings = Settings.load()
        configured_root = root or Path(os.getenv("LENS_PROJECT_PATH", "."))
        self.root = configured_root.resolve()
        if not self.root.is_dir():
            raise ValueError("LENS_PROJECT_PATH must name an existing directory")
        self.project = stable_id(str(self.root))
        self.store = PostgresStore(self.settings.database_url)
        self.store.snapshot(self.project)
        self.embeddings = LocalEmbeddingProvider(self.settings)
        self._llm = None
        self._provider_lock = threading.Lock()
        self._index_lock = threading.Lock()

    def provider(self):
        with self._provider_lock:
            if self._llm is None:
                from lens.llm.models import create_provider, load_settings
                self._llm = create_provider(load_settings())
            return self._llm

    def index(self, refresh=False, force=False):
        with self._index_lock:
            return index(self.root, self.store, self.embeddings, force=force, refresh=refresh)

    def ask(self, query, mode="client"):
        from lens.llm.answering import ask
        self.store.check_model(self.embeddings.model, self.embeddings.dimension, self.project)
        llm = self.provider()
        answer = ask(query, self.store, self.embeddings, llm, self.project,
                     mode=mode, context_chars=llm.settings.context_chars,
                     alpha=self.settings.hybrid_alpha)
        return answer.to_dict()

    def sql(self, question, variable=None, config_file=None, env_file=None,
            user_id=None, record_scope=None):
        from lens.database.application import discover, generate_sql, inspect_schema
        env_file = self._project_file(env_file)
        config_file = self._project_file(config_file)
        connection = discover(self.root, variable, env_file, config_file)
        schema = inspect_schema(connection, include_routine_definitions=True)
        sql = generate_sql(question, schema, self.provider(), user_id=user_id,
                           record_scope=record_scope)
        return {"sql": sql, "dialect": connection.dialect,
                "execution": "not_performed", "validated_read_only": True}

    def _project_file(self, value):
        if value is None:
            return None
        path = (self.root / value).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Configuration files must be existing files inside LENS_PROJECT_PATH")
        return path


def _loopback(host):
    return host.lower() in {"127.0.0.1", "localhost", "::1"}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_handler(service, api_key=""):
    """Create an HTTP handler bound to one service and its configured key."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "Lens/0.6"
        sys_version = ""

        def log_message(self, fmt, *args):
            from lens.core.logging import event
            event("http_request", client=self.client_address[0], message=fmt % args)

        def _send(self, status, data):
            raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self):
            if not api_key:
                return _loopback(self.server.server_address[0])
            supplied = self.headers.get("Authorization", "")
            supplied = supplied[7:] if supplied.startswith("Bearer ") else self.headers.get("X-Lens-Key", "")
            return hmac.compare_digest(supplied, api_key)

        def do_GET(self):
            if urlparse(self.path).path != "/health":
                self._send(404, {"error": "not_found"})
                return
            self._send(200, {"status": "ready", "project_id": service.project})

        def do_POST(self):
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            route = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 1_048_576:
                    raise ValueError("Request body must be between 2 bytes and 1 MiB")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON request body must be an object")
                if route == "/v1/ask":
                    query = payload.get("question")
                    if not isinstance(query, str):
                        raise ValueError("question must be a string")
                    mode = payload.get("mode", "client")
                    if mode not in {"client", "developer", "user"}:
                        raise ValueError("mode must be client, developer, or user")
                    self._send(200, service.ask(query, mode))
                elif route == "/v1/sql":
                    question = payload.get("question")
                    if not isinstance(question, str):
                        raise ValueError("question must be a string")
                    for name in ("variable", "config_file", "env_file"):
                        if payload.get(name) is not None and not isinstance(payload[name], str):
                            raise ValueError(f"{name} must be a string")
                    user_id = payload.get("user_id")
                    if user_id is not None and (type(user_id) is not int or user_id <= 0):
                        raise ValueError("user_id must be a positive integer")
                    record_scope = payload.get("record_scope")
                    if record_scope is not None and (
                        not isinstance(record_scope, str)
                        or record_scope not in {"user_records_only", "all_records"}
                        or user_id is None
                    ):
                        raise ValueError("record_scope requires user_id and must be user_records_only or all_records")
                    self._send(200, service.sql(
                        question,
                        variable=payload.get("variable"),
                        config_file=payload.get("config_file"),
                        env_file=payload.get("env_file"),
                        user_id=user_id,
                        record_scope=record_scope,
                    ))
                elif route == "/v1/index":
                    refresh, force = payload.get("refresh", False), payload.get("force", False)
                    if not isinstance(refresh, bool) or not isinstance(force, bool):
                        raise ValueError("refresh and force must be booleans")
                    self._send(200, service.index(refresh, force))
                else:
                    self._send(404, {"error": "not_found"})
            except ValueError as exc:
                self._send(400, {"error": str(exc)[:500]})
            except (RuntimeError, OSError) as exc:
                from lens.core.logging import event
                event("service_operation_failed", error_type=type(exc).__name__)
                self._send(503, {"error": "service_unavailable"})
            except Exception:
                self._send(500, {"error": "internal_error"})

    return Handler


def serve(host="127.0.0.1", port=8765, root=None):
    """Serve local SDK requests; non-loopback binding requires LENS_API_KEY."""
    api_key = os.getenv("LENS_API_KEY", "")
    if not _loopback(host) and not api_key:
        raise ValueError("LENS_API_KEY is required when binding Lens beyond loopback")
    service = LensService(root)
    server = _Server((host, port), create_handler(service, api_key))
    from lens.core.logging import event
    event("service_started", host=host, port=port, project=str(service.root))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
