"""Per-user sessions for the public app: the isolation between users signed in at once.

The public app is not available with the LiteLLM gateway yet (config.py refuses
``auth_mode=entra``); this module is kept, type-checked, for when it returns.

Each signed-in user gets their own set of the components the local app uses (ARM client,
service, discovery, metrics, setup checks, ...), built for the gateway their browser chose
and calling Azure with that user's own token. Every cache those components keep (deployments,
metrics, permissions, cost) therefore belongs to one user and one gateway; nothing one user
loaded can answer another user's request, even in the same tenant, where their roles may differ.

The guest view is one more session, shared by every visitor who browses without signing in: it
shows the one gateway the deployment configured (``guest_*`` settings), read with the app's own
managed identity, which holds Reader roles only, and every route that changes or reveals
anything refuses guests (auth.py). Its caches are shared on purpose: all guests see the same.

Sessions live in memory only, keyed by the verified tenant and object ID, never by anything the
browser could choose. A session ends after ``session_idle_seconds`` without requests, when the
store is full (the least recently used goes first), or when the user signs out; with it go the
user's ARM tokens. The process keeps no state files, so any replica can serve any request.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any
from urllib.parse import unquote

import httpx

from .access import AccessChecker
from .arm import ArmClient, TokenCredential
from .config import Settings
from .cost import CostReader
from .discovery import DeploymentDiscovery
from .entra import EntraUser, Exchange, UserCredential
from .errors import ServiceError
from .litellm.api_test import ApiTester
from .litellm.benchmark import BenchmarkRunner
from .litellm.gateway import GatewayConnection
from .litellm.service import PoolManagerService
from .metrics import PoolMonitor
from .models import GATEWAY_NAME_PATTERN
from .target import TargetChooser

logger = logging.getLogger(__name__)

TARGET_HEADER = "X-Pool-Manager-Target"
# Subscription IDs are GUIDs; ARM itself rejects anything else, so this only keeps the header sane.
_SUBSCRIPTION = re.compile(r"[0-9A-Za-z-]{1,64}")
_RESOURCE_GROUP = re.compile(r"[-\w.()]{0,89}[-\w()]")
_GATEWAY_NAME = re.compile(GATEWAY_NAME_PATTERN)
# One user rarely needs more than a few gateways open at once.
_SESSIONS_PER_USER = 8
GUEST_KEY = "guest"

Target = tuple[str, str, str]


def parse_target(value: str | None) -> Target | None:
    """``<subscription>/<resource group>/<gateway name>``, each part URL-encoded, from the
    header the browser sends with the gateway it chose; ``None`` when none is chosen yet."""
    if not value:
        return None
    parts = [unquote(part) for part in value.split("/")]
    if (
        len(parts) != 3
        or not _SUBSCRIPTION.fullmatch(parts[0])
        or not _RESOURCE_GROUP.fullmatch(parts[1])
        or not _GATEWAY_NAME.fullmatch(parts[2])
    ):
        raise ServiceError(400, f"{TARGET_HEADER} must be <subscription>/<resource group>/<name>")
    return parts[0], parts[1], parts[2]


@dataclass
class UserSession:
    """One user's view of one gateway (or of none, while they choose one)."""

    user_key: str
    settings: Settings
    credential: TokenCredential
    arm: ArmClient
    service: PoolManagerService
    discovery: DeploymentDiscovery
    monitor: PoolMonitor
    tester: ApiTester
    benchmarks: BenchmarkRunner
    cost: CostReader
    access: AccessChecker
    targets: TargetChooser
    last_used: float = 0.0

    def close(self) -> None:
        self.arm.close()
        self.tester.close()
        self.benchmarks.close()
        self.cost.close()
        self.targets.close()


@dataclass
class _Window:
    hits: deque[float] = field(default_factory=deque)


