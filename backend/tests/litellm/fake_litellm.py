"""An in-memory LiteLLM proxy (admin API and chat completions) behind ``httpx.MockTransport``.

It behaves like LiteLLM v1.106 where the app relies on it (checked against a real proxy,
see tests/litellm/litellm_stack): ``/model/info`` leaves secrets out,
``PATCH /model/{id}/update`` merges, ``/config/update`` replaces each router setting it is
given, ``/key/list`` pages, keys are scoped by ``models`` and only returned once. It records
every request.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from itertools import count
from typing import Any
from urllib.parse import unquote

import httpx

URL = "https://gw.example.azurecontainerapps.io"
MASTER_KEY = "sk-master-0123456789"


@dataclass
class FakeLiteLLM:
    master_key: str = MASTER_KEY
    rows: list[dict[str, Any]] = field(default_factory=list)
    router: dict[str, Any] = field(
        default_factory=lambda: {"routing_strategy": "simple-shuffle", "model_group_alias": {}}
    )
    # Virtual keys by token (the hash LiteLLM keeps); "key" is the secret itself.
    keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    credentials: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: str = "1.106.4"
    db: str = "connected"
    # Rows (model_info.id) that answer this status instead of 200.
    failing: dict[str, int] = field(default_factory=dict)
    # Reads of /model/info after a model write that still show the rows from before it, as a
    # replica that hasn't reloaded from the database yet does.
    lagging_reads: int = 0
    _stale: list[dict[str, Any]] | None = None
    _stale_left: int = 0
    requests: list[httpx.Request] = field(default_factory=list)
    # Hosts that don't answer yet.
    unreachable_hosts: set[str] = field(default_factory=set)
    _keys: count = field(default_factory=lambda: count(1))  # type: ignore[type-arg]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def add_row(self, model_name: str, model: str, api_base: str, **info: Any) -> str:
        """A row this app didn't write (e.g. from LiteLLM's own UI)."""
        row_id = info.pop("id", f"{model_name}-{len(self.rows)}")
        self.rows.append(
            {
                "model_name": model_name,
                "litellm_params": {"model": model, "api_base": api_base, "api_key": "secret"},
                "model_info": {"id": row_id, **info},
            }
        )
        return row_id

    def pool(self, name: str) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["model_name"] == name]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host in self.unreachable_hosts:
            # e.g. an AKS gateway whose certificate isn't issued yet.
            raise httpx.ConnectError("unreachable", request=request)
        path = unquote(request.url.path)
        body = json.loads(request.content) if request.content else None
        auth = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if path in {"/health/readiness", "/health/liveliness"}:
            # As LiteLLM 1.106 answers anyone: no version (that is behind the master key).
            return httpx.Response(200, json={"status": "healthy", "db": self.db})
        if path.startswith("/v1/"):
            return self._inference(request, path, body, auth)
        if auth != self.master_key:
            return _error(401, "Authentication Error, invalid master key")
        if path == "/health/readiness/details":
            return httpx.Response(
                200, json={"status": "healthy", "db": self.db, "litellm_version": self.version}
            )
        if path == "/model/info":
            rows = self.rows
            if self._stale_left:
                self._stale_left -= 1
                rows = self._stale or []
            return httpx.Response(200, json={"data": [self._public(row) for row in rows]})
        if self.lagging_reads and path.startswith("/model/") and not self._stale_left:
            self._stale, self._stale_left = copy.deepcopy(self.rows), self.lagging_reads
        if path == "/model/new":
            return self._new(body)
        if path.startswith("/model/") and path.endswith("/update"):
            return self._update(path.removeprefix("/model/").removesuffix("/update"), body)
        if path == "/model/delete":
            before = len(self.rows)
            self.rows = [row for row in self.rows if row["model_info"]["id"] != body["id"]]
            if len(self.rows) == before:
                return _error(404, f"Model with id={body['id']} not found in db")
            return httpx.Response(200, json={"message": "Model deleted"})
        if path == "/router/settings":
            fields = [{"field_name": k, "field_value": v} for k, v in self.router.items()]
            return httpx.Response(200, json={"fields": fields})
        if path == "/config/update":
            for name, value in (body.get("router_settings") or {}).items():
                self.router[name] = copy.deepcopy(value)
            return httpx.Response(200, json={"message": "Config updated successfully"})
        if path == "/key/generate":
            return self._generate(body)
        if path == "/key/list":
            return self._list_keys(request)
        if path == "/key/delete":
            deleted = [token for token in body["keys"] if self.keys.pop(token, None)]
            return httpx.Response(200, json={"deleted_keys": deleted})
        if path == "/credentials" and request.method == "POST":
            name = body["credential_name"]
            if name in self.credentials:
                return _error(400, f"Credential {name} already exists")
            self.credentials[name] = body
            return httpx.Response(200, json={"success": True})
        if path.startswith("/credentials/") and request.method == "DELETE":
            if self.credentials.pop(path.removeprefix("/credentials/"), None) is None:
                return _error(404, "Credential not found")
            return httpx.Response(200, json={"success": True})
        return _error(400, f"unhandled {request.method} {path}")

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        public = copy.deepcopy(row)
        public["litellm_params"].pop("api_key", None)
        public["model_info"]["db_model"] = True
        return public

    def _new(self, body: dict[str, Any]) -> httpx.Response:
        row = copy.deepcopy(body)
        info = row.setdefault("model_info", {})
        info.setdefault("id", hashlib.sha256(json.dumps(body).encode()).hexdigest())
        if any(existing["model_info"]["id"] == info["id"] for existing in self.rows):
            return _error(400, f"Model with id={info['id']} already exists")
        credential = row["litellm_params"].get("litellm_credential_name")
        if credential and credential not in self.credentials:
            return _error(400, f"Credential {credential} not found")
        self.rows.append(row)
        return httpx.Response(200, json={"model_id": info["id"], **row})

    def _update(self, row_id: str, body: dict[str, Any]) -> httpx.Response:
        row = next((row for row in self.rows if row["model_info"]["id"] == row_id), None)
        if row is None:
            return _error(404, f"Model with id={row_id} not found")
        for section in ("litellm_params", "model_info"):
            row[section].update(copy.deepcopy(body.get(section) or {}))
        row["model_info"]["id"] = row_id
        if "model_name" in body:
            row["model_name"] = body["model_name"]
        return httpx.Response(200, json={"model_id": row_id})

    def _generate(self, body: dict[str, Any]) -> httpx.Response:
        alias = body.get("key_alias")
        if alias and any(key.get("key_alias") == alias for key in self.keys.values()):
            # As LiteLLM (1.106) answers: aliases are unique across keys.
            return _error(
                400,
                f"Key with alias '{alias}' already exists. Unique key aliases across all keys "
                "are required.",
            )
        number = next(self._keys)
        secret = f"sk-test{number:04d}abcdef"
        token = hashlib.sha256(secret.encode()).hexdigest()
        self.keys[token] = {
            "token": token,
            "key": secret,
            "key_alias": body.get("key_alias"),
            "key_name": f"sk-...{secret[-4:]}",
            "models": list(body.get("models") or []),
            "metadata": body.get("metadata") or {},
            "spend": 0.0,
            "expires": None,
            "blocked": None,
        }
        return httpx.Response(200, json={**self.keys[token]})

    def _list_keys(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        alias = params.get("key_alias")
        items = [
            {k: v for k, v in key.items() if k != "key"}
            for key in self.keys.values()
            if alias is None or key["key_alias"] == alias
        ]
        size = int(params.get("size") or 10)
        page = int(params.get("page") or 1)
        pages = max(1, -(-len(items) // size))
        chunk = items[(page - 1) * size : page * size]
        return httpx.Response(
            200,
            json={
                "keys": chunk,
                "total_count": len(items),
                "current_page": page,
                "total_pages": pages,
            },
        )

    def _inference(self, request: httpx.Request, path: str, body: Any, auth: str) -> httpx.Response:
        model = str((body or {}).get("model") or "")
        aliases = self.router.get("model_group_alias") or {}
        group = aliases.get(model, model)
        group = group.get("model") if isinstance(group, dict) else group
        if auth != self.master_key:
            key = next((k for k in self.keys.values() if k["key"] == auth), None)
            if key is None:
                return _error(401, "Authentication Error, Invalid proxy server token passed.")
            if key["models"] and model not in key["models"]:
                return _error(
                    403, f"The requested model '{model}' is not available for this API key"
                )
        rows = sorted(self.pool(group), key=lambda row: row["litellm_params"].get("order") or 1)
        if not rows:
            return _error(400, f"Invalid model name passed in model={model}")
        fallbacks = 0
        for row in rows:
            status = self.failing.get(row["model_info"]["id"])
            if status:
                fallbacks += 1
                continue
            headers = {
                "x-litellm-model-id": row["model_info"]["id"],
                "x-litellm-model-group": group,
                "x-litellm-model-api-base": row["litellm_params"]["api_base"],
                "x-litellm-attempted-fallbacks": str(fallbacks),
                "x-litellm-response-cost": "0.0001",
                "x-ms-region": "East US 2",
            }
            served = row["litellm_params"]["model"]
            words = sum(len(str(m.get("content", "")).split()) for m in body.get("messages") or [])
            usage = {
                "prompt_tokens": max(1, round(words / 0.75)),
                "completion_tokens": 2,
                "total_tokens": max(1, round(words / 0.75)) + 2,
            }
            if body.get("stream"):
                chunks = [
                    {"model": served, "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                    {"model": served, "choices": [{"index": 0, "delta": {"content": "hi"}}]},
                    {"model": served, "choices": [{"index": 0, "delta": {"content": " there"}}]},
                ]
                if (body.get("stream_options") or {}).get("include_usage"):
                    chunks.append({"model": served, "choices": [], "usage": usage})
                text = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                return httpx.Response(
                    200,
                    content=(text + "data: [DONE]\n\n").encode(),
                    headers={**headers, "content-type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                json={
                    "model": served,
                    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                    "usage": usage,
                },
                headers=headers,
            )
        return _error(429, "Rate limit reached for every deployment")


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message, "code": str(status)}})
