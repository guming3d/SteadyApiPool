"""Request and response models for APIM backend pools.

Members reference APIM backend IDs directly; the app has no model registry of its own.
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

# APIM resource names: letters, digits and hyphens, starting with a letter or digit.
RESOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{0,79}$"
_RESOURCE_ID = re.compile(RESOURCE_ID_PATTERN)

ErrorReason = Literal["BackendConnectionFailure", "Timeout", "ClientConnectionFailure"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FailureStatusCodeRange(StrictModel):
    minimum: int = Field(ge=200, le=599)
    maximum: int = Field(ge=200, le=599)

    @model_validator(mode="after")
    def validate_order(self) -> FailureStatusCodeRange:
        if self.minimum > self.maximum:
            raise ValueError("failure status code minimum cannot exceed maximum")
        return self


class CircuitBreaker(StrictModel):
    """One APIM circuit-breaker rule. The defaults are the app's platform breaker."""

    failure_count: int = Field(default=1, ge=1, le=10_000)
    interval_seconds: int = Field(default=60, ge=1, le=86_400)
    trip_duration_seconds: int = Field(default=60, ge=1, le=86_400)
    accept_retry_after: bool = True
    status_code_ranges: list[FailureStatusCodeRange] = Field(
        default_factory=lambda: [
            FailureStatusCodeRange(minimum=429, maximum=429),
            FailureStatusCodeRange(minimum=408, maximum=408),
            FailureStatusCodeRange(minimum=500, maximum=599),
        ],
        max_length=10,
    )
    error_reasons: list[ErrorReason] = Field(
        default_factory=lambda: list[ErrorReason](["BackendConnectionFailure", "Timeout"]),
        max_length=3,
    )

    @model_validator(mode="after")
    def require_failure_signal(self) -> CircuitBreaker:
        if not self.status_code_ranges and not self.error_reasons:
            raise ValueError("a circuit breaker needs at least one status range or error reason")
        return self


class SecretSource(StrictModel):
    """Exactly one of a literal secret (stored as an APIM secret named value) or a Key Vault
    secret identifier (resolved by the APIM managed identity)."""

    value: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    key_vault_secret_id: HttpUrl | None = None
    key_vault_identity_client_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def require_one(self) -> SecretSource:
        if (self.value is None) == (self.key_vault_secret_id is None):
            raise ValueError("provide exactly one of value or key_vault_secret_id")
        return self


class MemberAuth(StrictModel):
    # ``managed_identity`` adds nothing to the backend; the API binding applies
    # ``authentication-managed-identity`` because APIM backends cannot carry it.
    type: Literal["none", "api_key", "bearer", "managed_identity"] = "none"
    header_name: str = Field(default="api-key", pattern=r"^[A-Za-z0-9-]{1,64}$")
    secret: SecretSource | None = None

    @model_validator(mode="after")
    def require_secret(self) -> MemberAuth:
        if self.type in {"api_key", "bearer"} and self.secret is None:
            raise ValueError(f"{self.type} authentication requires a secret")
        if self.type in {"none", "managed_identity"} and self.secret is not None:
            raise ValueError(f"{self.type} authentication does not take a secret")
        return self


class MemberBackendWrite(StrictModel):
    """Create or update a ``Single`` APIM backend that can join a pool."""

    title: str | None = Field(default=None, max_length=300)
    url: HttpUrl
    # On update, omitting the secret keeps the existing named value.
    auth: MemberAuth = Field(default_factory=MemberAuth)
    circuit_breaker: CircuitBreaker | None = Field(default_factory=CircuitBreaker)
    etag: str | None = None


class BackendView(StrictModel):
    id: str
    title: str | None
    description: str | None
    type: Literal["Single", "Pool"]
    url: str | None
    auth_type: Literal["none", "api_key", "bearer", "managed_identity", "custom"]
    header_name: str | None
    named_value: str | None
    circuit_breaker: CircuitBreaker | None
    has_circuit_breaker: bool
    managed: bool
    protected: bool
    etag: str | None


