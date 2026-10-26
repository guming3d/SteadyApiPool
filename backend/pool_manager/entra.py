"""Signing users in to the public app with their own Entra account, and acting in Azure as them.

The browser signs in with MSAL.js (a multi-tenant app registration) and sends a token for this
app's own API on every request. The backend:

1. verifies it (signature from Entra's published keys, audience, issuer matching the token's
   tenant, the ``access_as_user`` scope, an optional tenant allow-list), and
2. exchanges it on-behalf-of the user for an Azure Resource Manager token, so ARM decides what
   each call may do with the user's own roles.

Nothing is written anywhere. The app's own credential for the exchange is an assertion from its
user-assigned managed identity, trusted by the app registration as a federated credential, so
there is no secret either. ARM tokens live in memory in the user's session (sessions.py), which
ends when they sign out or go idle; Entra expires them within about an hour regardless.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any

import httpx
import jwt

from .config import Settings
from .service import ServiceError

logger = logging.getLogger(__name__)

API_SCOPE_NAME = "access_as_user"
ARM_USER_SCOPE = "https://management.azure.com/user_impersonation"
# What a managed identity token must be for to stand in for the app's client secret.
TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"
_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
# Entra error codes that mean the user (or their admin) hasn't consented to Azure access yet.
_CONSENT_ERRORS = frozenset({65001, 65004, 90094})
_LEEWAY_SECONDS = 60
_REFRESH_MARGIN_SECONDS = 300
_TIMEOUT = 15.0


class SignInRequired(Exception):
    """The user must sign in again, or consent, before the app can act for them."""

    def __init__(self, message: str, code: str = "SignInRequired") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EntraUser:
    """A verified caller of the public app and the token they sent."""

    tenant_id: str
    object_id: str
    name: str
    token: str
    expires_on: float

    @property
    def key(self) -> str:
        """Identifies the user across requests; never shared between tenants."""
        return f"{self.tenant_id}/{self.object_id}".casefold()


KeyResolver = Callable[[str], Any]


class TokenVerifier:
    """Verifies the tokens the browser sends for this app's API."""

    def __init__(self, settings: Settings, keys: KeyResolver | None = None) -> None:
        self._client_id = settings.entra_client_id
        self._audiences = [self._client_id, f"api://{self._client_id}"]
        self._authority = settings.entra_authority.rstrip("/")
        self._tenants = settings.allowed_tenant_set
        if keys is None:
            # One key set for every tenant; PyJWT caches it and refetches on an unknown key ID.
            client = jwt.PyJWKClient(
                f"{self._authority}/common/discovery/v2.0/keys", cache_keys=True, lifespan=3600
            )

            def keys(token: str) -> Any:
                return client.get_signing_key_from_jwt(token).key

        self._keys = keys

    def verify(self, token: str) -> EntraUser:
        try:
            key = self._keys(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audiences,
                leeway=_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "tid", "oid"]},
            )
        except (jwt.PyJWTError, ValueError) as error:
            raise ServiceError(401, f"Sign in again: {error}") from error
        tenant = str(claims["tid"]).casefold()
        # A multi-tenant app accepts any tenant's keys, so the issuer must be the token's own
        # tenant; otherwise one tenant could mint tokens claiming to be from another.
        if str(claims["iss"]).rstrip("/").casefold() != f"{self._authority}/{tenant}/v2.0":
            raise ServiceError(401, "Sign in again: the token's issuer doesn't match its tenant")
        if API_SCOPE_NAME not in str(claims.get("scp") or "").split():
            # App-only tokens carry roles, not scopes: only people use this app.
            raise ServiceError(401, f"Sign in again: the token lacks the {API_SCOPE_NAME} scope")
        if self._tenants and tenant not in self._tenants:
            raise ServiceError(403, "Your organization isn't allowed to use this app")
        name = str(
            claims.get("preferred_username")
            or claims.get("upn")
            or claims.get("email")
            or claims.get("name")
            or claims["oid"]
        )
        return EntraUser(
            tenant_id=tenant,
            object_id=str(claims["oid"]).casefold(),
            name=name,
            token=token,
            expires_on=float(claims["exp"]),
        )


