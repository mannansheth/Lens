"""Dependency-free client for the local Lens HTTP API."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

__version__ = "0.1.0"


class LensClientError(RuntimeError):
    """An HTTP or protocol error returned by the Lens service."""


class LensClient:
    """Call a configured local Lens instance without coupling to its internals."""

    def __init__(self, base_url="http://127.0.0.1:8765", api_key=None, timeout=660):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def health(self):
        return self._request("GET", "/health")

    def ask(self, question, mode="client"):
        return self._request("POST", "/v1/ask", {"question": question, "mode": mode})

    def sql(self, question, *, variable=None, config_file=None, env_file=None,
            user_id=None, record_scope=None):
        """Return validated SQL only; the Lens service does not execute it."""
        body = {"question": question}
        for key, value in (("variable", variable), ("config_file", config_file), ("env_file", env_file)):
            if value is not None:
                body[key] = value
        if user_id is not None:
            if type(user_id) is not int or user_id <= 0:
                raise ValueError("user_id must be a positive integer")
            body["user_id"] = user_id
        if record_scope is not None:
            if record_scope not in {"user_records_only", "all_records"} or user_id is None:
                raise ValueError("record_scope requires user_id and must be user_records_only or all_records")
            body["record_scope"] = record_scope
        return self._request("POST", "/v1/sql", body)

    def index(self, *, refresh=False, force=False):
        """Reindex the root configured on the Lens engine."""
        return self._request("POST", "/v1/index", {"refresh": refresh, "force": force})

    def _request(self, method, path, payload=None):
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise LensClientError(f"Lens returned HTTP {exc.code}: {detail}") from None
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LensClientError(f"Could not complete Lens request: {exc}") from None