class PoolMemberWrite(StrictModel):
    backend_id: str = Field(pattern=RESOURCE_ID_PATTERN)
    # 0-based here; APIM stores ``priority + 1``.
    priority: int = Field(ge=0, le=99)
    weight: int = Field(ge=0, le=100)


class PoolWrite(StrictModel):
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=1500)
    members: list[PoolMemberWrite] = Field(min_length=2, max_length=30)
    session_affinity: bool = False
    # The pool's circuit breaker: saving writes it to every member this app manages (APIM
    # keeps breakers on member backends). ``None`` leaves the members' rules as they are.
    circuit_breaker: CircuitBreaker | None = None
    # ``None`` creates a new pool; otherwise the update must match the current ETag.
    etag: str | None = None

    @model_validator(mode="after")
    def validate_members(self) -> PoolWrite:
        backend_ids = [member.backend_id.casefold() for member in self.members]
        if len(backend_ids) != len(set(backend_ids)):
            raise ValueError("a backend pool cannot repeat a backend")
        priority_weights: dict[int, int] = {}
        for member in self.members:
            priority_weights[member.priority] = (
                priority_weights.get(member.priority, 0) + member.weight
            )
        if any(total == 0 for total in priority_weights.values()):
            raise ValueError("every backend priority group needs a positive weight")
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
    session_affinity: bool
    session_cookie_name: str | None
    managed: bool
    protected: bool
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


class ApiBindingWrite(StrictModel):
    pool_id: str = Field(pattern=RESOURCE_ID_PATTERN)
    # The app's default retry on 429: two attempts, one second apart, the first at once.
    retry_on_429: bool = True
    max_attempts_per_request: int = Field(default=2, ge=2, le=10)
    retry_interval_seconds: int = Field(default=1, ge=0, le=30)
    first_fast_retry: bool = True
    backend_timeout_seconds: int = Field(default=120, ge=1, le=240)
    managed_identity_resource: str | None = Field(
        default=None, max_length=300, pattern=r"^[A-Za-z0-9:/._-]+$"
    )
    etag: str | None = None


# An APIM API path: no leading slash, segments of URL-safe characters.
API_PATH_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._~-]*(?:/[A-Za-z0-9._~-]+)*$"


class PoolApiCreate(StrictModel):
    """Creates an HTTP API for a pool and binds it, so clients get an endpoint right away."""

    # Defaults to the pool ID.
    api_id: str | None = Field(default=None, pattern=RESOURCE_ID_PATTERN)
    path: str = Field(max_length=400, pattern=API_PATH_PATTERN)
    # Defaults to the pool title.
    display_name: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("path", mode="before")
    @classmethod
    def strip_slashes(cls, value: object) -> object:
        # "/gpt-4o/" and "gpt-4o" are the same API path.
        return value.strip().strip("/") if isinstance(value, str) else value


class ApiBinding(StrictModel):
    pool_id: str
    retry_on_429: bool
    # Read back from the managed retry block; ``None`` when 429 retry is off.
    max_attempts_per_request: int | None
    retry_interval_seconds: int | None
    first_fast_retry: bool | None
    backend_timeout_seconds: int | None
    managed_identity_resource: str | None


class ApiView(StrictModel):
    id: str
    display_name: str
    path: str
    binding: ApiBinding | None
    # A ``set-backend-service`` outside this app's managed block would override the pool.
    foreign_backend_selection: bool
    # Created by this app (description starts with the managed marker).
    managed: bool
    # How clients authenticate to the API: APIM subscription key and its header name.
    subscription_required: bool
    subscription_key_header: str
    etag: str | None


class RemovalApi(StrictModel):
    id: str
    display_name: str
    path: str
    # The pool it is bound to; None for an API this app created that is no longer bound.
    pool_id: str | None


