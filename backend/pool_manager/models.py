"""Request and response models for AI gateway pools on LiteLLM.

A pool is a LiteLLM model group: one row in the gateway's model table per member, all with the
pool's ID as their ``model_name``. Members are identified by IDs this app gives them; a member
in two pools is two rows.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)

# Pool and member IDs: letters, digits and hyphens, starting with a letter or digit. Pool IDs
# are also the model name clients send, so they stay URL- and YAML-safe.
RESOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{0,79}$"
_RESOURCE_ID = re.compile(RESOURCE_ID_PATTERN)
# Model names clients may send for a pool (aliases): what OpenAI and Azure deployment names use.
MODEL_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CircuitBreaker(StrictModel):
    """LiteLLM's cooldown, applied to each member of a pool: a member that fails more than
    ``allowed_fails`` times within a minute sits out ``cooldown_seconds``, and its share goes to
    the other members (or, in failover, to the next priority)."""

    allowed_fails: int = Field(default=0, ge=0, le=1000)
    cooldown_seconds: int = Field(default=60, ge=1, le=86_400)


MemberAuthType = Literal["none", "api_key", "managed_identity"]


class MemberAuth(StrictModel):
    # ``managed_identity``: the gateway calls the member as its own identity (Foundry members;
    # it needs Cognitive Services OpenAI User there). ``api_key``: LiteLLM stores the key,
    # encrypted with the gateway's salt key, and sends it as Azure's ``api-key`` header or an
    # OpenAI-style bearer token.
    type: MemberAuthType = "none"
    secret: SecretStr | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def require_secret(self) -> MemberAuth:
        if self.type in {"none", "managed_identity"} and self.secret is not None:
            raise ValueError(f"{self.type} authentication does not take a secret")
        return self


class MemberBackendWrite(StrictModel):
    """A member a pool can use: an Azure OpenAI deployment URL (``.../openai/deployments/<name>``)
    or any OpenAI-compatible base URL, which then needs the ``model`` to ask it for."""

    title: str | None = Field(default=None, max_length=300)
    url: HttpUrl
    # OpenAI-compatible members only: the model name the endpoint serves.
    model: str | None = Field(default=None, min_length=1, max_length=200)
    # On update, an api_key member without a secret keeps the key the gateway already has.
    auth: MemberAuth = Field(default_factory=MemberAuth)


class BackendView(StrictModel):
    """A member, as its pools' rows describe it (or added but not in a pool yet)."""

    id: str
    title: str | None
    # The Azure OpenAI deployment URL, or the OpenAI-compatible base URL.
    url: str
    provider: Literal["azure", "openai"]
    # The model an OpenAI-compatible member is asked for; the deployment for Azure members.
    model: str
    auth_type: MemberAuthType | Literal["custom"]
    # Pools it is a member of; empty until a pool using it is saved.
    pools: list[str]
    managed: bool
    protected: bool


class PoolMemberWrite(StrictModel):
    backend_id: str = Field(pattern=RESOURCE_ID_PATTERN)
    # 0-based here; LiteLLM's ``order`` is ``priority + 1``.
    priority: int = Field(ge=0, le=99)
    weight: int = Field(ge=0, le=100)


DEFAULT_RETRIES = 1
DEFAULT_TIMEOUT_SECONDS = 120


class PoolWrite(StrictModel):
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=1500)
    members: list[PoolMemberWrite] = Field(min_length=2, max_length=30)
    # Written to every member row. ``None`` keeps what the pool has (the defaults when new).
    circuit_breaker: CircuitBreaker | None = None
    # Further attempts LiteLLM makes for a failed request, on the same or another member.
    retries: int | None = Field(default=None, ge=0, le=10)
    # How long one attempt may take before LiteLLM gives up on that member.
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    # ``None`` creates a new pool; otherwise it must match the pool as loaded.
    etag: str | None = None

    @model_validator(mode="after")
    def validate_members(self) -> PoolWrite:
        backend_ids = [member.backend_id.casefold() for member in self.members]
        if len(backend_ids) != len(set(backend_ids)):
            raise ValueError("a pool cannot repeat a member")
        priority_weights: dict[int, int] = {}
        for member in self.members:
            priority_weights[member.priority] = (
                priority_weights.get(member.priority, 0) + member.weight
            )
        if any(total == 0 for total in priority_weights.values()):
            raise ValueError("every member priority group needs a positive weight")
        return self


