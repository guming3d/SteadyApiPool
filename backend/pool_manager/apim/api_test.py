"""Trying a pool API from the UI, and getting an APIM subscription key to call it with.

Browsers can't call the APIM gateway directly (it sends no CORS headers), so the backend sends
the test request: only to this APIM service's gateway, only to APIs bound to a pool, and only as
a POST below the API's path. The key comes with the request and is never stored or logged. The
public app, where anyone can sign in and bring their own service, only calls Azure's own gateway
host names (``*.azure-api.net`` and the sovereign clouds' equivalents), never a custom domain,
so its backend can't be pointed at other hosts.

Keys are read with the signed-in user's own Azure rights, like the setup fixes; the app
identity has no ``subscriptions/listSecrets``. The only write is one test subscription per API,
scoped to that API and created with ``If-None-Match: *``. Existing subscriptions are never
changed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..arm import ArmClient, ArmError, segment
from ..benchmark import BenchmarkTarget
from ..config import Settings
from ..errors import ServiceError
from ..models import (
    ApiTestResult,
)
from .models import (
    ApiTestRequest,
    ApiView,
    KeyScope,
    SubscriptionKeyView,
    SubscriptionSecret,
)
from .service import ApimService

logger = logging.getLogger(__name__)

# The built-in all-access subscription; never offered to clients or for tests.
MASTER_SUBSCRIPTION = "master"
# Gateway host names Azure assigns (public, China and US Government clouds).
AZURE_GATEWAY_SUFFIXES = (".azure-api.net", ".azure-api.cn", ".azure-api.us")
KEY_ACTIONS = (
    "Microsoft.ApiManagement/service/subscriptions/read",
    "Microsoft.ApiManagement/service/subscriptions/listSecrets/action",
)
# Longer than the pool's 120 s backend timeout, so APIM's own timeout answer gets through.
_TIMEOUT = httpx.Timeout(150.0, connect=10.0)
_MAX_BODY = 64 * 1024
_HEADER_PREFIXES = ("x-ms-", "x-ratelimit-", "apim-")
_HEADERS = frozenset({"content-type", "retry-after", "x-request-id"})
_SCOPE_ORDER: dict[KeyScope, int] = {"api": 0, "product": 1, "all_apis": 2}


def _key_error(error: ArmError, apim: str) -> ServiceError:
    if error.status == 401:
        return ServiceError(401, f"Sign in to Azure to read subscription keys: {error.message}")
    if error.status == 403:
        return ServiceError(
            403,
            f"Your Azure account cannot read subscription keys on {apim}. It needs "
            f"{' and '.join(KEY_ACTIONS)} (API Management Service Contributor has both).",
        )
    if error.status in {400, 404, 409}:
        return ServiceError(error.status, error.message)
    return ServiceError(502, f"APIM returned {error.status} {error.code}: {error.message}")


def _key_scope(
    scope: str, api_id: str, products: Mapping[str, str]
) -> tuple[KeyScope, str | None] | None:
    """What a subscription scope opens for ``api_id``, or None if its key doesn't work there.

    ARM returns full IDs (``/subscriptions/.../service/<name>/apis/<id>``); short forms
    (``/apis/<id>``) are accepted too. ``products`` maps lower-case IDs of the products
    containing the API to their IDs.
    """
    lowered = scope.casefold().rstrip("/")
    marker = "/providers/microsoft.apimanagement/service/"
    if marker in lowered:
        lowered = "/" + lowered.split(marker, 1)[1].partition("/")[2]
    if lowered == "/apis":
        return "all_apis", None
    if lowered == f"/apis/{api_id.casefold()}":
        return "api", None
    if lowered.startswith("/products/"):
        product = lowered.removeprefix("/products/")
        if product in products:
            return "product", products[product]
    return None


class ApimTester:
    def __init__(
        self,
        settings: Settings,
        service: ApimService,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._service = service
        self._http = httpx.Client(timeout=_TIMEOUT, transport=transport, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def _bound_api(self, api_id: str) -> ApiView:
        api = self._service.get_api(api_id)
        if api.binding is None:
            raise ServiceError(
                409, f"{api_id} is not bound to a pool; only pool APIs are tested here"
            )
        return api

    def _test_key_id(self, api_id: str) -> str:
        return f"{self._settings.id_prefix}test-{api_id}"

    # -- subscription keys (with the user's rights) -----------------------------------------

    def _products(self, arm: ArmClient, api_id: str) -> dict[str, str]:
        items = arm.list(
            f"/apis/{segment(api_id)}/products", api_version=self._settings.api_version
        )
        return {
            str(item["name"]).casefold(): str(item["name"]) for item in items if item.get("name")
        }

    def _view(
        self, item: Mapping[str, Any], api_id: str, products: Mapping[str, str]
    ) -> SubscriptionKeyView | None:
        name = str(item.get("name") or "")
        properties = item.get("properties") or {}
        scope = _key_scope(str(properties.get("scope") or ""), api_id, products)
        if not name or name.casefold() == MASTER_SUBSCRIPTION or scope is None:
            return None
        return SubscriptionKeyView(
            id=name,
            display_name=str(properties.get("displayName") or name),
            scope=scope[0],
            product=scope[1],
            state=str(properties.get("state") or "unknown"),
            managed=name.casefold() == self._test_key_id(api_id).casefold(),
        )

    def keys(self, arm: ArmClient, api_id: str) -> list[SubscriptionKeyView]:
        """Subscriptions whose key works for the API, most specific and usable first."""
        self._bound_api(api_id)
        try:
            products = self._products(arm, api_id)
            items = list(arm.list("/subscriptions", api_version=self._settings.api_version))
        except ArmError as error:
            raise _key_error(error, self._settings.apim_name) from error
        views = [view for item in items if (view := self._view(item, api_id, products))]
        return sorted(
            views,
            key=lambda view: (
                not view.managed,
                view.state != "active",
                _SCOPE_ORDER[view.scope],
                view.display_name.casefold(),
            ),
        )

    def create_key(self, arm: ArmClient, api_id: str, actor: str) -> SubscriptionKeyView:
        """Creates this API's test subscription, or returns it if it already exists."""
        api = self._bound_api(api_id)
        key_id = self._test_key_id(api_id)
        path = f"/subscriptions/{segment(key_id)}"
        body = {
            "properties": {
                "scope": f"/apis/{api_id}",
                "displayName": f"Test key for {api.display_name} (SteadyApiPool)"[:100],
                "state": "active",
            }
        }
        try:
            try:
                response = arm.request(
                    "PUT",
                    path,
                    api_version=self._settings.api_version,
                    json_body=body,
                    headers={"If-None-Match": "*"},
                )
                item = response.json()
                logger.info("%s created test subscription %s for API %s", actor, key_id, api_id)
            except ArmError as error:
                if error.status != 412:
                    raise
                found = arm.get_json(path, api_version=self._settings.api_version)
                if found is None:
                    raise
                item = found[0]
        except ArmError as error:
            raise _key_error(error, self._settings.apim_name) from error
        view = self._view(item, api_id, {})
        if view is None:
            raise ServiceError(409, f"Subscription {key_id} exists but doesn't open {api_id}")
        return view

    def secret(self, arm: ArmClient, api_id: str, key_id: str, actor: str) -> SubscriptionSecret:
        """The primary key of a subscription that works for the API (and only such one)."""
        self._bound_api(api_id)
        path = f"/subscriptions/{segment(key_id)}"
        try:
            found = arm.get_json(path, api_version=self._settings.api_version)
            if found is None:
                raise ServiceError(404, f"Subscription {key_id} was not found")
            if self._view(found[0], api_id, self._products(arm, api_id)) is None:
                raise ServiceError(403, f"The key of {key_id} doesn't open {api_id}")
            secrets = arm.request(
                "POST", f"{path}/listSecrets", api_version=self._settings.api_version
            ).json()
        except ArmError as error:
            raise _key_error(error, self._settings.apim_name) from error
        key = str(secrets.get("primaryKey") or "")
        if not key:
            raise ServiceError(502, f"APIM returned no key for {key_id}")
        logger.info("%s read the key of subscription %s for API %s", actor, key_id, api_id)
        return SubscriptionSecret(primary_key=key)

    # -- the test call ----------------------------------------------------------------

    def _api_base(self, api: ApiView) -> tuple[str, str]:
        """The gateway and the API's URL on it, where test calls and load tests may go."""
        gateway = self._service.capabilities().gateway_url
        if not gateway or not gateway.startswith("https://"):
            raise ServiceError(409, "The APIM service has no gateway URL yet")
        split = urlsplit(gateway)
        host = (split.hostname or "").casefold()
        if self._settings.public and (
            split.port is not None or not host.endswith(AZURE_GATEWAY_SUFFIXES)
        ):
            raise ServiceError(
                409, f"The public app only calls Azure's own gateway host names, not {host}"
            )
        base = "/".join(part for part in (gateway.rstrip("/"), api.path.strip("/")) if part)
        return gateway, base

    def benchmark_target(
        self, api_id: str, model: str | None, api_version: str | None, key: str | None
    ) -> tuple[str, BenchmarkTarget]:
        """The pool the API is bound to, and where a load test through the API sends its
        requests. APIM names no member in its answer; Azure OpenAI's ``x-ms-region`` header,
        which APIM passes on, tells members in different regions apart."""
        api = self._bound_api(api_id)
        assert api.binding is not None
        _, base = self._api_base(api)
        headers = {"Content-Type": "application/json"}
        if key:
            headers[api.subscription_key_header] = key
        return api.binding.pool_id, BenchmarkTarget(
            url=f"{base}/chat/completions",
            # The member's URL fixes the deployment; the body still needs a model.
            model=model or api.binding.pool_id,
            headers=headers,
            params={"api-version": api_version} if api_version else {},
            member_header="x-ms-region",
            tokens_field="max_completion_tokens",
        )

    def call(self, api_id: str, request: ApiTestRequest, actor: str) -> ApiTestResult:
        api = self._bound_api(api_id)
        gateway, base = self._api_base(api)
        url = f"{base}/{request.path}"
        headers = {"Content-Type": "application/json"}
        if request.subscription_key:
            headers[api.subscription_key_header] = request.subscription_key.get_secret_value()
        params = {"api-version": request.api_version} if request.api_version else {}
        started = time.monotonic()
        try:
            with self._http.stream(
                "POST", url, params=params, json=request.body, headers=headers
            ) as response:
                data = b""
                for chunk in response.iter_bytes():
                    data += chunk
                    if len(data) > _MAX_BODY:
                        break
        except httpx.TimeoutException as error:
            raise ServiceError(
                504, f"APIM did not answer within {_TIMEOUT.read:.0f} seconds"
            ) from error
        except httpx.HTTPError as error:
            raise ServiceError(502, f"Could not reach {gateway}: {error}") from error
        elapsed = round((time.monotonic() - started) * 1000)
        logger.info("%s tested API %s: %s in %d ms", actor, api_id, response.status_code, elapsed)
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