class RemovalPlan(StrictModel):
    """What removing pools takes with it, shown before the user confirms. Nothing is written."""

    pools: list[str]
    # APIs this app created for those pools, deleted along with their policy.
    delete_apis: list[RemovalApi]
    # Other APIs bound to them; unbinding restores their original policy byte for byte.
    unbind_apis: list[RemovalApi]
    # Members this app created that no remaining pool uses, deleted with their credential.
    delete_members: list[str]
    # Cleanup only: pools this app didn't create, which it leaves alone.
    kept_pools: list[str]
    # Why the plan can't run; removal refuses while this is not empty.
    blocked: list[str]


class RemovalConfirm(StrictModel):
    """The plan the user reviewed. Removal recomputes it and refuses to delete or unbind
    anything that is not listed here, e.g. an API bound to the pool in the meantime."""

    pools: list[str] = Field(max_length=500)
    delete_apis: list[str] = Field(max_length=500)
    unbind_apis: list[str] = Field(max_length=500)
    # Single-pool removal: the pool's ETag as loaded, for a conditional delete.
    etag: str | None = None


class CleanupConfirm(RemovalConfirm):
    # The APIM service name typed again; checked here too, not only in the UI.
    confirm_name: str = Field(min_length=1, max_length=50)


class RemovalResult(StrictModel):
    pools: list[str]
    deleted_apis: list[str]
    unbound_apis: list[str]
    deleted_members: list[str]


class PolicyView(StrictModel):
    api_id: str
    # ``None`` when the API has no API-level policy and only inherits ``<base />``.
    xml: str | None
    etag: str | None


class PolicyPreview(StrictModel):
    """What binding would write, computed without changing the API policy."""

    current: str | None
    proposed: str


class Capabilities(StrictModel):
    apim_name: str
    resource_group: str
    session_affinity_supported: bool
    id_prefix: str
    protected_prefixes: list[str]
    # Managed identity that calls managed-identity members; it needs a data-plane role on them.
    # ARM ID of the APIM service, for links to the Azure portal.
    apim_resource_id: str
    apim_principal_id: str | None
    # Base URL clients call, e.g. https://<apim>.azure-api.net.
    gateway_url: str | None
    discovery_subscriptions: list[str]


class ListPrice(StrictModel):
    """The service tier's retail price (Azure Retail Prices API), before discounts and tax."""

    # As Azure prices it, e.g. "Basic v2".
    tier: str
    units: int
    # Per unit; None for tiers billed only per call (Consumption).
    hourly: float | None
    # All units over an average month of 730 hours.
    monthly: float | None
    # Requests beyond what the tier includes; None when the tier has no such charge.
    per_10k_calls: float | None
    currency: str


class MonthToDate(StrictModel):
    """What the service cost this month so far, from Cost Management (it lags a few hours)."""

    amount: float
    # None when Azure has recorded no charge yet.
    currency: str | None
    # First day of the month, ISO date.
    since: str


# Why this month's spend is missing: no delegated Azure token (deployed), no access to the
# resource group's costs, a subscription type without cost data, or Cost Management failed.
CostNote = Literal["no_token", "forbidden", "unsupported", "unavailable"]


class ServiceCost(StrictModel):
    sku: str
    units: int
    location: str
    # None when the Retail Prices API is unreachable or doesn't list the tier there.
    list_price: ListPrice | None
    month_to_date: MonthToDate | None
    month_to_date_note: CostNote | None
    # Azure's own message when month_to_date failed.
    detail: str | None
    # Cost analysis for the service's resource group in the Azure portal.
    portal_url: str


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
    """An Azure OpenAI / Foundry deployment that can become a member backend."""

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
    # Existing APIM backend already pointing at this deployment, if any.
    backend_id: str | None


class DeploymentRef(StrictModel):
    account_id: str = Field(max_length=500, pattern=r"^/subscriptions/[^?#]+$")
    deployment: str = Field(min_length=1, max_length=128)


