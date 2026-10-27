"""Minimal Azure Resource Manager client, scoped to the gateway's resource when it has one: the
APIM service, or a LiteLLM gateway's Container App."""

from __future__ import annotations

import base64
import copy
import json
import logging
import re
import time
from collections.abc import Collection, Iterator, Mapping
from contextlib import suppress
from threading import Lock
from typing import Any, Protocol
from urllib.parse import quote, urlencode

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_REQUEST_TIMEOUT_SECONDS = 30.0


class TokenCredential(Protocol):
    def get_token(self, *scopes: str, **kwargs: Any) -> Any: ...


class ArmError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def segment(value: str) -> str:
    return quote(value, safe="")


def gateway_resource_id(settings: Settings) -> str:
    """The gateway's ARM resource, which relative paths start from. An external LiteLLM gateway
    has none (nor has the app before a gateway is chosen), so only the ARM-root methods
    (Foundry, metrics, cost) make sense then."""
    group = (
        f"/subscriptions/{segment(settings.subscription_id)}"
        f"/resourceGroups/{segment(settings.resource_group)}"
    )
    if settings.product == "apim":
        return f"{group}/providers/Microsoft.ApiManagement/service/{segment(settings.apim_name)}"
    if settings.product == "litellm" and not settings.external:
        return f"{group}/providers/Microsoft.App/containerApps/{segment(settings.gateway_name)}"
    return ""