class PoolMemberView(StrictModel):
    backend_id: str
    priority: int
    weight: int


class PoolView(StrictModel):
    id: str
    title: str | None
    description: str | None
    members: list[PoolMemberView]
    circuit_breaker: CircuitBreaker | None
    retries: int | None
    timeout_seconds: int | None
    # Other names clients may send for this pool (LiteLLM model group aliases).
    aliases: list[str]
    managed: bool
    protected: bool
    # A hash of the pool's rows: LiteLLM has no ETags, so a save compares this instead.
    etag: str | None


class ModelCheckRequest(StrictModel):
    members: list[str] = Field(min_length=1, max_length=30)


class MemberModel(StrictModel):
    backend_id: str
    # ARM ID of the Foundry deployment the member points at; ``None`` when it points elsewhere.
    deployment_id: str | None
    # Read from the deployment resource, never inferred from a deployment or backend name.
    model_name: str | None
    model_version: str | None
    # Why a Foundry deployment's model could not be read. Such a member fails the check.
    problem: str | None


class ModelCheck(StrictModel):
    members: list[MemberModel]
    # Distinct model names, in member order.
    model_names: list[str]
    ok: bool
    message: str | None


class AliasWrite(StrictModel):
    """Another model name that reaches the pool, e.g. the deployment name clients already send."""

    name: str = Field(pattern=MODEL_NAME_PATTERN)


class AliasView(StrictModel):
    name: str
    pool_id: str
    # Added by this app for the pool; others were set on the gateway some other way.
    managed: bool


class PoolEndpoint(StrictModel):
    """How clients call a pool: the gateway's OpenAI-compatible base URL and a model name."""

    pool_id: str
    # ``<gateway>/v1``; None while the gateway has no URL yet.
    base_url: str | None
    # The pool ID first, then its aliases.
    model_names: list[str]
    aliases: list[AliasView]


class ConfigView(StrictModel):
    """The pool as LiteLLM ``config.yaml`` would describe it; member secrets are left out."""

    pool_id: str
    yaml: str | None


class ConfigPreview(StrictModel):
    """What saving the pool would write, computed without changing the gateway."""

    current: str | None
    proposed: str


class RemovalAlias(StrictModel):
    name: str
    pool_id: str


class RemovalKey(StrictModel):
    # LiteLLM's ID of the key (a hash; the key itself can't be read back).
    id: str
    alias: str | None
    pool_id: str | None


class RemovalPlan(StrictModel):
    """What removing pools takes with it, shown before the user confirms. Nothing is written."""

    pools: list[str]
    # Aliases this app added for those pools.
    delete_aliases: list[RemovalAlias]
    # Other aliases that point at them; they are removed too, so none is left dangling.
    unbind_aliases: list[RemovalAlias]
    # Test keys this app created for those pools.
    delete_keys: list[RemovalKey]
    # Cleanup only: pools this app didn't create, which it leaves alone.
    kept_pools: list[str]
    # Why the plan can't run; removal refuses while this is not empty.
    blocked: list[str]


class RemovalConfirm(StrictModel):
    """The plan the user reviewed. Removal recomputes it and refuses to delete anything that is
    not listed here, e.g. an alias added to the pool in the meantime."""

    pools: list[str] = Field(max_length=500)
    delete_aliases: list[str] = Field(max_length=500)
    unbind_aliases: list[str] = Field(max_length=500)
    delete_keys: list[str] = Field(max_length=500)
    # Single-pool removal: the pool's ETag as loaded.
    etag: str | None = None


class CleanupConfirm(RemovalConfirm):
    # The gateway name typed again; checked here too, not only in the UI.
    confirm_name: str = Field(min_length=1, max_length=64)


class RemovalResult(StrictModel):
    pools: list[str]
    deleted_aliases: list[str]
    unbound_aliases: list[str]
    deleted_keys: list[str]


GatewayKind = Literal["aca", "external"]


