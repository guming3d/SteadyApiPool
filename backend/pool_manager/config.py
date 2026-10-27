from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PrivateAttr, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import Product

logger = logging.getLogger(__name__)

# APIM: pools with session affinity need a preview API version; 2024-05-01 drops
# ``pool.sessionAffinity`` on both read and write, so pools use the preview and the rest GA.
GA_API_VERSION = "2024-05-01"
DEFAULT_POOL_API_VERSION = "2025-09-01-preview"
# LiteLLM: Container Apps' GA ARM API version.
CONTAINER_APPS_API_VERSION = "2025-01-01"

# An APIM service is addressed by these.
APIM_FIELDS = ("subscription_id", "resource_group", "apim_name")
# A LiteLLM gateway on Container Apps by these; an external one (e.g. on AKS) by its URL.
LITELLM_FIELDS = ("subscription_id", "resource_group", "gateway_name")
# What the setup wizard saves with the chosen gateway, per product. Never a master key.
SAVED_FIELDS: dict[Product, tuple[str, ...]] = {
    "apim": (*APIM_FIELDS, "tenant_id"),
    "litellm": (
        *LITELLM_FIELDS,
        "tenant_id",
        "gateway_url",
        "gateway_principal_id",
        "gateway_app_insights_id",
        "gateway_cluster_id",
        "gateway_provisioning",
        "gateway_started_at",
    ),
}
# Where the local app remembers the gateway chosen in the setup wizard.
STATE_FILE = Path(__file__).resolve().parent.parent / ".pool-manager.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POOL_MANAGER_", env_file=".env", extra="ignore")

    # The gateway to manage: an APIM service (apim_name) or a LiteLLM gateway (gateway_name),
    # never both. Set in the environment (or .env) it is fixed; locally it may be left out and
    # chosen, created or connected in the setup wizard instead.
    subscription_id: str = ""
    resource_group: str = ""
    apim_name: str = ""
    # A LiteLLM gateway on Azure Container Apps is its Container App: the app reads the gateway's
    # URL, identity and master key (a Container App secret) through ARM.
    gateway_name: str = ""
    # An external LiteLLM gateway (on AKS, or any LiteLLM proxy) is its URL, with gateway_name as
    # its display name and the master key here or in the local secret store. subscription_id is
    # then only the subscription searched for Foundry deployments by default.
    gateway_url: str = ""
    gateway_master_key: SecretStr = SecretStr("")
    # External gateways: the managed identity (object ID) the gateway calls Foundry as, and the
    # Application Insights component its OpenTelemetry goes to. Container Apps report their own.
    gateway_principal_id: str = ""
    gateway_app_insights_id: str = ""
    # An external gateway the app deployed on AKS: its cluster (for the maintenance commands and
    # deleting it), and while the app is still installing it, "aks" and when it started.
    gateway_cluster_id: str = ""
    gateway_provisioning: str = ""
    gateway_started_at: str = ""
    # Tenant of the subscription, so local tokens are requested there and not in the home tenant.
    tenant_id: str = ""

    arm_endpoint: str = "https://management.azure.com"
    api_version: str = GA_API_VERSION
    pool_api_version: str = DEFAULT_POOL_API_VERSION
    container_apps_api_version: str = CONTAINER_APPS_API_VERSION

    # Prefix for members, named values and keys this app creates.
    id_prefix: str = Field(default="pool-", pattern=r"^[a-z0-9-]{1,20}$")
    # Comma-separated ID prefixes of pools and members owned by another control plane: they are
    # shown but never modified. Empty = none.
    protected_prefixes: str = ""

    # Subscriptions searched for Azure OpenAI / Foundry deployments. Empty = the gateway's.
    discovery_subscriptions: str = ""
    cognitive_api_version: str = "2024-10-01"

    # ``easy_auth`` trusts App Service Authentication headers; ``disabled`` is for local dev;
    # ``entra`` is the public app (see entra.py): every user signs in with their own Entra
    # account and the app acts in Azure as them, with no gateway of its own. It manages APIM
    # services only.
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
    # Also let users change pools whose own Azure roles already allow it: every APIM write this
    # app makes, or reading a LiteLLM gateway's master key. Needs the signed-in user's ARM token
    # from Easy Auth (see README).
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
    def product(self) -> Product | None:
        """The kind of gateway chosen, from which name is set; None until one is."""
        if self.apim_name:
            return "apim"
        if self.gateway_name or self.gateway_url:
            return "litellm"
        return None

    @property
    def external(self) -> bool:
        """A LiteLLM gateway reached by its URL rather than through a Container App."""
        return self.product == "litellm" and bool(self.gateway_url)

    @property
    def has_target(self) -> bool:
        if self.product == "apim":
            return all(getattr(self, name) for name in APIM_FIELDS)
        if self.external:
            return bool(self.gateway_name)
        return all(getattr(self, name) for name in LITELLM_FIELDS)

    @property
    def target_name(self) -> str:
        """The chosen gateway's name, whichever product it is."""
        return self.apim_name or self.gateway_name

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
        return self.public and all(getattr(self, f"guest_{name}") for name in APIM_FIELDS)

    @property
    def can_choose_target(self) -> bool:
        """Locally unless the environment fixes the gateway; always in the public app,
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
        if self.apim_name and (self.gateway_name or self.gateway_url):
            raise RuntimeError(
                "Set POOL_MANAGER_APIM_NAME (APIM) or POOL_MANAGER_GATEWAY_NAME (LiteLLM), not both"
            )
        if self.public:
            self._validate_public()
            return
        if self.external:
            if not self.gateway_name:
                raise RuntimeError("POOL_MANAGER_GATEWAY_URL needs POOL_MANAGER_GATEWAY_NAME")
            if not self.local and not self.gateway_master_key.get_secret_value():
                raise RuntimeError(
                    "POOL_MANAGER_GATEWAY_URL needs POOL_MANAGER_GATEWAY_MASTER_KEY when deployed"
                )
            return
        fields = LITELLM_FIELDS if self.product == "litellm" else APIM_FIELDS
        names = [f"POOL_MANAGER_{name.upper()}" for name in fields]
        given = [bool(getattr(self, name)) for name in fields]
        if any(given) and not all(given):
            raise RuntimeError(f"Set all of {', '.join(names)}, or none to choose in the app")
        # Only the local setup wizard can choose a gateway; deployed, Bicep sets these.
        if self.auth_mode != "disabled" and not all(given):
            raise RuntimeError(
                "Set POOL_MANAGER_SUBSCRIPTION_ID, POOL_MANAGER_RESOURCE_GROUP and "
                "POOL_MANAGER_APIM_NAME (APIM) or POOL_MANAGER_GATEWAY_NAME (LiteLLM on Container "
                "Apps), or POOL_MANAGER_GATEWAY_NAME and POOL_MANAGER_GATEWAY_URL"
            )

    def _validate_public(self) -> None:
        if not self.entra_client_id:
            raise RuntimeError("POOL_MANAGER_AUTH_MODE=entra needs POOL_MANAGER_ENTRA_CLIENT_ID")
        # Each user chooses their own service and subscriptions; nothing is shared.
        shared = [
            f"POOL_MANAGER_{name.upper()}"
            for name in (*APIM_FIELDS, "gateway_name", "gateway_url")
            if getattr(self, name)
        ]
        if self.discovery_subscriptions:
            shared.append("POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS")
        if shared:
            raise RuntimeError(f"The public app can't use {', '.join(shared)}; unset it")
        guest = [bool(getattr(self, f"guest_{name}")) for name in APIM_FIELDS]
        if any(guest) and not all(guest):
            fields = [f"POOL_MANAGER_GUEST_{name.upper()}" for name in APIM_FIELDS]
            raise RuntimeError(f"Set all of {', '.join(fields)} for the guest view, or none")

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


def saved_product(target: Mapping[str, object]) -> Product | None:
    """Which product a saved target is for. Files from before both products were in one app
    have no ``product``; their fields tell."""
    product = target.get("product")
    if product == "apim" or product == "litellm":
        return product
    if target.get("apim_name"):
        return "apim"
    if target.get("gateway_name"):
        return "litellm"
    return None


def read_saved_target(path: Path | None = None) -> dict[str, str] | None:
    """The gateway saved by the setup wizard, as settings fields, or ``None`` when there is none
    (or it is unusable).

    It never holds a master key: a Container App keeps its own as a secret, and an external
    LiteLLM gateway's is in the local secret store (secret_store.py)."""
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
    product = saved_product(target)
    if product is None:
        return None
    values = {name: str(target.get(name) or "") for name in SAVED_FIELDS[product]}
    if product == "litellm" and values["gateway_url"]:
        return values if values["gateway_name"] else None
    required = APIM_FIELDS if product == "apim" else LITELLM_FIELDS
    return values if all(values[name] for name in required) else None


def save_target(product: Product, target: Mapping[str, str], path: Path | None = None) -> None:
    fields = {name: str(target.get(name) or "") for name in SAVED_FIELDS[product]}
    (path or STATE_FILE).write_text(
        json.dumps({"target": {"product": product, **fields}}, indent=2) + "\n", encoding="utf-8"
    )


def clear_saved_target(path: Path | None = None) -> None:
    """Forgets the chosen gateway, e.g. after it was deleted; setup then asks again."""
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
