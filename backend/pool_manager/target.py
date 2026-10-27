"""Choosing the gateway to manage, from the local setup wizard (and the public app).

Locally the app can start without a gateway in the environment: once signed in, the user picks
a tenant and a subscription, then an APIM service or a LiteLLM gateway there, or creates one.
The choice is saved to ``.pool-manager.json`` and everything bound to one gateway is rebuilt
(``on_change``). The Easy Auth deployment always uses the gateway its deployment set. In the
public app (APIM only) every user chooses their own: their browser keeps the choice and sends it
with each request (sessions.py), so the server saves nothing.

``TargetChooser`` is what both products share: tenants, subscriptions, the current choice and
saving it. ``apim/target.py`` and ``litellm/target.py`` list, create, select and delete their
gateways.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Lock
from typing import Any

from .arm import ArmClient, ArmError, TokenCredential
from .config import Settings, clear_saved_target, read_saved_target, save_target
from .errors import ServiceError, arm_failure
from .models import Product, SubscriptionView, TargetSource, TargetView, TenantView

logger = logging.getLogger(__name__)

SUBSCRIPTIONS_API_VERSION = "2022-12-01"


def resource_group_of(resource_id: str) -> str:
    parts = resource_id.split("/")
    lowered = [part.casefold() for part in parts]
    return parts[lowered.index("resourcegroups") + 1] if "resourcegroups" in lowered else ""


class TargetChooser:
    def __init__(
        self,
        settings: Settings,
        credential: TokenCredential | None,
        *,
        transport: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        state_file: Path | None = None,
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        self.settings = settings
        self.sleep = sleep
        self._credential = credential
        self._transport = transport
        self._state_file = state_file
        self._on_change = on_change
        self._lock = Lock()
        self._clients: dict[str, ArmClient] = {}

    def arm(self, tenant_id: str | None) -> ArmClient:
        """ARM from the root, with a token for ``tenant_id`` (the default tenant if None)."""
        key = (tenant_id or "").casefold()
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = ArmClient(
                    self.settings,
                    self._credential,
                    self._transport,
                    self.sleep,
                    tenant_id=tenant_id,
                )
                self._clients[key] = client
        return client

    def forget(self) -> None:
        """Drops cached tokens, e.g. after the user signed in again."""
        with self._lock:
            self._clients.clear()

    def close(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()

    def require_local(self) -> None:
        if self.settings.guest:
            raise ServiceError(403, "The guest view shows one gateway; sign in to choose your own")
        if not (self.settings.local or self.settings.public):
            raise ServiceError(400, "The deployed app manages the gateway set at deployment")

    def require_changeable(self) -> None:
        self.require_local()
        if not self.settings.can_choose_target:
            raise ServiceError(
                409,
                "The gateway is set in the environment or backend/.env "
                "(POOL_MANAGER_SUBSCRIPTION_ID, _RESOURCE_GROUP, and _APIM_NAME or _GATEWAY_NAME "
                "or _GATEWAY_URL). Remove those to choose one here.",
            )

    # -- the choice --------------------------------------------------------------------

    def current(self) -> TargetView:
        settings = self.settings
        source: TargetSource = (
            "none"
            if not settings.has_target
            else "guest"
            if settings.guest
            else "browser"
            if settings.public
            else "saved"
            if settings.can_choose_target
            else "environment"
        )
        litellm = settings.has_target and settings.product == "litellm"
        return TargetView(
            product=settings.product if settings.has_target else None,
            gateway_name=settings.target_name or None,
            gateway_kind=("external" if settings.external else "aca") if litellm else None,
            subscription_id=settings.subscription_id or None,
            resource_group=settings.resource_group or None,
            gateway_url=settings.gateway_url or None,
            tenant_id=settings.tenant_id or None,
            source=source,
            can_change=settings.can_choose_target,
            cluster_id=settings.gateway_cluster_id or None,
            provisioning=bool(settings.gateway_provisioning),
        )

    def saved(self) -> dict[str, str] | None:
        """The target saved locally, which may be newer than these settings."""
        return read_saved_target(self._state_file)

    def save(self, product: Product, target: Mapping[str, str]) -> None:
        """Remembers the chosen gateway and rebuilds everything bound to the old one."""
        if self.settings.public:
            return  # The user's browser keeps it.
        save_target(product, target, self._state_file)
        self._on_change()

    def clear(self) -> None:
        """Forgets the chosen gateway, e.g. after it was deleted; setup then asks again."""
        if self.settings.public:
            return
        clear_saved_target(self._state_file)
        self._on_change()

    # -- reads -------------------------------------------------------------------------

    def tenants(self) -> list[TenantView]:
        self.require_local()
        try:
            items = list(self.arm(None).list_arm("/tenants", api_version=SUBSCRIPTIONS_API_VERSION))
        except ArmError as error:
            raise arm_failure(error, "list your tenants") from error
        views = [
            TenantView(
                tenant_id=str(item["tenantId"]),
                display_name=item.get("displayName"),
                default_domain=item.get("defaultDomain"),
            )
            for item in items
            if item.get("tenantId")
        ]
        return sorted(views, key=lambda view: (view.display_name or view.tenant_id).casefold())

    def subscriptions(self, tenant_id: str | None) -> list[SubscriptionView]:
        self.require_local()
        try:
            items = list(
                self.arm(tenant_id).list_arm(
                    "/subscriptions", api_version=SUBSCRIPTIONS_API_VERSION
                )
            )
        except ArmError as error:
            raise arm_failure(error, "list the subscriptions of this tenant") from error
        views = [
            SubscriptionView(
                subscription_id=str(item["subscriptionId"]),
                display_name=str(item.get("displayName") or item["subscriptionId"]),
                tenant_id=item.get("tenantId"),
                state=str(item.get("state") or "Unknown"),
            )
            for item in items
            if item.get("subscriptionId")
        ]
        return sorted(views, key=lambda view: view.display_name.casefold())