class Capabilities(StrictModel):
    gateway_name: str
    # ``aca``: a Container App (created here or not); ``external``: any other LiteLLM proxy
    # the user connected by URL, e.g. on AKS.
    gateway_kind: GatewayKind
    resource_group: str | None
    id_prefix: str
    protected_prefixes: list[str]
    # ARM ID of the Container App, for links to the Azure portal; None for external gateways.
    gateway_resource_id: str | None
    # Identity the gateway calls managed-identity members as; it needs a data-plane role there.
    gateway_principal_id: str | None
    # Base URL of the gateway, e.g. https://<app>.<env>.<region>.azurecontainerapps.io.
    gateway_url: str | None
    litellm_version: str | None
    # Application Insights component that receives the gateway's OpenTelemetry spans.
    app_insights_id: str | None
    discovery_subscriptions: list[str]


CostItemKind = Literal[
    "container_apps", "environment", "postgres", "redis", "aks_nodes", "aks_tier"
]


class CostItem(StrictModel):
    kind: CostItemKind
    # What is priced, e.g. "1.25 vCPU, 2.5 GiB x 1 replica" or "B1MS + 32 GB".
    detail: str
    # Per month (730 hours): Container Apps bill less while replicas are idle, so a range.
    monthly_low: float
    monthly_high: float


class ListPrice(StrictModel):
    """The gateway's retail price estimate (Azure Retail Prices API), before discounts, free
    grants and tax, and without log ingestion."""

    items: list[CostItem]
    monthly_low: float
    monthly_high: float
    currency: str


class MonthToDate(StrictModel):
    """What the gateway cost this month so far, from Cost Management (it lags a few hours)."""

    amount: float
    # None when Azure has recorded no charge yet.
    currency: str | None
    # First day of the month, ISO date.
    since: str


# Why this month's spend is missing: no delegated Azure token (deployed), no access to the
# resource group's costs, a subscription type without cost data, Cost Management failed, or an
# external gateway this app can't see the bill of.
CostNote = Literal["no_token", "forbidden", "unsupported", "unavailable", "external"]


class ServiceCost(StrictModel):
    profile: str | None
    location: str | None
    # None when the Retail Prices API is unreachable or the gateway runs elsewhere.
    list_price: ListPrice | None
    month_to_date: MonthToDate | None
    month_to_date_note: CostNote | None
    # Azure's own message when month_to_date failed.
    detail: str | None
    # Cost analysis for the gateway's resource group in the Azure portal.
    portal_url: str | None


class Principal(StrictModel):
    name: str
    can_write: bool
    authenticated: bool
    # The public app: the Entra tenant the user signed in to.
    tenant_id: str | None = None
    # The public app's read-only guest view: nothing can be changed or revealed.
    guest: bool = False


class PublicConfig(StrictModel):
    """What the browser needs before signing in; served without authentication."""

    mode: Literal["local", "deployed", "public"]
    # Only in the public app.
    client_id: str | None = None
    authority: str | None = None
    # The scope the browser's token is for (this app's API); the backend exchanges it for ARM.
    api_scope: str | None = None
    # Asked for at sign-in so one consent covers the Azure access the app needs.
    consent_scopes: list[str] = []
    # Visitors may browse a demo service read-only without signing in.
    guest: bool = False


class DeploymentView(StrictModel):
    """An Azure OpenAI / Foundry deployment that can become a pool member."""

    account_id: str
    account_name: str
    resource_group: str
    location: str
    deployment: str
    model_name: str
    model_version: str
    sku: str
    capacity: int | None
    backend_url: str
    local_auth_disabled: bool
    suggested_backend_id: str
    # Member already pointing at this deployment, if any.
    backend_id: str | None


class DeploymentRef(StrictModel):
    account_id: str = Field(max_length=500, pattern=r"^/subscriptions/[^?#]+$")
    deployment: str = Field(min_length=1, max_length=128)


class DeploymentImport(StrictModel):
    deployments: list[DeploymentRef] = Field(min_length=1, max_length=30)


class DeploymentImportResult(StrictModel):
    # Member IDs in request order, whether newly added or already present.
    backend_ids: list[str]
    created: list[str]


# -- creating Foundry deployments (model_deploy.py) -------------------------------------

