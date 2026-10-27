"""Request and response models for APIM native backend pools.

A pool is a ``Pool`` backend whose members are ``Single`` backends; clients reach it through an
API whose policy routes to the pool (policy.py). Circuit breakers live on the member backends.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator

from ..models import (
    GUID_PATTERN,
    RESOURCE_GROUP_PATTERN,
    RESOURCE_ID_PATTERN,
    ApiTestRequestBase,
    CostNote,
    MonthToDate,
    PoolMemberView,
    PoolMemberWrite,
    ResourceGroupView,
    StrictModel,
)

ErrorReason = Literal["BackendConnectionFailure", "Timeout", "ClientConnectionFailure"]


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


# APIM service names become <name>.azure-api.net: a letter first, no trailing hyphen.
APIM_NAME_PATTERN = r"^[A-Za-z](?:[A-Za-z0-9-]{0,48}[A-Za-z0-9])?$"

# Tiers offered when creating a service. Consumption has no backend circuit breaker, which
# imported members use; classic Basic/Standard/Premium are slow to create and cost more.
ApimSku = Literal["BasicV2", "StandardV2", "Developer"]


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


class ApimCreateOptions(StrictModel):
    resource_groups: list[ResourceGroupView]
    # Regions offering each tier in this subscription.
    locations: dict[ApimSku, list[str]]
    # Microsoft.ApiManagement is registered; otherwise creating registers it first.
    provider_registered: bool


class TargetSelect(StrictModel):
    tenant_id: str | None = Field(default=None, pattern=GUID_PATTERN)
    subscription_id: str = Field(pattern=GUID_PATTERN)
    resource_group: str = Field(pattern=RESOURCE_GROUP_PATTERN)
    apim_name: str = Field(pattern=APIM_NAME_PATTERN)


class ApimCreate(StrictModel):
    tenant_id: str | None = Field(default=None, pattern=GUID_PATTERN)
    subscription_id: str = Field(pattern=GUID_PATTERN)
    # Created in ``location`` when it does not exist yet.
    resource_group: str = Field(pattern=RESOURCE_GROUP_PATTERN)
    location: str = Field(pattern=r"^[a-z0-9]{2,40}$")
    name: str = Field(pattern=APIM_NAME_PATTERN)
    sku: ApimSku = "BasicV2"
    publisher_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=100)
    publisher_name: str = Field(min_length=1, max_length=100)


# Which APIs an APIM subscription opens: this API, a product containing it, or every API.
KeyScope = Literal["api", "product", "all_apis"]

DEFAULT_TEST_API_VERSION = "2025-04-01-preview"


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


class ApiTestRequest(ApiTestRequestBase):
    api_version: str | None = Field(
        default=DEFAULT_TEST_API_VERSION, pattern=r"^[A-Za-z0-9._-]{1,40}$"
    )
    # An APIM subscription key; None calls the API without one.
    subscription_key: SecretStr | None = Field(default=None, min_length=1, max_length=256)


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
    tenant_id: str | None = Field(default=None, pattern=GUID_PATTERN)
    subscription_id: str = Field(pattern=GUID_PATTERN)
    location: str = Field(pattern=r"^[a-z0-9]{2,40}$")
    name: str = Field(pattern=APIM_NAME_PATTERN)
