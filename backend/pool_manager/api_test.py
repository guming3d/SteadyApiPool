"""Trying a pool from the UI, and the LiteLLM virtual keys to call it with.

The backend sends the test request, so the browser needs no CORS from the gateway: only to this
gateway's own URL, only below ``/v1``, and only for a model name of the pool. The key comes with
the request and is never stored or logged.

LiteLLM keeps only a hash of each virtual key, so a key can be shown once, when it is created.
The only key this app writes is one test key per pool (alias ``<prefix>test-<pool>``), scoped to
the pool's model names; creating it again replaces it (rotating a key in place is an Enterprise
feature).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx

from .config import Settings
from .litellm import LiteLLMError
from .models import (
    ApiTestRequest,
    ApiTestResult,
    PoolEndpoint,
    VirtualKeySecret,
    VirtualKeyView,
)
from .service import PoolManagerService, ServiceError, test_key_alias, translate_error

logger = logging.getLogger(__name__)

# Longer than the pool's default 120 s per-attempt timeout, so LiteLLM's own answer gets through.
_TIMEOUT = httpx.Timeout(150.0, connect=10.0)
_MAX_BODY = 64 * 1024
_HEADER_PREFIXES = ("x-litellm-", "x-ratelimit-", "x-ms-", "llm_provider-")
_HEADERS = frozenset({"content-type", "retry-after", "x-request-id"})
# Cost and spend headers say nothing about routing.
_SKIPPED = ("x-litellm-response-cost", "x-litellm-key-spend", "x-litellm-callback")


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


class ApiTester:
    def __init__(
        self,
        settings: Settings,
        service: PoolManagerService,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._service = service
        self._http = httpx.Client(timeout=_TIMEOUT, transport=transport, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def _view(self, key: Mapping[str, Any], endpoint: PoolEndpoint) -> VirtualKeyView | None:
        """The key, when it can call the pool; ``None`` otherwise."""
        token = str(key.get("token") or "")
        models = [str(model) for model in key.get("models") or []]
        names = {name.casefold() for name in endpoint.model_names}
        if not token:
            return None
        # "all-proxy-models" is LiteLLM's way of saying every model.
        every = not models or "all-proxy-models" in models
        if not every and not names & {model.casefold() for model in models}:
            return None
        alias = str(key.get("key_alias") or "") or None
        expires = key.get("expires")
        return VirtualKeyView(
            id=token,
            alias=alias,
            key_name=str(key.get("key_name") or "") or None,
            scope="all" if every else "pool",
            models=models,
            blocked=key.get("blocked") is True,
            expires=str(expires) if expires else None,
            spend=_float(key.get("spend")),
            managed=alias == test_key_alias(self._settings.id_prefix, endpoint.pool_id),
        )

    def keys(self, pool_id: str) -> list[VirtualKeyView]:
        """Keys that can call the pool, the pool's own test key and usable ones first."""
        endpoint = self._service.endpoint(pool_id)
        try:
            keys = list(self._service.client().list_keys())
        except LiteLLMError as error:
            raise translate_error(error) from error
        views = [view for key in keys if (view := self._view(key, endpoint))]
        return sorted(
            views,
            key=lambda view: (
                not view.managed,
                view.blocked,
                view.scope != "pool",
                view.alias or "",
            ),
        )

    def create_key(self, pool_id: str, actor: str) -> VirtualKeySecret:
        """Creates the pool's test key, replacing the one created before."""
        endpoint = self._service.endpoint(pool_id)
        alias = test_key_alias(self._settings.id_prefix, endpoint.pool_id)
        client = self._service.client()
        try:
            old = [
                str(key["token"]) for key in client.list_keys(key_alias=alias) if key.get("token")
            ]
            # LiteLLM keeps aliases unique, so the old test key goes first.
            client.delete_keys(old)
            created = client.generate_key(
                {
                    "key_alias": alias,
                    "models": endpoint.model_names,
                    "metadata": {
                        "pool_manager": {
                            "managed_by": "AI Gateway Pool Manager",
                            "pool": endpoint.pool_id,
                        }
                    },
                }
            )
        except LiteLLMError as error:
            raise translate_error(error) from error
        logger.info("%s created the test key of pool %s", actor, endpoint.pool_id)
        view = self._view({**created, "models": endpoint.model_names}, endpoint)
        if view is None:
            raise ServiceError(502, "The gateway created a key that can't call the pool")
        return VirtualKeySecret(key=str(created["key"]), view=view)

    def call(self, pool_id: str, request: ApiTestRequest, actor: str) -> ApiTestResult:
        endpoint = self._service.endpoint(pool_id)
        if not endpoint.base_url:
            raise ServiceError(409, "The gateway has no URL yet")
        # A local gateway (e.g. in Docker) may be plain HTTP; a deployed app only calls HTTPS.
        if not endpoint.base_url.startswith("https://") and not self._settings.local:
            raise ServiceError(409, "The gateway has no HTTPS URL")
        if request.key is None:
            raise ServiceError(400, "Create or paste a key to call the pool with")
        body = dict(request.body)
        model = body.get("model")
        if model is None:
            body["model"] = endpoint.pool_id
        elif not isinstance(model, str) or model.casefold() not in {
            name.casefold() for name in endpoint.model_names
        }:
            raise ServiceError(
                400, f"model must be one of the pool's names: {', '.join(endpoint.model_names)}"
            )
        url = f"{endpoint.base_url}/{request.path}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {request.key.get_secret_value()}",
        }
        started = time.monotonic()
        try:
            with self._http.stream("POST", url, json=body, headers=headers) as response:
                data = b""
                for chunk in response.iter_bytes():
                    data += chunk
                    if len(data) > _MAX_BODY:
                        break
        except httpx.TimeoutException as error:
            raise ServiceError(
                504, f"The gateway did not answer within {_TIMEOUT.read:.0f} seconds"
            ) from error
        except httpx.HTTPError as error:
            raise ServiceError(502, f"Could not reach {endpoint.base_url}: {error}") from error
        elapsed = round((time.monotonic() - started) * 1000)
        logger.info("%s tested pool %s: %s in %d ms", actor, pool_id, response.status_code, elapsed)
        return ApiTestResult(
            status=response.status_code,
            elapsed_ms=elapsed,
            url=str(response.request.url),
            headers={
                name: value
                for name, value in response.headers.items()
                if (name in _HEADERS or name.startswith(_HEADER_PREFIXES))
                and not name.startswith(_SKIPPED)
            },
            body=data[:_MAX_BODY].decode("utf-8", errors="replace"),
            truncated=len(data) > _MAX_BODY,
        )