# Pay-per-token deployment types only; provisioned (PTU) is a costly commitment, not offered.
DeploymentSku = Literal["GlobalStandard", "DataZoneStandard", "Standard"]
VersionUpgrade = Literal[
    "OnceNewDefaultVersionAvailable", "OnceCurrentVersionExpired", "NoAutoUpgrade"
]
# Conservative: Azure documents no pattern for deployment names; this is what the portal accepts.
DEPLOYMENT_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$"


class FoundryAccountView(StrictModel):
    """A Foundry / Azure OpenAI account in the discovery subscriptions."""

    id: str
    name: str
    kind: str
    location: str
    resource_group: str
    subscription_id: str


class ModelSkuOption(StrictModel):
    name: DeploymentSku
    # Thousands of tokens per minute.
    default_capacity: int
    min_capacity: int
    max_capacity: int
    step: int


class DeployableModel(StrictModel):
    """An OpenAI model the account can deploy, as its ``models`` list reports it."""

    name: str
    version: str
    is_default_version: bool
    lifecycle_status: str | None
    # When inference stops, if Azure has announced it (ISO 8601).
    retires_on: str | None
    capabilities: list[str]
    skus: list[ModelSkuOption]


class ModelCapacity(StrictModel):
    """Capacity left for one model version in the account's region, per deployment type."""

    available: dict[str, int]
    # Why it couldn't be read (often missing permission); the form still works without it.
    error: str | None = None


class FoundryDeploymentCreate(StrictModel):
    account_id: str = Field(max_length=500, pattern=r"^/subscriptions/[^?#]+$")
    deployment: str = Field(pattern=DEPLOYMENT_NAME_PATTERN)
    model_name: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=64)
    sku: DeploymentSku
    capacity: int = Field(ge=1, le=1_000_000)
    version_upgrade: VersionUpgrade = "OnceNewDefaultVersionAvailable"


class FoundryDeploymentStatus(StrictModel):
    account_id: str
    deployment: str
    # Azure's provisioningState: Accepted, Creating, Succeeded, Failed, ...
    state: str
    error: str | None = None


def is_valid_resource_id(value: str) -> bool:
    return bool(_RESOURCE_ID.fullmatch(value))


# -- setup wizard ----------------------------------------------------------------------

SetupStatus = Literal["ok", "action", "blocked", "pending", "unknown", "skipped"]
# Fixes the app can perform itself. Everything else becomes an admin request.
SetupFix = Literal[
    "sign_in",
    "consent",
    "refresh_token",
    "select_gateway",
    # An external gateway whose master key isn't known here (yet).
    "connect_gateway",
    "grant_foundry_access",
]
SetupKind = Literal[
    "sign_in",
    "azure_token",
    "gateway_target",
    "gateway_access",
    "gateway_health",
    "discovery",
    "app_gateway",
    "app_discovery",
    "gateway_identity",
    "foundry_access",
]


class AdminRequest(StrictModel):
    """What to ask an administrator for, ready to copy."""

    assignee: str
    assignee_id: str | None
    role: str | None
    scope: str | None
    command: str
    portal_url: str | None


class SetupCheck(StrictModel):
    id: str
    kind: SetupKind
    # Subscription or Foundry account ID the check is about, if any.
    subject: str | None = None
    status: SetupStatus
    missing: list[str] = Field(default_factory=list)
    detail: str | None = None
    fix: SetupFix | None = None
    request: AdminRequest | None = None
    # Why the signed-in user may change pools in the deployed app.
    granted_by: Literal["owners", "azure_rbac"] | None = None
    # When a pending operation began (ISO 8601, UTC), so the UI can show how long it has run.
    started_at: str | None = None


class SetupIdentity(StrictModel):
    name: str | None
    object_id: str | None
    tenant_id: str | None


SignInSource = Literal["browser", "device_code", "azure_cli", "azd", "powershell"]


class LocalSignIn(StrictModel):
    """How the local app is signed in to Azure (see credentials.py)."""

    # What answered the last token request; None when nothing is signed in.
    source: SignInSource | None
    # A sign-in made from the app that survives a restart.
    remembered: bool
    # The OS can encrypt a token cache here; without it app sign-ins last until the app stops.
    persistent: bool
    # Sign-in tools on PATH.
    tools: list[SignInSource]