class ArmClient:
    """Calls ARM with a managed identity / developer token.

    ``DefaultAzureCredential`` covers App Service managed identity (set ``AZURE_CLIENT_ID``
    for a user-assigned identity) and ``az login`` locally. Transient 429/5xx responses
    are retried with exponential backoff, honouring ``Retry-After``.
    """

    def __init__(
        self,
        settings: Settings,
        credential: TokenCredential | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
        *,
        tenant_id: str | None = None,
    ) -> None:
        if credential is None:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        self._credential = credential
        self._origin = settings.arm_endpoint.rstrip("/")
        self._scope = f"{self._origin}/.default"
        self._sleep = sleep
        self._token_lock = Lock()
        self._token: tuple[str, float] | None = None
        # Locally the user may belong to several tenants; ask for a token in the target's.
        self._tenant_id = tenant_id or settings.tenant_id or None
        self._subscription_id = settings.subscription_id
        self.resource_id = gateway_resource_id(settings)
        self._http = httpx.Client(
            base_url=f"{self._origin}{self.resource_id}",
            timeout=_REQUEST_TIMEOUT_SECONDS,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    @property
    def token_scope(self) -> str:
        return self._scope

    def arm_url(self, arm_path: str) -> str:
        """Absolute URL for an ARM path such as a subscription or a Foundry account."""
        return f"{self._origin}{arm_path}"

    def reset_token(self) -> None:
        """Drops the cached token, e.g. after the credential signed in as someone else."""
        with self._token_lock:
            self._token = None

    def with_token(self, token: str, expires_on: float) -> ArmClient:
        """A client that calls ARM with someone else's token (the signed-in user's), sharing
        this client's connection pool."""
        clone = copy.copy(self)
        clone._credential = _StaticToken(token, expires_on)
        clone._token_lock = Lock()
        clone._token = (token, expires_on)
        return clone

    def token_for(self, scope: str) -> str:
        """A token for another Azure API (e.g. Log Analytics), from the same credential and
        tenant. Not cached: callers keep it as long as they need it."""
        if isinstance(self._credential, _StaticToken):
            raise ArmError(401, "TokenScope", "Only an ARM token is available here")
        options = {"tenant_id": self._tenant_id} if self._tenant_id else {}
        try:
            return str(self._credential.get_token(scope, **options).token)
        except Exception as error:  # azure-identity raises several unrelated types.
            message = str(error).splitlines()[0] if str(error) else type(error).__name__
            raise ArmError(401, "CredentialUnavailable", message) from error

    def token_claims(self) -> dict[str, Any]:
        """Claims of the current token, for display only (ARM validates the token itself)."""
        return jwt_claims(self._bearer())

    def subscription_tenant(self, subscription_id: str | None = None) -> str | None:
        """The tenant of a subscription (the gateway's by default), from the unauthenticated
        challenge ARM returns."""
        subscription_id = subscription_id or self._subscription_id
        if not subscription_id:
            return None
        try:
            response = self._http.get(
                self.arm_url(f"/subscriptions/{segment(subscription_id)}"),
                params={"api-version": "2022-12-01"},
            )
        except httpx.HTTPError:
            return None
        challenge = response.headers.get("WWW-Authenticate", "")
        match = re.search(r"authorization_uri=\"https://[^/]+/([0-9a-fA-F-]{36})", challenge)
        return match.group(1) if match else None

    def _bearer(self) -> str:
        with self._token_lock:
            if self._token is None or self._token[1] - 300 < time.time():
                options = {"tenant_id": self._tenant_id} if self._tenant_id else {}
                try:
                    token = self._credential.get_token(self._scope, **options)
                except Exception as error:  # azure-identity raises several unrelated types.
                    message = str(error).splitlines()[0] if str(error) else type(error).__name__
                    # entra.SignInRequired says why (ConsentRequired, TenantMismatch, ...).
                    code = getattr(error, "code", None)
                    code = code if isinstance(code, str) else "CredentialUnavailable"
                    raise ArmError(401, code, message) from error
                self._token = (token.token, float(token.expires_on))
            return self._token[0]

    def request(
        self,
        method: str,
        path: str,
        *,
        api_version: str,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        allow_not_found: bool = False,
        accept: Collection[int] = (),
    ) -> httpx.Response:
        """``accept``: error statuses returned rather than raised, for a body worth reading."""
        query = {"api-version": api_version, **(params or {})}
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            response = self._http.request(
                method,
                # An empty path addresses the gateway itself; httpx would add a trailing "/".
                path or f"{self._origin}{self.resource_id}",
                params=query,
                json=json_body,
                headers={"Authorization": f"Bearer {self._bearer()}", **(headers or {})},
            )
            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                delay = _retry_delay(response, attempt)
                logger.warning(
                    "ARM %s %s returned %s; retrying in %.1fs",
                    method,
                    path,
                    response.status_code,
                    delay,
                )
                self._sleep(delay)
                continue
            break
        if response.status_code == 404 and allow_not_found:
            return response
        if response.status_code >= 400 and response.status_code not in accept:
            raise _arm_error(response)
        return response

    def get_url(self, url: str, *, accept: Collection[int] = ()) -> httpx.Response:
        """GETs an absolute ARM URL, e.g. a long-running operation's Location."""
        return self._absolute_get(url, accept=accept)

    def wait(self, response: httpx.Response, *, timeout_seconds: float = 120.0) -> None:
        """Polls an ARM long-running operation (202 + Azure-AsyncOperation/Location)."""
        if response.status_code != 202:
            return
        monitor = response.headers.get("Azure-AsyncOperation") or response.headers.get("Location")
        if not monitor:
            return
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self._sleep(_retry_delay(response, 1))
            response = self._absolute_get(monitor)
            if response.status_code == 202:
                continue
            status = ""
            with suppress(ValueError):
                status = str(response.json().get("status") or "")
            if status.casefold() in {"failed", "canceled", "cancelled"}:
                error = response.json().get("error") or {}
                raise ArmError(
                    409,
                    str(error.get("code") or "OperationFailed"),
                    str(error.get("message") or "ARM operation failed"),
                )
            if status.casefold() in {"", "succeeded"}:
                return
        raise ArmError(504, "OperationTimeout", "ARM operation did not finish in time")

    def get_json(
        self, path: str, *, api_version: str, params: Mapping[str, str] | None = None
    ) -> tuple[dict[str, Any], str | None] | None:
        """Returns ``(body, etag)`` or ``None`` for 404."""
        response = self.request(
            "GET", path, api_version=api_version, params=params, allow_not_found=True
        )
        if response.status_code == 404:
            return None
        return response.json(), response.headers.get("ETag")

    def list(self, path: str, *, api_version: str) -> Iterator[dict[str, Any]]:
        """Lists a collection under the gateway's resource."""
        yield from self._pages(self.request("GET", path, api_version=api_version))

    def list_arm(
        self, arm_path: str, *, api_version: str, params: Mapping[str, str] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Lists any collection addressed from the ARM root (e.g. Cognitive Services)."""
        query = urlencode({"api-version": api_version, **(params or {})})
        yield from self._pages(self._absolute_get(f"{self._origin}{arm_path}?{query}"))

    def get_arm_json(
        self, arm_path: str, *, api_version: str, params: Mapping[str, str] | None = None
    ) -> dict[str, Any] | None:
        """Reads any resource addressed from the ARM root; ``None`` for 404."""
        query = urlencode({"api-version": api_version, **(params or {})})
        url = f"{self._origin}{arm_path}?{query}"
        try:
            body: dict[str, Any] = self._absolute_get(url).json()
        except ArmError as error:
            if error.status == 404:
                return None
            raise
        return body

    def _pages(self, response: httpx.Response) -> Iterator[dict[str, Any]]:
        while True:
            payload = response.json()
            yield from payload.get("value") or []
            next_link = payload.get("nextLink")
            if not next_link:
                return
            # nextLink is absolute and already carries api-version and $skipToken.
            response = self._absolute_get(next_link)

    def _absolute_get(self, url: str, *, accept: Collection[int] = ()) -> httpx.Response:
        # Never send the bearer token to a host other than the configured ARM endpoint.
        if not url.startswith(f"{self._origin}/"):
            raise ArmError(502, "UnexpectedNextLink", "ARM returned a nextLink for another host")
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            response = self._http.get(url, headers={"Authorization": f"Bearer {self._bearer()}"})
            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                self._sleep(_retry_delay(response, attempt))
                continue
            break
        if response.status_code >= 400 and response.status_code not in accept:
            raise _arm_error(response)
        return response


class _StaticToken:
    def __init__(self, token: str, expires_on: float) -> None:
        self.token = token
        self.expires_on = expires_on

    def get_token(self, *_: str, **__: Any) -> _StaticToken:
        return self


def jwt_claims(token: str) -> dict[str, Any]:
    """Decodes a JWT payload without verifying it; ``{}`` for anything that is not a JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return {}
    return claims if isinstance(claims, dict) else {}


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After", "")
    if retry_after.isdecimal():
        return min(float(retry_after), 30.0)
    return min(2.0 ** (attempt - 1), 8.0)


def _arm_error(response: httpx.Response) -> ArmError:
    code, message = "ArmError", response.text[:500]
    try:
        error = response.json().get("error") or {}
        code = str(error.get("code") or code)
        message = str(error.get("message") or message)
    except ValueError:
        pass
    return ArmError(response.status_code, code, message)
