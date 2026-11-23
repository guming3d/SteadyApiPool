"""Trying a pool from the UI, and the runtime API key to call it with.

The backend sends the test request, so the browser needs no CORS from the gateway: only to this
gateway's own HTTPS host, only below its OpenAI v1 surface, and only with the pool's model name.
The key comes with the request and is never stored or logged.

An AI Gateway runtime key opens every alias on the gateway, not one pool. The only key this app
writes is one test key for the gateway (``<prefix>test``), created and read with the user's own
Azure rights, never the app identity; the built-in ``master`` key is never read.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx

from ..arm import ArmClient, ArmError, segment
from ..benchmark import BenchmarkTarget
from ..config import Settings
from ..errors import ServiceError
from ..models import ApiTestResult
from .models import ApiKeySecret, ApiKeyView, ApiTestRequest
from .service import AiGatewayService, managed_key_name

logger = logging.getLogger(__name__)

# Longer than a slow model's answer; the gateway has no timeout of its own to wait for.
_TIMEOUT = httpx.Timeout(150.0, connect=10.0)
_MAX_BODY = 64 * 1024
_HEADER_PREFIXES = ("x-ratelimit-", "x-ms-", "apim-", "azureml-")
_HEADERS = frozenset({"content-type", "retry-after", "x-request-id", "traceparent"})
_BUILT_IN = "master"
_GATEWAY_SUFFIX = ".ai.gateway.azure.com"
# Model names a test call or load test may use go with these paths below ``/openai/v1``.
_PATHS = frozenset({"chat/completions", "responses", "embeddings"})


def _translate(error: ArmError, what: str) -> ServiceError:
    if error.status in {401, 403}:
        return ServiceError(403, f"Your Azure account cannot {what}: {error.message}")
    if error.status == 404:
        return ServiceError(404, error.message)
    return ServiceError(502, f"Could not {what}: {error.message}")


class AiGatewayTester:
    def __init__(
        self,
        settings: Settings,
        service: AiGatewayService,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._service = service
        self._version = settings.aigateway_api_version
        self._http = httpx.Client(timeout=_TIMEOUT, transport=transport, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    @property
    def _key_name(self) -> str:
        return managed_key_name(self._settings.id_prefix)

    def _view(self, key: Mapping[str, Any]) -> ApiKeyView:
        name = str(key.get("name") or "")
        properties = key.get("properties") or {}
        expires = properties.get("expirationDate")
        return ApiKeyView(
            id=name,
            display_name=str(properties.get("displayName") or "") or None,
            state=str(properties.get("state") or "") or None,
            expires=str(expires) if expires else None,
            managed=name.casefold() == self._key_name.casefold(),
        )

    def keys(self, user: ArmClient) -> list[ApiKeyView]:
        """The gateway's runtime keys (all of them open every pool), the test key first."""
        try:
            items = list(user.list("/apiKeys", api_version=self._version))
        except ArmError as error:
            raise _translate(error, "list the gateway's API keys") from error
        views = [
            self._view(item)
            for item in items
            if str(item.get("name") or "").casefold() != _BUILT_IN
        ]
        return sorted(views, key=lambda view: (not view.managed, view.id.casefold()))

    def create_key(self, user: ArmClient, actor: str) -> ApiKeyView:
        """Creates the test key, or keeps the one created before."""
        path = f"/apiKeys/{segment(self._key_name)}"
        try:
            found = user.get_json(path, api_version=self._version)
            if found is None:
                response = user.request(
                    "PUT",
                    path,
                    api_version=self._version,
                    json_body={"properties": {"displayName": "SteadyApiPool test key"}},
                    headers={"If-None-Match": "*"},
                )
                found = (response.json() if response.content else {"name": self._key_name}), None
                logger.info("%s created the gateway's test key", actor)
        except ArmError as error:
            raise _translate(error, "create the test key") from error
        return self._view({"name": self._key_name, **found[0]})

    def secret(self, user: ArmClient, key_id: str, actor: str) -> ApiKeySecret:
        """A key's primary value, read with the user's own rights; never the built-in key."""
        if key_id.casefold() == _BUILT_IN:
            raise ServiceError(403, "The app never reads the built-in master key")
        try:
            response = user.request(
                "POST",
                f"/apiKeys/{segment(key_id)}/listSecrets",
                api_version=self._version,
            )
        except ArmError as error:
            raise _translate(error, "read this API key") from error
        value = str((response.json() or {}).get("primaryKey") or "")
        if not value:
            raise ServiceError(502, "The gateway returned no key value")
        logger.info("%s read API key %s", actor, key_id)
        return ApiKeySecret(primary_key=value)

    def target(self, pool_id: str, model: object) -> tuple[str, str]:
        """The gateway's OpenAI v1 URL and the model name to call the pool with, for a test
        call or a load test."""
        endpoint = self._service.endpoint(pool_id)
        base_url = endpoint.base_url
        if not base_url:
            raise ServiceError(409, "The gateway has no host name yet")
        host = httpx.URL(base_url).host
        if not base_url.startswith("https://") or (
            not self._settings.local and not host.endswith(_GATEWAY_SUFFIX)
        ):
            raise ServiceError(409, "The gateway has no HTTPS host name")
        if model is not None and (
            not isinstance(model, str) or model.casefold() != endpoint.model_name.casefold()
        ):
            raise ServiceError(400, f"model must be the pool's model name, {endpoint.model_name}")
        return base_url, endpoint.model_name

    def benchmark_target(self, pool_id: str, model: str | None, key: str) -> BenchmarkTarget:
        """Where a load test of the pool sends its requests. The gateway names the backend that
        answered in ``x-ms-served-model``; it doesn't say how often it failed over."""
        base_url, name = self.target(pool_id, model)
        return BenchmarkTarget(
            url=f"{base_url}/chat/completions",
            model=name,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            member_header="x-ms-served-model",
            # The gateway forwards the body; newer Azure OpenAI models refuse max_tokens.
            tokens_field="max_completion_tokens",
        )

    def call(self, pool_id: str, request: ApiTestRequest, actor: str) -> ApiTestResult:
        if request.path not in _PATHS:
            raise ServiceError(400, f"path must be one of {', '.join(sorted(_PATHS))}")
        body = dict(request.body)
        base_url, body["model"] = self.target(pool_id, body.get("model"))
        if request.key is None:
            raise ServiceError(400, "Create or paste a key to call the pool with")
        url = f"{base_url}/{request.path}"
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
            raise ServiceError(502, f"Could not reach {base_url}: {error}") from error
        elapsed = round((time.monotonic() - started) * 1000)
        logger.info("%s tested pool %s: %s in %d ms", actor, pool_id, response.status_code, elapsed)
        return ApiTestResult(
            status=response.status_code,
            elapsed_ms=elapsed,
            url=str(response.request.url),
            headers={
                name: value
                for name, value in response.headers.items()
                if name in _HEADERS or name.startswith(_HEADER_PREFIXES)
            },
            body=data[:_MAX_BODY].decode("utf-8", errors="replace"),
            truncated=len(data) > _MAX_BODY,
        )