class Sessions:
    def __init__(
        self,
        settings: Settings,
        exchange: Exchange,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        guest_credential: TokenCredential | None = None,
    ) -> None:
        self._settings = settings
        self._exchange = exchange
        self._guest_credential = guest_credential
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._lock = Lock()
        self._sessions: OrderedDict[tuple[str, str], UserSession] = OrderedDict()
        self._credentials: dict[str, UserCredential] = {}
        self._windows: dict[str, _Window] = {}

    def get(self, user: EntraUser, target: Target | None) -> UserSession:
        now = self._clock()
        self._count(user.key, now, self._settings.user_requests_per_minute)
        key = (user.key, "/".join(target).casefold() if target else "")
        with self._lock:
            self._expire(now)
            credential = self._credentials.get(user.key)
            if credential is None:
                credential = UserCredential(user, self._exchange)
                self._credentials[user.key] = credential
            # Every request renews the token the session exchanges for ARM.
            credential.update(user)
            session = self._sessions.get(key)
            if session is None:
                updates: dict[str, Any] = {"tenant_id": user.tenant_id}
                if target:
                    updates.update(
                        subscription_id=target[0], resource_group=target[1], gateway_name=target[2]
                    )
                session = self._open(key, self._settings.model_copy(update=updates), credential)
                self._trim(user.key)
                # Making room may have closed this user's other sessions, and their credential
                # with them; this session keeps using it.
                self._credentials[user.key] = credential
            session.last_used = now
            self._sessions.move_to_end(key)
        return session

    def guest(self, client: str) -> UserSession:
        """The shared read-only guest session, for a visitor at address ``client``."""
        if not self._settings.guest_enabled or self._guest_credential is None:
            raise ServiceError(404, "This app has no guest view; sign in instead")
        now = self._clock()
        self._count(f"{GUEST_KEY}:*", now, self._settings.guest_requests_per_minute_total)
        self._count(f"{GUEST_KEY}:{client}", now, self._settings.guest_requests_per_minute)
        key = (GUEST_KEY, "")
        with self._lock:
            self._expire(now)
            session = self._sessions.get(key)
            if session is None:
                session = self._open(key, self._settings.as_guest(), self._guest_credential)
            session.last_used = now
            self._sessions.move_to_end(key)
        return session

    def drop(self, user_key: str) -> int:
        """Signs a user out of the app: their sessions and Azure tokens are gone."""
        with self._lock:
            keys = [key for key in self._sessions if key[0] == user_key]
            for key in keys:
                self._sessions.pop(key).close()
            credential = self._credentials.pop(user_key, None)
            self._windows.pop(user_key, None)
        if credential is not None:
            credential.clear()
        return len(keys)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    # -- internals, under the lock ------------------------------------------------------

    def _count(self, user_key: str, now: float, limit: int) -> None:
        with self._lock:
            window = self._windows.setdefault(user_key, _Window())
            while window.hits and now - window.hits[0] > 60:
                window.hits.popleft()
            if len(window.hits) >= limit:
                raise ServiceError(429, "Too many requests; wait a minute and try again")
            window.hits.append(now)

    def _expire(self, now: float) -> None:
        idle = self._settings.session_idle_seconds
        for key in [key for key, item in self._sessions.items() if now - item.last_used > idle]:
            self._close(key)

    def _trim(self, user_key: str) -> None:
        mine = [key for key in self._sessions if key[0] == user_key]
        for key in mine[: max(0, len(mine) - _SESSIONS_PER_USER)]:
            self._close(key)

    def _close(self, key: tuple[str, str]) -> None:
        self._sessions.pop(key).close()
        if not any(other[0] == key[0] for other in self._sessions):
            credential = self._credentials.pop(key[0], None)
            if credential is not None:
                credential.clear()
            self._windows.pop(key[0], None)

    def _open(
        self, key: tuple[str, str], settings: Settings, credential: TokenCredential
    ) -> UserSession:
        while len(self._sessions) >= self._settings.max_sessions:
            self._close(next(iter(self._sessions)))
        session = self._build(key[0], settings, credential)
        self._sessions[key] = session
        return session

    def _build(self, user_key: str, settings: Settings, credential: TokenCredential) -> UserSession:
        arm = ArmClient(settings, credential, self._transport, self._sleep)
        gateway = GatewayConnection(settings, arm, transport=self._transport, sleep=self._sleep)
        service = PoolManagerService(settings, arm, gateway)
        discovery = DeploymentDiscovery(settings, arm, service)

        def discovered_accounts() -> list[str]:
            try:
                return [view.account_id for view in discovery.list()]
            except ServiceError as error:
                logger.info("Cannot list discovered deployments: %s", error.message)
                return []

        return UserSession(
            user_key=user_key,
            settings=settings,
            credential=credential,
            arm=arm,
            service=service,
            discovery=discovery,
            monitor=PoolMonitor(settings, arm, service),
            tester=ApiTester(settings, service, transport=self._transport),
            benchmarks=BenchmarkRunner(
                self._transport if isinstance(self._transport, httpx.AsyncBaseTransport) else None
            ),
            cost=CostReader(settings, service, transport=self._transport),
            access=AccessChecker(settings, arm, service, discovered_accounts),
            targets=TargetChooser(
                settings, credential, transport=self._transport, sleep=self._sleep
            ),
        )
