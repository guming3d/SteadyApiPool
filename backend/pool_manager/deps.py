"""Dependencies, replaced in tests through ``app.dependency_overrides``.

Locally and in the Easy Auth deployment there is one set of components for the process (the
``lru_cache`` factories). In the public app each request resolves to the caller's own session
(sessions.py), built for them and the gateway their browser chose.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request

from .access import AccessChecker
from .arm import ArmClient
from .config import Settings, get_settings
from .cost import CostReader
from .credentials import LocalCredential
from .discovery import DeploymentDiscovery
from .entra import EntraUser, OboExchanger, TokenVerifier
from .errors import ServiceError
from .litellm.api_test import ApiTester
from .litellm.benchmark import BenchmarkRunner
from .litellm.gateway import GatewayConnection
from .litellm.service import PoolManagerService
from .metrics import PoolMonitor
from .secret_store import SecretStore
from .sessions import TARGET_HEADER, Sessions, UserSession, parse_target
from .target import TargetChooser

logger = logging.getLogger(__name__)


@lru_cache
def get_local_credential() -> LocalCredential | None:
    """Locally the operator is the user, who can switch to a browser sign-in from the UI."""
    return LocalCredential() if get_settings().auth_mode == "disabled" else None


# -- one set for the process (local, Easy Auth) ----------------------------------------------


@lru_cache
def _arm() -> ArmClient:
    return ArmClient(get_settings(), get_local_credential())


@lru_cache
def _secrets() -> SecretStore:
    """External gateways' master keys, kept locally (OS-encrypted)."""
    return SecretStore()


@lru_cache
def _gateway() -> GatewayConnection:
    settings = get_settings()
    stored = _secrets().get if settings.local and settings.external else None
    return GatewayConnection(settings, _arm(), stored_key=stored)


@lru_cache
def _service() -> PoolManagerService:
    return PoolManagerService(get_settings(), _arm(), _gateway())


@lru_cache
def _discovery() -> DeploymentDiscovery:
    return DeploymentDiscovery(get_settings(), _arm(), _service())


@lru_cache
def _monitor() -> PoolMonitor:
    return PoolMonitor(get_settings(), _arm(), _service())


@lru_cache
def _tester() -> ApiTester:
    return ApiTester(get_settings(), _service())


@lru_cache
def _benchmarks() -> BenchmarkRunner:
    """Load tests; not bound to a gateway (each run gets its URL), so a switch keeps them."""
    return BenchmarkRunner()


@lru_cache
def _cost() -> CostReader:
    return CostReader(get_settings(), _service())


@lru_cache
def _access() -> AccessChecker:
    discovery = _discovery()

    def discovered_accounts() -> list[str]:
        try:
            return [view.account_id for view in discovery.list()]
        except ServiceError as error:
            logger.warning("Cannot list discovered deployments: %s", error.message)
            return []

    return AccessChecker(
        get_settings(), _arm(), _service(), discovered_accounts, get_local_credential()
    )


@lru_cache
def _targets() -> TargetChooser:
    settings = get_settings()
    return TargetChooser(
        settings,
        get_local_credential(),
        secrets=_secrets() if settings.local else None,
        on_change=reset_target,
    )


def reset_target() -> None:
    """Rebuilds everything bound to one gateway once the setup wizard chose another.

    The local credential stays, so a browser sign-in survives the switch.
    """
    factories = (
        _targets,
        _monitor,
        _tester,
        _cost,
        _access,
        _discovery,
        _service,
        _gateway,
        _arm,
    )
    for factory in (*factories, get_settings):
        factory.cache_clear()


# -- the public app: who is calling, and their session --------------------------------------


@lru_cache
def _verifier() -> TokenVerifier:
    return TokenVerifier(get_settings())


@lru_cache
def _sessions() -> Sessions:
    settings = get_settings()
    guest = None
    if settings.guest_enabled:
        from azure.identity import ManagedIdentityCredential

        # The guest view reads with the app's own identity, which holds Reader roles only.
        guest = ManagedIdentityCredential(client_id=settings.federated_identity_client_id or None)
    return Sessions(settings, OboExchanger(settings).exchange, guest_credential=guest)


SettingsDep = Annotated[Settings, Depends(get_settings)]
# Sent by the public app's guest view instead of a token.
GUEST_HEADER = "X-Pool-Manager-Guest"


def get_verifier(settings: SettingsDep) -> TokenVerifier | None:
    return _verifier() if settings.public else None


def get_sessions(settings: SettingsDep) -> Sessions | None:
    return _sessions() if settings.public else None


def get_guest(request: Request, settings: SettingsDep) -> bool:
    """A visitor of the public app's read-only guest view: no token, and the guest header."""
    if not settings.public or "Authorization" in request.headers:
        return False
    if request.headers.get(GUEST_HEADER) != "1":
        return False
    if not settings.guest_enabled:
        raise ServiceError(404, "This app has no guest view; sign in instead")
    return True


Guest = Annotated[bool, Depends(get_guest)]


def get_user(
    request: Request,
    settings: SettingsDep,
    verifier: Annotated[TokenVerifier | None, Depends(get_verifier)],
    guest: Guest,
) -> EntraUser | None:
    """The verified caller of the public app; ``None`` for guests and in the other modes."""
    if not settings.public or guest:
        return None
    if verifier is None:
        raise ServiceError(500, "The public app has no token verifier")
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        raise ServiceError(401, "Sign in first")
    return verifier.verify(token.strip())


User = Annotated[EntraUser | None, Depends(get_user)]


def get_session(
    request: Request,
    user: User,
    guest: Guest,
    sessions: Annotated[Sessions | None, Depends(get_sessions)],
) -> UserSession | None:
    if user is None and not guest:
        return None
    if sessions is None:
        raise ServiceError(500, "The public app has no session store")
    if guest:
        # Behind Container Apps' proxy; uvicorn runs with --proxy-headers.
        return sessions.guest(request.client.host if request.client else "unknown")
    assert user is not None
    return sessions.get(user, parse_target(request.headers.get(TARGET_HEADER)))


Session = Annotated[UserSession | None, Depends(get_session)]


def get_request_settings(settings: SettingsDep, session: Session) -> Settings:
    """The settings for this request: in the public app, with the caller's chosen service."""
    return session.settings if session else settings


def get_arm(session: Session) -> ArmClient:
    return session.arm if session else _arm()


def get_service(session: Session) -> PoolManagerService:
    return session.service if session else _service()


def get_discovery(session: Session) -> DeploymentDiscovery:
    return session.discovery if session else _discovery()


def get_monitor(session: Session) -> PoolMonitor:
    return session.monitor if session else _monitor()


def get_tester(session: Session) -> ApiTester:
    return session.tester if session else _tester()


def get_benchmarks(session: Session) -> BenchmarkRunner:
    return session.benchmarks if session else _benchmarks()


def get_cost(session: Session) -> CostReader:
    return session.cost if session else _cost()


def get_access(session: Session) -> AccessChecker:
    return session.access if session else _access()


def get_targets(session: Session) -> TargetChooser:
    return session.targets if session else _targets()
