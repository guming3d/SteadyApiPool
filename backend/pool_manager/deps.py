"""Dependencies, replaced in tests through ``app.dependency_overrides``.

Locally and in the Easy Auth deployment there is one set of components for the process (the
``lru_cache`` factories): the shared ones, and each product's, built when its routes are first
used. In the public app (APIM only) each request resolves to the caller's own session
(sessions.py), built for them and the service their browser chose.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request

from .access import AccessChecker
from .apim.access import ApimAccessChecker
from .apim.api_test import ApimTester
from .apim.cost import ApimCost
from .apim.metrics import ApimMonitor
from .apim.service import ApimService
from .apim.target import ApimTargets
from .arm import ArmClient
from .config import Settings, get_settings
from .credentials import LocalCredential
from .discovery import DeploymentDiscovery, MemberStore
from .entra import EntraUser, OboExchanger, TokenVerifier
from .errors import ServiceError
from .litellm.access import LiteLLMAccessChecker
from .litellm.api_test import LiteLLMTester
from .litellm.benchmark import BenchmarkRunner
from .litellm.cleanup import GatewayCleanup
from .litellm.cost import LiteLLMCost
from .litellm.gateway import GatewayConnection
from .litellm.metrics import LiteLLMMonitor
from .litellm.service import LiteLLMService
from .litellm.target import LiteLLMTargets
from .secret_store import SecretStore
from .sessions import TARGET_HEADER, Sessions, UserSession, parse_target
from .target import TargetChooser

logger = logging.getLogger(__name__)


@lru_cache
def get_local_credential() -> LocalCredential | None:
    """Locally the operator is the user, who can switch to a browser sign-in from the UI."""
    return LocalCredential() if get_settings().auth_mode == "disabled" else None


# -- shared, one set for the process (local, Easy Auth) ---------------------------------------


@lru_cache
def _arm() -> ArmClient:
    return ArmClient(get_settings(), get_local_credential())


@lru_cache
def _secrets() -> SecretStore:
    """External LiteLLM gateways' master keys, kept locally (OS-encrypted)."""
    return SecretStore()


@lru_cache
def _targets() -> TargetChooser:
    return TargetChooser(get_settings(), get_local_credential(), on_change=reset_target)


# -- APIM ------------------------------------------------------------------------------------


@lru_cache
def _apim_service() -> ApimService:
    return ApimService(get_settings(), _arm())


@lru_cache
def _apim_monitor() -> ApimMonitor:
    return ApimMonitor(get_settings(), _arm(), _apim_service())


@lru_cache
def _apim_tester() -> ApimTester:
    return ApimTester(get_settings(), _apim_service())


@lru_cache
def _apim_cost() -> ApimCost:
    return ApimCost(get_settings(), _apim_service())


@lru_cache
def _apim_targets() -> ApimTargets:
    return ApimTargets(_targets())


# -- LiteLLM ---------------------------------------------------------------------------------


@lru_cache
def _gateway() -> GatewayConnection:
    settings = get_settings()
    stored = _secrets().get if settings.local and settings.external else None
    return GatewayConnection(settings, _arm(), stored_key=stored)


@lru_cache
def _litellm_service() -> LiteLLMService:
    return LiteLLMService(get_settings(), _arm(), _gateway())


@lru_cache
def _litellm_monitor() -> LiteLLMMonitor:
    return LiteLLMMonitor(get_settings(), _arm(), _litellm_service())


@lru_cache
def _litellm_tester() -> LiteLLMTester:
    return LiteLLMTester(get_settings(), _litellm_service())


@lru_cache
def _benchmarks() -> BenchmarkRunner:
    """Load tests; not bound to a gateway (each run gets its URL), so a switch keeps them."""
    return BenchmarkRunner()


@lru_cache
def _litellm_cost() -> LiteLLMCost:
    return LiteLLMCost(get_settings(), _litellm_service())


@lru_cache
def _cleanups() -> GatewayCleanup:
    """Cleanups after failed deployments; like load tests, they outlive a gateway switch."""
    return GatewayCleanup()


@lru_cache
def _litellm_targets() -> LiteLLMTargets:
    settings = get_settings()
    return LiteLLMTargets(
        _targets(), secrets=_secrets() if settings.local else None, cleanup=_cleanups()
    )


# -- the chosen gateway, whichever product it is ---------------------------------------------


def _members() -> MemberStore | None:
    settings = get_settings()
    product = settings.product if settings.has_target else None
    if product == "apim":
        return _apim_service()
    if product == "litellm":
        return _litellm_service()
    return None


@lru_cache
def _discovery() -> DeploymentDiscovery:
    return DeploymentDiscovery(get_settings(), _arm(), _members())


@lru_cache
def _access() -> AccessChecker:
    settings = get_settings()
    discovery = _discovery()

    def discovered_accounts() -> list[str]:
        try:
            return [view.account_id for view in discovery.list()]
        except ServiceError as error:
            logger.warning("Cannot list discovered deployments: %s", error.message)
            return []

    credential = get_local_credential()
    product = settings.product if settings.has_target else None
    if product == "apim":
        return ApimAccessChecker(settings, _arm(), _apim_service(), discovered_accounts, credential)
    if product == "litellm":
        return LiteLLMAccessChecker(
            settings, _arm(), _litellm_service(), discovered_accounts, credential
        )
    # No gateway yet: the report asks for one.
    return AccessChecker(settings, _arm(), None, discovered_accounts, credential)


def reset_target() -> None:
    """Rebuilds everything bound to one gateway once the setup wizard chose another.

    The local credential and the secret store stay, so a browser sign-in survives the switch.
    """
    factories = (
        _targets,
        _access,
        _discovery,
        _apim_targets,
        _apim_monitor,
        _apim_tester,
        _apim_cost,
        _apim_service,
        _litellm_targets,
        _litellm_monitor,
        _litellm_tester,
        _litellm_cost,
        _litellm_service,
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


# Shared.


def get_arm(session: Session) -> ArmClient:
    return session.arm if session else _arm()


def get_discovery(session: Session) -> DeploymentDiscovery:
    return session.discovery if session else _discovery()


def get_access(session: Session) -> AccessChecker:
    return session.access if session else _access()


def get_targets(session: Session) -> TargetChooser:
    return session.targets if session else _targets()


# APIM.


def get_apim_service(session: Session) -> ApimService:
    return session.service if session else _apim_service()


def get_apim_monitor(session: Session) -> ApimMonitor:
    return session.monitor if session else _apim_monitor()


def get_apim_tester(session: Session) -> ApimTester:
    return session.tester if session else _apim_tester()


def get_apim_cost(session: Session) -> ApimCost:
    return session.cost if session else _apim_cost()


def get_apim_targets(session: Session) -> ApimTargets:
    return session.apim_targets if session else _apim_targets()


# LiteLLM (never in the public app, which manages APIM services only).


def get_litellm_service() -> LiteLLMService:
    return _litellm_service()


def get_litellm_monitor() -> LiteLLMMonitor:
    return _litellm_monitor()


def get_litellm_tester() -> LiteLLMTester:
    return _litellm_tester()


def get_litellm_cost() -> LiteLLMCost:
    return _litellm_cost()


def get_benchmarks() -> BenchmarkRunner:
    return _benchmarks()


def get_litellm_targets() -> LiteLLMTargets:
    return _litellm_targets()