class DeploymentImport(StrictModel):
    deployments: list[DeploymentRef] = Field(min_length=1, max_length=30)
    circuit_breaker: CircuitBreaker | None = Field(default_factory=CircuitBreaker)


class DeploymentImportResult(StrictModel):
    # Backend IDs in request order, whether newly created or already present.
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
    "select_apim",
    "enable_apim_identity",
    "grant_foundry_access",
]
SetupKind = Literal[
    "sign_in",
    "azure_token",
    "apim_target",
    "apim_access",
    "discovery",
    "app_apim",
    "app_discovery",
    "apim_identity",
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
    # The app can at least read the APIM service, so the rest of the UI works.
    can_operate: bool
    local_sign_in: LocalSignIn | None = None


# What a setup report is doing, in the order it does it; each step calls ARM.
SetupStepKind = Literal[
    "sign_in",
    "apim_target",
    "apim_access",
    "app_apim",
    "discovery",
    "apim_identity",
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


# -- choosing or creating the APIM service ---------------------------------------------------

_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
# Resource group names: letters, digits, underscores, hyphens, periods and parentheses; no
# trailing period.
_RESOURCE_GROUP = r"^[-\w.()]{0,89}[-\w()]$"
# APIM service names become <name>.azure-api.net: a letter first, no trailing hyphen.
APIM_NAME_PATTERN = r"^[A-Za-z](?:[A-Za-z0-9-]{0,48}[A-Za-z0-9])?$"
# Tiers offered when creating a service. Consumption has no backend circuit breaker, which
# imported members use; classic Basic/Standard/Premium are slow to create and cost more.
ApimSku = Literal["BasicV2", "StandardV2", "Developer"]
# ``browser``: the public app, where each user's browser remembers their own choice.
# ``guest``: the public app's read-only guest view, which shows one configured service.
TargetSource = Literal["environment", "saved", "browser", "guest", "none"]


class TargetView(StrictModel):
    """The APIM service the app manages and where that choice comes from."""

    subscription_id: str | None
    resource_group: str | None
    apim_name: str | None
    tenant_id: str | None
    source: TargetSource
    # Locally without a fixed service in the environment, and always in the public app; never in
    # the Easy Auth deployment.
    can_change: bool


class TenantView(StrictModel):
    tenant_id: str
    display_name: str | None
    default_domain: str | None


class SubscriptionView(StrictModel):
    subscription_id: str
    display_name: str
    tenant_id: str | None
    state: str


class ApimServiceView(StrictModel):
    id: str
    name: str
    resource_group: str
    location: str
    sku: str
    provisioning_state: str | None
    # Created by this app (the managed-by tag).
    managed: bool
    # Whether the signed-in user may do every APIM read and write the app makes; None if unknown.
    can_manage: bool | None
    missing: list[str] = Field(default_factory=list)


class ResourceGroupView(StrictModel):
    name: str
    location: str


class ApimCreateOptions(StrictModel):
    resource_groups: list[ResourceGroupView]
    # Regions offering each tier in this subscription.
    locations: dict[ApimSku, list[str]]
    # Microsoft.ApiManagement is registered; otherwise creating registers it first.
    provider_registered: bool


class TargetSelect(StrictModel):
    tenant_id: str | None = Field(default=None, pattern=_GUID)
    subscription_id: str = Field(pattern=_GUID)
    resource_group: str = Field(pattern=_RESOURCE_GROUP)
    apim_name: str = Field(pattern=APIM_NAME_PATTERN)


class ApimCreate(StrictModel):
    tenant_id: str | None = Field(default=None, pattern=_GUID)
    subscription_id: str = Field(pattern=_GUID)
    # Created in ``location`` when it does not exist yet.
    resource_group: str = Field(pattern=_RESOURCE_GROUP)
    location: str = Field(pattern=r"^[a-z0-9]{2,40}$")
    name: str = Field(pattern=APIM_NAME_PATTERN)
    sku: ApimSku = "BasicV2"
    publisher_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=100)
    publisher_name: str = Field(min_length=1, max_length=100)


class SignInRequest(StrictModel):
    """Local browser sign-in, optionally to one tenant (e.g. to list its subscriptions)."""

    tenant_id: str | None = Field(default=None, pattern=_GUID)


# -- trying a pool API -----------------------------------------------------------------------

# Which APIs an APIM subscription opens: this API, a product containing it, or every API.
KeyScope = Literal["api", "product", "all_apis"]
# Path below the API, e.g. chat/completions. Plain segments only, so it stays under the API.
_TEST_PATH = re.compile(r"[A-Za-z0-9_~-][A-Za-z0-9._~-]*(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*")
DEFAULT_TEST_API_VERSION = "2025-04-01-preview"
MAX_TEST_BODY = 64 * 1024


class SubscriptionKeyView(StrictModel):
    """An APIM subscription whose key works for an API. The key itself is read separately."""

    id: str
    display_name: str
    scope: KeyScope
    product: str | None
    state: str
    # The test key this app creates for the API.
    managed: bool


class SubscriptionSecret(StrictModel):
    primary_key: str


class ApiTestRequest(StrictModel):
    path: str = Field(default="chat/completions", max_length=200)
    api_version: str | None = Field(
        default=DEFAULT_TEST_API_VERSION, pattern=r"^[A-Za-z0-9._-]{1,40}$"
    )
    subscription_key: SecretStr | None = Field(default=None, min_length=1, max_length=256)
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
    # Response headers that explain the result: request IDs, region, rate limits, Retry-After.
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
    """What clients of the pool's APIs saw, from APIM's metrics (filtered by ApiId)."""

    requests: int
    success: int
    # 429 answered to clients: every member was throttled, even after retries.
    throttled: int
    client_errors: int
    server_errors: int
    # Share of requests APIM didn't answer with a 5xx; None without traffic.
    availability: float | None
    duration_ms: float | None
    backend_duration_ms: float | None
    traffic: list[TrafficPoint]
    latency: list[LatencyPoint]


class MemberMetrics(StrictModel):
    """A member's Foundry deployment, from its Azure OpenAI metrics: all callers, not only APIM."""

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


class PoolMetrics(StrictModel):
    pool_id: str
    range: MetricsRange
    interval: str
    start: str
    end: str
    api_ids: list[str]
    gateway: GatewayMetrics | None
    # Why gateway metrics are missing, e.g. no metrics/read permission.
    gateway_error: str | None
    members: list[MemberMetrics]


# -- deleting and restoring an APIM service (local) ----------------------------------------


class ServiceDeleteCheck(StrictModel):
    """What deleting a service would take with it, shown before the user confirms."""

    name: str
    resource_group: str
    location: str
    sku: str
    # The service the app manages now; deleting it sends setup back to choosing one.
    current: bool
    pools: int
    members: int
    apis: int


class ServiceDelete(TargetSelect):
    # The service name typed again; checked here too, not only in the UI.
    confirm_name: str = Field(min_length=1, max_length=50)


class DeletedServiceView(StrictModel):
    """A soft-deleted APIM service Azure still keeps (48 hours) and can restore."""

    name: str
    # Short location name (eastus2), as the deletedservices endpoints address it.
    location: str
    display_location: str
    resource_group: str
    service_id: str
    deletion_date: str | None
    scheduled_purge_date: str | None


class ServiceRestore(StrictModel):
    tenant_id: str | None = Field(default=None, pattern=_GUID)
    subscription_id: str = Field(pattern=_GUID)
    location: str = Field(pattern=r"^[a-z0-9]{2,40}$")
    name: str = Field(pattern=APIM_NAME_PATTERN)