class SetupReport(StrictModel):
    mode: Literal["local", "deployed", "public", "guest"]
    user: SetupIdentity | None
    checks: list[SetupCheck]
    # Nothing needs action; blocked or pending checks are not ready either.
    ready: bool
    # The app can reach the gateway's admin API, so the rest of the UI works.
    can_operate: bool
    local_sign_in: LocalSignIn | None = None


# What a setup report is doing, in the order it does it; each step calls ARM.
SetupStepKind = Literal[
    "sign_in",
    "gateway_target",
    "gateway_access",
    "app_gateway",
    "gateway_health",
    "discovery",
    "gateway_identity",
    "members",
    "foundry_access",
]


class SetupProgressStep(StrictModel):
    kind: SetupStepKind
    state: Literal["running", "done"]
    subject: str | None = None
    # Steps that check several subscriptions or accounts count them.
    done: int | None = None
    total: int | None = None


class SetupProgress(StrictModel):
    """How far the caller's setup report has got, so the UI can say what it waits for."""

    running: bool
    steps: list[SetupProgressStep]


class DeviceCodePrompt(StrictModel):
    """Enter ``user_code`` at ``verification_uri`` on any device to finish signing in."""

    verification_uri: str
    user_code: str
    expires_on: float


class DeviceCodeStatus(StrictModel):
    state: Literal["idle", "pending", "done", "failed"]
    error: str | None


class FoundryAccessRequest(StrictModel):
    account_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("account_ids")
    @classmethod
    def validate_account_ids(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _ACCOUNT_ID.fullmatch(value):
                raise ValueError(f"not a Cognitive Services account ID: {value}")
        return values


_ACCOUNT_ID = re.compile(
    r"/subscriptions/[^/]+/resourceGroups/[^/]+"
    r"/providers/Microsoft\.CognitiveServices/accounts/[^/]+",
    re.IGNORECASE,
)


# -- choosing, deploying or connecting the gateway -----------------------------------------

_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
# Resource group names: letters, digits, underscores, hyphens, periods and parentheses; no
# trailing period.
_RESOURCE_GROUP = r"^[-\w.()]{0,89}[-\w()]$"
# Container App names: lower case letters, digits and hyphens, a letter first, no trailing or
# double hyphen; the template derives resource names (psql-<name>-..., id-<name>) from it.
GATEWAY_NAME_PATTERN = r"^[a-z](?:[a-z0-9]|-[a-z0-9]){1,30}$"
# ``dev``: one replica and no Redis; ``production``: 2-5 replicas sharing cooldowns in Redis.
GatewayProfile = Literal["dev", "production"]
# Where a gateway the app deploys runs: Azure Container Apps, or a new AKS cluster.
GatewayPlatform = Literal["aca", "aks"]
# ``browser``: the public app, where each user's browser remembers their own choice.
# ``guest``: the public app's read-only guest view, which shows one configured gateway.
TargetSource = Literal["environment", "saved", "browser", "guest", "none"]


class TargetView(StrictModel):
    """The gateway the app manages and where that choice comes from."""

    gateway_name: str | None
    gateway_kind: GatewayKind | None
    subscription_id: str | None
    resource_group: str | None
    # External gateways only; a Container App's URL is read from Azure.
    gateway_url: str | None
    tenant_id: str | None
    source: TargetSource
    # Locally without a fixed gateway in the environment; never in the Easy Auth deployment.
    can_change: bool
    # An AKS gateway the app deployed: its cluster, and whether the app is still installing it.
    cluster_id: str | None = None
    provisioning: bool = False


class TenantView(StrictModel):
    tenant_id: str
    display_name: str | None
    default_domain: str | None


class SubscriptionView(StrictModel):
    subscription_id: str
    display_name: str
    tenant_id: str | None
    state: str


class GatewayView(StrictModel):
    """A LiteLLM gateway on Container Apps in a subscription, or one being deployed."""

    id: str
    name: str
    resource_group: str
    location: str
    url: str | None
    profile: GatewayProfile | None
    provisioning_state: str | None
    # Deployed by this app (the managed-by tag).
    managed: bool
    # Whether the signed-in user may read the app and its master key; None if unknown.
    can_manage: bool | None
    missing: list[str] = Field(default_factory=list)
    # aks: an AKS cluster the app deployed LiteLLM on (id is the cluster's).
    platform: GatewayPlatform = "aca"


class ResourceGroupView(StrictModel):
    name: str
    location: str


class GatewayCreateOptions(StrictModel):
    resource_groups: list[ResourceGroupView]
    # Regions where this subscription can create Container Apps environments.
    locations: list[str]
    # Resource providers the template needs that are not registered yet; creating registers them.
    unregistered_providers: list[str]


class TargetSelect(StrictModel):
    tenant_id: str | None = Field(default=None, pattern=_GUID)
    subscription_id: str = Field(pattern=_GUID)
    resource_group: str = Field(pattern=_RESOURCE_GROUP)
    gateway_name: str = Field(min_length=2, max_length=32, pattern=r"^[a-z0-9-]+$")


class GatewayCreate(StrictModel):
    tenant_id: str | None = Field(default=None, pattern=_GUID)
    subscription_id: str = Field(pattern=_GUID)
    # Created in ``location`` when it does not exist yet.
    resource_group: str = Field(pattern=_RESOURCE_GROUP)
    location: str = Field(pattern=r"^[a-z0-9]{2,40}$")
    name: str = Field(max_length=32, pattern=GATEWAY_NAME_PATTERN)
    profile: GatewayProfile = "dev"
    platform: GatewayPlatform = "aca"


class GatewayConnect(StrictModel):
    """A LiteLLM proxy this app didn't deploy (e.g. on AKS), reached by URL and master key."""

    tenant_id: str | None = Field(default=None, pattern=_GUID)
    # Searched for Foundry deployments; the gateway itself needs no subscription.
    subscription_id: str = Field(pattern=_GUID)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    url: HttpUrl
    master_key: SecretStr = Field(min_length=8, max_length=512)
    # The managed identity's object ID the gateway calls Foundry as, for the access check.
    principal_id: str | None = Field(default=None, pattern=_GUID)
    # Application Insights component its OpenTelemetry spans go to, for the Monitor tab.
    app_insights_id: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.Insights/components/[^/]+$",
    )


