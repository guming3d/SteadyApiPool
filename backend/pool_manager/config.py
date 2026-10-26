from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Pools with session affinity need a preview API version; 2024-05-01 drops
# ``pool.sessionAffinity`` on both read and write, so pools use the preview and the rest GA.
GA_API_VERSION = "2024-05-01"
DEFAULT_POOL_API_VERSION = "2025-09-01-preview"


TARGET_FIELDS = ("subscription_id", "resource_group", "apim_name")
# Where the local app remembers the APIM service chosen in the setup wizard.
STATE_FILE = Path(__file__).resolve().parent.parent / ".pool-manager.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POOL_MANAGER_", env_file=".env", extra="ignore")

    # The APIM service to manage. Set in the environment (or .env) they are fixed; locally they
    # may be left out and chosen, or created, in the setup wizard instead.
    subscription_id: str = ""
    resource_group: str = ""
    apim_name: str = ""
    # Tenant of the subscription, so local tokens are requested there and not in the home tenant.
    tenant_id: str = ""

    arm_endpoint: str = "https://management.azure.com"
    api_version: str = GA_API_VERSION
    pool_api_version: str = DEFAULT_POOL_API_VERSION

    # Prefix for backends and named values this app creates.
    id_prefix: str = Field(default="pool-", pattern=r"^[a-z0-9-]{1,20}$")
    # Comma-separated ID prefixes of resources owned by another control plane: they are shown
    # but never modified. Empty = none.
    protected_prefixes: str = ""

    # Subscriptions searched for Azure OpenAI / Foundry deployments. Empty = the APIM subscription.
    discovery_subscriptions: str = ""
    cognitive_api_version: str = "2024-10-01"

    # ``easy_auth`` trusts App Service Authentication headers; ``disabled`` is for local dev;
    # ``entra`` is the public app (see entra.py): every user signs in with their own Entra
    # account and the app acts in Azure as them, with no APIM service of its own.
    auth_mode: Literal["easy_auth", "disabled", "entra"] = "easy_auth"
    # The public app's multi-tenant app registration, and the client ID of the user-assigned
    # managed identity it uses as its federated credential (it has no secret).
    entra_client_id: str = ""
    federated_identity_client_id: str = ""
    entra_authority: str = "https://login.microsoftonline.com"
    # Tenants whose users may sign in to the public app. Empty = any work or school tenant.
    entra_allowed_tenants: str = ""
    # A signed-in user's in-memory session (their Azure token and caches) ends this long after
    # their last request, or when they sign out.
    session_idle_seconds: int = Field(default=1800, ge=60)
    max_sessions: int = Field(default=500, ge=1)
    # Requests per user per minute in the public app.
    user_requests_per_minute: int = Field(default=600, ge=1)
    # The public app's guest view: anyone can browse this one APIM service (and the Foundry
    # deployments in these subscriptions; empty = the service's) read-only, through the app's
    # managed identity, which holds Reader roles only. All three or none.
    guest_subscription_id: str = ""
    guest_resource_group: str = ""
    guest_apim_name: str = ""
    guest_discovery_subscriptions: str = ""
    # Guest requests per client address per minute, and for all guests together.
    guest_requests_per_minute: int = Field(default=300, ge=1)
    guest_requests_per_minute_total: int = Field(default=3000, ge=1)
    # Comma-separated Entra UPNs/emails or object IDs allowed to make changes. Others are read-only.
    owners: str = ""
    # Also let users change pools whose own Azure roles already allow every APIM write this app
    # makes. Needs the signed-in user's ARM token from Easy Auth (see README).
    rbac_writers: bool = True
    # Easy Auth also obtains an ARM token for the signed-in user (infra parameter
    # enableDelegatedAzureAccess). Without it the setup wizard can check but not fix.
    delegated_access: bool = False
    # Locally, verify TLS against the operating system's certificate store instead of Python's
    # bundled one, so a company CA installed on the machine (TLS inspection proxies) is trusted.
    system_trust: bool = True

    _target_saved: bool = PrivateAttr(default=False)
    # Settings of the public app's guest session (as_guest).
    _guest: bool = PrivateAttr(default=False)

    @property
    def has_target(self) -> bool:
        return all(getattr(self, name) for name in TARGET_FIELDS)

    @property
    def local(self) -> bool:
        return self.auth_mode == "disabled"

    @property
    def public(self) -> bool:
        return self.auth_mode == "entra"

    @property
    def operator_is_user(self) -> bool:
        """Every Azure call runs as the user: their own sign-in locally, their token in the
        public app. The Easy Auth deployment and the public app's guests use the app's own
        identity."""
        return self.local or (self.public and not self._guest)

    @property
    def guest(self) -> bool:
        """This is the public app's read-only guest session."""
        return self._guest

    @property
    def guest_enabled(self) -> bool:
        return self.public and all(getattr(self, f"guest_{name}") for name in TARGET_FIELDS)

    @property
    def can_choose_target(self) -> bool:
        """Locally unless the environment fixes the APIM service; always in the public app,
        where each user's browser remembers their own choice."""
        if self.public:
            return not self._guest
        return self.local and (not self.has_target or self._target_saved)

    @property
    def allowed_tenant_set(self) -> frozenset[str]:
        return frozenset(
            item.strip().casefold()
            for item in self.entra_allowed_tenants.split(",")
            if item.strip()
        )

    @property
    def protected_prefix_list(self) -> list[str]:
        return [
            item.strip().casefold() for item in self.protected_prefixes.split(",") if item.strip()
        ]

    @property
    def owner_set(self) -> frozenset[str]:
        return frozenset(item.strip().casefold() for item in self.owners.split(",") if item.strip())

    @property
    def discovery_subscription_list(self) -> list[str]:
        items = [item.strip() for item in self.discovery_subscriptions.split(",") if item.strip()]
        return items or ([self.subscription_id] if self.subscription_id else [])

    @property
    def session_affinity_supported(self) -> bool:
        return self.pool_api_version != GA_API_VERSION

    def validate_runtime(self) -> None:
        # App Service sets WEBSITE_SITE_NAME and Container Apps CONTAINER_APP_NAME. Never serve
        # an unauthenticated control plane there.
        hosted = os.environ.get("WEBSITE_SITE_NAME") or os.environ.get("CONTAINER_APP_NAME")
        if self.local and hosted:
            raise RuntimeError("POOL_MANAGER_AUTH_MODE=disabled is not allowed on Azure hosting")
        names = [f"POOL_MANAGER_{name.upper()}" for name in TARGET_FIELDS]
        given = [bool(getattr(self, name)) for name in TARGET_FIELDS]
        if self.public:
            if not self.entra_client_id:
                raise RuntimeError(
                    "POOL_MANAGER_AUTH_MODE=entra needs POOL_MANAGER_ENTRA_CLIENT_ID"
                )
            # Each user chooses their own service and subscriptions; nothing is shared.
            shared = [
                f"POOL_MANAGER_{name.upper()}" for name in TARGET_FIELDS if getattr(self, name)
            ]
            if self.discovery_subscriptions:
                shared.append("POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS")
            if shared:
                raise RuntimeError(f"The public app can't use {', '.join(shared)}; unset it")
            guest = [bool(getattr(self, f"guest_{name}")) for name in TARGET_FIELDS]
            if any(guest) and not all(guest):
                fields = [f"POOL_MANAGER_GUEST_{name.upper()}" for name in TARGET_FIELDS]
                raise RuntimeError(f"Set all of {', '.join(fields)} for the guest view, or none")
            return
        if any(given) and not all(given):
            raise RuntimeError(f"Set all of {', '.join(names)}, or none to choose in the app")
        # Only the local setup wizard can choose a service; deployed, Bicep sets these.
        if self.auth_mode != "disabled" and not all(given):
            raise RuntimeError(f"Set {', '.join(names)}")

    def with_saved_target(self, target: Mapping[str, str]) -> Settings:
        settings = self.model_copy(update=dict(target))
        settings._target_saved = True
        return settings

    def as_guest(self) -> Settings:
        """The guest session's settings: the configured service, read with the app's identity."""
        settings = self.model_copy(
            update={
                "subscription_id": self.guest_subscription_id,
                "resource_group": self.guest_resource_group,
                "apim_name": self.guest_apim_name,
                "discovery_subscriptions": self.guest_discovery_subscriptions,
                # The managed identity's token comes from its own tenant.
                "tenant_id": "",
            }
        )
        settings._guest = True
        return settings


def read_saved_target(path: Path | None = None) -> dict[str, str] | None:
    """The service saved by the setup wizard, or ``None`` when there is none (or it is unusable)."""
    path = path or STATE_FILE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.warning("Ignoring %s: %s", path, error)
        return None
    target = state.get("target") if isinstance(state, dict) else None
    if not isinstance(target, dict):
        return None
    values = {name: str(target.get(name) or "") for name in (*TARGET_FIELDS, "tenant_id")}
    return values if all(values[name] for name in TARGET_FIELDS) else None


def save_target(target: Mapping[str, str], path: Path | None = None) -> None:
    (path or STATE_FILE).write_text(
        json.dumps({"target": dict(target)}, indent=2) + "\n", encoding="utf-8"
    )


def clear_saved_target(path: Path | None = None) -> None:
    """Forgets the chosen service, e.g. after it was deleted; setup then asks again."""
    (path or STATE_FILE).unlink(missing_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime()
    if settings.auth_mode == "disabled" and not settings.has_target:
        saved = read_saved_target()
        if saved:
            settings = settings.with_saved_target(saved)
    return settings