def managed_identity_assertion(settings: Settings) -> Callable[[], str]:
    """The app's credential for the exchange: a token from its user-assigned managed identity
    that the app registration trusts (a federated identity credential)."""
    lock = Lock()
    credential: Any = None

    def assertion() -> str:
        nonlocal credential
        with lock:
            if credential is None:
                from azure.identity import ManagedIdentityCredential

                credential = ManagedIdentityCredential(
                    client_id=settings.federated_identity_client_id or None
                )
        return str(credential.get_token(TOKEN_EXCHANGE_SCOPE).token)

    return assertion


class OboExchanger:
    """Exchanges a user's token for one to another API on their behalf (OAuth 2.0 OBO).

    A plain token request, so it is plain what happens: nothing is cached here, and the refresh
    token Entra may return is dropped.
    """

    def __init__(
        self,
        settings: Settings,
        assertion: Callable[[], str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client_id = settings.entra_client_id
        self._authority = settings.entra_authority.rstrip("/")
        self._assertion = assertion or managed_identity_assertion(settings)
        self._http = httpx.Client(timeout=_TIMEOUT, transport=transport)

    def exchange(self, user: EntraUser, scope: str) -> tuple[str, float]:
        try:
            client_assertion = self._assertion()
        except Exception as error:  # azure-identity raises several unrelated types.
            logger.error("The app's managed identity gave no assertion: %s", error)
            raise ServiceError(503, "The app can't reach Entra right now; try again") from error
        try:
            response = self._http.post(
                f"{self._authority}/{user.tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": _JWT_BEARER,
                    "client_id": self._client_id,
                    "client_assertion_type": _ASSERTION_TYPE,
                    "client_assertion": client_assertion,
                    "assertion": user.token,
                    "scope": scope,
                    "requested_token_use": "on_behalf_of",
                },
            )
        except httpx.HTTPError as error:
            raise ServiceError(503, f"Couldn't reach Entra: {error}") from error
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or "access_token" not in body:
            codes = {int(code) for code in body.get("error_codes") or [] if str(code).isdigit()}
            description = str(body.get("error_description") or response.text[:300]).splitlines()
            message = description[0] if description else f"Entra returned {response.status_code}"
            if codes & _CONSENT_ERRORS or body.get("suberror") == "consent_required":
                raise SignInRequired(message, code="ConsentRequired")
            raise SignInRequired(message)
        return str(body["access_token"]), time.time() + float(body.get("expires_in") or 300)


Exchange = Callable[[EntraUser, str], tuple[str, float]]


@dataclass
class _AccessToken:
    token: str
    expires_on: float


class UserCredential:
    """A token credential (as ArmClient expects) that acts as one signed-in user.

    It holds their latest token for this app, renewed by every request they make, and the ARM
    tokens exchanged for it, in memory only.
    """

    def __init__(self, user: EntraUser, exchange: Exchange) -> None:
        self._user = user
        self._exchange = exchange
        self._lock = Lock()
        self._tokens: dict[str, _AccessToken] = {}

    @property
    def user(self) -> EntraUser:
        return self._user

    def update(self, user: EntraUser) -> None:
        """The same user's newer token, from their latest request."""
        if user.key != self._user.key:
            raise ValueError("a session's credential belongs to one user")
        with self._lock:
            if user.expires_on >= self._user.expires_on:
                self._user = user

    def get_token(self, *scopes: str, tenant_id: str | None = None, **_: Any) -> _AccessToken:
        if tenant_id and tenant_id.casefold() != self._user.tenant_id:
            # Entra only exchanges a token in the tenant it was issued for.
            raise SignInRequired(
                f"You're signed in to tenant {self._user.tenant_id}; sign in to {tenant_id} "
                "to use it",
                code="TenantMismatch",
            )
        # ARM is asked for ".default"; the app's consented permission is user_impersonation.
        scope = " ".join(ARM_USER_SCOPE if item.endswith("/.default") else item for item in scopes)
        with self._lock:
            cached = self._tokens.get(scope)
            if cached and cached.expires_on - _REFRESH_MARGIN_SECONDS > time.time():
                return cached
            user = self._user
        if user.expires_on - _LEEWAY_SECONDS < time.time():
            raise SignInRequired("Your sign-in has expired; sign in again")
        token, expires_on = self._exchange(user, scope)
        fresh = _AccessToken(token, expires_on)
        with self._lock:
            self._tokens[scope] = fresh
        return fresh

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()