class DeployStep(StrictModel):
    resource_type: str
    name: str
    # ARM's provisioningState of the deployment operation: Running, Succeeded, Failed, ...
    state: str
    error: str | None = None


class GatewayDeployStatus(StrictModel):
    """How far deploying a gateway has got: its ARM deployment, and on AKS the install in the
    cluster and the HTTPS certificate after it."""

    name: str
    state: Literal["none", "running", "succeeded", "failed"]
    started_at: str | None
    steps: list[DeployStep]
    error: str | None
    platform: GatewayPlatform = "aca"
    # azure: the ARM deployment. cluster: installing LiteLLM in the cluster (AKS Run Command).
    # certificate: waiting for its HTTPS address to answer. done: connected.
    phase: Literal["azure", "cluster", "certificate", "done"] = "azure"
    # The end of the install's output, when it has finished or failed.
    log_tail: str | None = None


class AksBundleFile(StrictModel):
    path: str
    content: str


class AksBundle(StrictModel):
    """Files that deploy the gateway on an existing AKS cluster with LiteLLM's Helm chart."""

    name: str
    namespace: str
    files: list[AksBundleFile]


class SignInRequest(StrictModel):
    """Local browser sign-in, optionally to one tenant (e.g. to list its subscriptions)."""

    tenant_id: str | None = Field(default=None, pattern=_GUID)


# -- trying a pool -----------------------------------------------------------------------------

# Path below the gateway's /v1, e.g. chat/completions. Plain segments only.
_TEST_PATH = re.compile(r"[A-Za-z0-9_~-][A-Za-z0-9._~-]*(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*")
MAX_TEST_BODY = 64 * 1024
# ``pool``: the key may call the pool (or one of its aliases); ``all``: every model.
KeyScope = Literal["pool", "all"]


class VirtualKeyView(StrictModel):
    """A LiteLLM virtual key that can call the pool. LiteLLM keeps only a hash of the key, so
    this is all that can be shown of it afterwards."""

    # LiteLLM's ID of the key (``token``, a hash).
    id: str
    alias: str | None
    # How LiteLLM abbreviates the key, e.g. ``sk-...Ab12``.
    key_name: str | None
    scope: KeyScope
    models: list[str]
    blocked: bool
    expires: str | None
    spend: float | None
    # The test key this app creates for the pool.
    managed: bool


class VirtualKeySecret(StrictModel):
    """A key just created; the only time it can be shown."""

    key: str
    view: VirtualKeyView


class ApiTestRequest(StrictModel):
    path: str = Field(default="chat/completions", max_length=200)
    key: SecretStr | None = Field(default=None, min_length=1, max_length=256)
    body: dict[str, Any]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        value = value.strip("/")
        if not _TEST_PATH.fullmatch(value):
            raise ValueError("letters, digits and - . _ ~ separated by /")
        return value

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > MAX_TEST_BODY:
            raise ValueError(f"at most {MAX_TEST_BODY // 1024} KB")
        return value


class ApiTestResult(StrictModel):
    status: int
    elapsed_ms: int
    # The URL called, without the key.
    url: str
    # Response headers that explain the result: which member answered, retries and fallbacks,
    # rate limits, Retry-After.
    headers: dict[str, str]
    body: str
    # The body was longer than the app returns.
    truncated: bool


# -- pool dashboard --------------------------------------------------------------------------

MetricsRange = Literal["1h", "6h", "24h", "7d"]


class TrafficPoint(StrictModel):
    time: str
    success: int
    throttled: int
    client_errors: int
    server_errors: int


class LatencyPoint(StrictModel):
    time: str
    overall_ms: float | None
    backend_ms: float | None


class GatewayMetrics(StrictModel):
    """What clients of the pool saw, from the gateway's OpenTelemetry spans in Application
    Insights: one span per attempt, grouped into requests by their trace."""

    requests: int
    success: int
    # 429 answered to clients: every member was throttled or cooling down.
    throttled: int
    client_errors: int
    server_errors: int
    # Requests that succeeded only because LiteLLM retried on another member.
    failovers: int
    # Share of requests not answered with a 5xx; None without traffic.
    availability: float | None
    # Average time from the first attempt's start to the last one's end.
    duration_ms: float | None
    # Average of the attempts that succeeded: the members' own time.
    backend_duration_ms: float | None
    traffic: list[TrafficPoint]
    latency: list[LatencyPoint]


class MemberMetrics(StrictModel):
    """A member: the gateway's attempts on it (spans), and its Foundry deployment's own metrics,
    which count every caller of the deployment, not only this gateway."""

    backend_id: str
    # None when the member isn't a Foundry deployment, so it has no deployment metrics.
    deployment_id: str | None
    region: str | None
    requests: int | None
    throttled: int | None
    server_errors: int | None
    tokens: int | None
    # Time to last byte, average.
    latency_ms: float | None
    # Provisioned (PTU) deployments only; peak in the period.
    utilization_pct: float | None
    # The share the pool's priority and weight give this member, 0 for standby members.
    expected_share: float
    error: str | None
    # From the gateway's spans; None without Application Insights.
    gateway_attempts: int | None = None
    gateway_failures: int | None = None
    gateway_throttled: int | None = None


class PoolMetrics(StrictModel):
    pool_id: str
    range: MetricsRange
    interval: str
    start: str
    end: str
    # The names the pool is called by (its ID and aliases).
    model_names: list[str]
    gateway: GatewayMetrics | None
    # Why gateway metrics are missing: "no_app_insights", or Azure's message.
    gateway_error: str | None
    members: list[MemberMetrics]


# -- deleting a gateway (local) -------------------------------------------------------------


class GatewayDeleteCheck(StrictModel):
    """What deleting a gateway would remove, shown before the user confirms."""

    name: str
    resource_group: str
    location: str
    # The gateway the app manages now; deleting it sends setup back to choosing one.
    current: bool
    # False when its deployment failed before the Container App existed: only what that
    # deployment created is left to delete.
    deployed: bool
    pools: int | None
    # Resources tagged as this gateway's, which are deleted with it (never the resource group).
    resources: list[str]


class GatewayDelete(TargetSelect):
    # The gateway name typed again; checked here too, not only in the UI.
    confirm_name: str = Field(min_length=1, max_length=64)
