"""Request and response models for pools on Azure AI Gateway.

A pool is a model alias in the gateway's default workspace: clients send the pool's ID as the
``model`` and the alias picks one of its backend models by priority and weight. Its members are
those backend models: model registrations under a model provider (one per Foundry account this
app imports from, or one per custom endpoint). The pool's failure rule and retry limit live on
the alias, so failover works without the members knowing about each other.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, model_validator

from ..models import (
    GUID_PATTERN,
    RESOURCE_GROUP_PATTERN,
    ApiTestRequestBase,
    BenchmarkSpec,
    CostNote,
    MonthToDate,
    PoolMemberView,
    PoolMemberWrite,
    ResourceGroupView,
    StrictModel,
)

# AI Gateway names: lower case letters, digits and hyphens, 3-24 characters, no hyphen at the
# ends. The name becomes the first label of the gateway's host name.
AIGATEWAY_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{1,22}[a-z0-9]$"
# An alias's description holds the marker, the pool's title and its description (1,024 at most).
MAX_DESCRIPTION = 600
MAX_RETRIES = 25
DEFAULT_RETRIES = 2


class FailureStatusCodeRange(StrictModel):
    minimum: int = Field(ge=100, le=599)
    maximum: int = Field(ge=100, le=599)

    @model_validator(mode="after")
    def validate_order(self) -> FailureStatusCodeRange:
        if self.minimum > self.maximum:
            raise ValueError("failure status code minimum cannot exceed maximum")
        return self


class CircuitBreaker(StrictModel):
    """The pool's failure rule, written to its alias: when a member answers one of these
    statuses (or doesn't answer at all), the gateway tries it ``same_member_retries`` more times,
    then moves the request on to the next member and keeps the failed one out of the pool for
    ``exclude_seconds``. The defaults are the app's: 408, 429 and 5xx, or no answer, for 60 s."""

    status_code_ranges: list[FailureStatusCodeRange] = Field(
        default_factory=lambda: [
            FailureStatusCodeRange(minimum=429, maximum=429),
            FailureStatusCodeRange(minimum=408, maximum=408),
            FailureStatusCodeRange(minimum=500, maximum=599),
        ],
        max_length=10,
    )
    # Timeouts and refused connections: the gateway got no HTTP answer.
    no_response: bool = True
    # 0 skips the member for the failed request only.
    exclude_seconds: int = Field(default=60, ge=0, le=86_400)
    same_member_retries: int = Field(default=0, ge=0, le=10)

    @model_validator(mode="after")
    def require_failure_signal(self) -> CircuitBreaker:
        if not self.status_code_ranges and not self.no_response:
            raise ValueError("a failure rule needs a status range or no_response")
        return self


MemberAuthType = Literal["api_key", "bearer"]


class MemberAuth(StrictModel):
    """How the gateway authenticates to a custom endpoint: a secret header the gateway keeps
    (it never returns the value). Foundry members use the gateway's managed identity instead."""

    type: MemberAuthType = "api_key"
    header_name: str = Field(default="api-key", pattern=r"^[A-Za-z0-9-]{1,64}$")
    # On update, omitting the secret keeps the one the gateway has.
    secret: SecretStr | None = Field(default=None, min_length=1, max_length=4096)


class MemberBackendWrite(StrictModel):
    """A member that isn't a Foundry deployment: an OpenAI-compatible endpoint (its base URL,
    e.g. ``https://host/v1``) and the model it serves."""

    title: str | None = Field(default=None, max_length=300)
    url: HttpUrl
    model: str = Field(min_length=1, max_length=200)
    auth: MemberAuth = Field(default_factory=MemberAuth)
    # ``None`` creates the member; otherwise the update must match its ETag.
    etag: str | None = None


# ``foundry``: a deployment of a Foundry account, called as the gateway's managed identity;
# ``custom``: any other endpoint; ``github``: GitHub Models (read-only here).
MemberKind = Literal["foundry", "custom", "github"]


class BackendView(StrictModel):
    """A member: a model registered in the gateway, under its model provider."""

    id: str
    title: str | None
    # ARM ID of the registration, which aliases point at.
    resource_id: str
    # The model provider it is registered under.
    provider: str
    kind: MemberKind
    # Foundry: the deployment's URL, ``<account endpoint>/openai/deployments/<name>``; custom:
    # the endpoint's base URL.
    url: str | None
    # The model (Foundry: the deployment) the gateway asks the provider for.
    model: str
    # The Foundry deployment's ARM ID; None for other members.
    deployment_id: str | None
    auth_type: Literal["managed_identity", "api_key", "bearer", "custom"]
    header_name: str | None
    # Pools it is a member of.
    pools: list[str]
    managed: bool
    protected: bool
    etag: str | None


class PoolWrite(StrictModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    members: list[PoolMemberWrite] = Field(min_length=2, max_length=30)
    # Written to the alias. ``None`` keeps what the pool has (the defaults when new).
    circuit_breaker: CircuitBreaker | None = None
    # Extra sends of one request across the pool's members (the alias's maxRetryAttempts).
    retries: int | None = Field(default=None, ge=0, le=MAX_RETRIES)
    # ``None`` creates a new pool; otherwise the update must match the alias's ETag.
    etag: str | None = None

    @model_validator(mode="after")
    def validate_members(self) -> PoolWrite:
        backend_ids = [member.backend_id.casefold() for member in self.members]
        if len(backend_ids) != len(set(backend_ids)):
            raise ValueError("a pool cannot repeat a member")
        if any(member.weight < 1 for member in self.members):
            # An alias's weights are 1-100; a standby member has a lower priority instead.
            raise ValueError("AI Gateway weights are 1 to 100; give a standby a lower priority")
        return self


class PoolView(StrictModel):
    id: str
    # The name clients send as ``model`` (the alias's routing name; the pool ID for pools this
    # app created).
    model_name: str
    title: str | None
    description: str | None
    members: list[PoolMemberView]
    circuit_breaker: CircuitBreaker | None
    # The alias has failure rules this app didn't write; saving replaces them with the pool's.
    custom_failures: bool
    retries: int | None
    # Inline policies on the alias (token and cost limits, ...), kept as they are on a save.
    policies: list[str]
    managed: bool
    protected: bool
    etag: str | None


class PoolEndpoint(StrictModel):
    """How clients call a pool: the gateway's OpenAI v1 base URL and the pool's model name."""

    pool_id: str
    # ``https://<host>/default/models/openai/v1``; None while the gateway has no host name yet.
    base_url: str | None
    model_name: str


class Capabilities(StrictModel):
    gateway_name: str
    resource_group: str
    id_prefix: str
    protected_prefixes: list[str]
    # ARM ID of the gateway, for links to the Azure portal.
    gateway_resource_id: str
    # The system-assigned identity the gateway calls Foundry members as.
    gateway_principal_id: str | None
    # ``https://<name>.<region>.ai.gateway.azure.com``.
    gateway_url: str | None
    location: str | None
    sku: str | None
    # Application Insights component its telemetry exporter sends to; None without one.
    app_insights_id: str | None
    discovery_subscriptions: list[str]


class RemovalPlan(StrictModel):
    """What removing pools takes with it, shown before the user confirms. Nothing is written."""

    pools: list[str]
    # Members this app created that no remaining pool uses, deleted with their model provider
    # once it has no models left.
    delete_members: list[str]
    # Cleanup only: the test key this app created, which opens every alias on the gateway.
    delete_keys: list[str]
    # Cleanup only: pools this app didn't create, which it leaves alone.
    kept_pools: list[str]
    # Why the plan can't run; removal refuses while this is not empty.
    blocked: list[str]


class RemovalConfirm(StrictModel):
    """The plan the user reviewed. Removal recomputes it and refuses to delete anything that is
    not listed here."""

    pools: list[str] = Field(max_length=500)
    delete_keys: list[str] = Field(default_factory=list, max_length=10)
    # Single-pool removal: the alias's ETag as loaded, for a conditional delete.
    etag: str | None = None


class CleanupConfirm(RemovalConfirm):
    # The gateway name typed again; checked here too, not only in the UI.
    confirm_name: str = Field(min_length=1, max_length=50)


class RemovalResult(StrictModel):
    pools: list[str]
    deleted_members: list[str]
    deleted_keys: list[str]


class ListPrice(StrictModel):
    """The gateway's retail price (Azure Retail Prices API), before discounts and tax. AI
    Gateway is billed per request, not per hour."""

    # Per 10,000 requests, at the highest tier listed; 0 while Azure lists no charge.
    per_10k_requests: float
    currency: str


class ServiceCost(StrictModel):
    sku: str | None
    location: str | None
    # None when the Retail Prices API is unreachable or doesn't list the gateway's SKU.
    list_price: ListPrice | None
    month_to_date: MonthToDate | None
    month_to_date_note: CostNote | None
    detail: str | None
    portal_url: str


# -- choosing, creating and deleting a gateway ---------------------------------------------


class GatewayView(StrictModel):
    id: str
    name: str
    resource_group: str
    location: str
    sku: str
    url: str | None
    provisioning_state: str | None
    # Created by this app (the managed-by tag).
    managed: bool
    # Whether the signed-in user may do every read and write the app makes; None if unknown.
    can_manage: bool | None
    missing: list[str] = Field(default_factory=list)


class GatewayCreateOptions(StrictModel):
    resource_groups: list[ResourceGroupView]
    # Regions offering AI Gateway to this subscription.
    locations: list[str]
    # Microsoft.ApiManagement is registered; otherwise creating registers it first.
    provider_registered: bool


class TargetSelect(StrictModel):
    tenant_id: str | None = Field(default=None, pattern=GUID_PATTERN)
    subscription_id: str = Field(pattern=GUID_PATTERN)
    resource_group: str = Field(pattern=RESOURCE_GROUP_PATTERN)
    aigateway_name: str = Field(pattern=AIGATEWAY_NAME_PATTERN)
    # Choose it even though the check before switching refused it (424), to fix it in setup.
    force: bool = False


class GatewayCreate(StrictModel):
    tenant_id: str | None = Field(default=None, pattern=GUID_PATTERN)
    subscription_id: str = Field(pattern=GUID_PATTERN)
    # Created in ``location`` when it does not exist yet.
    resource_group: str = Field(pattern=RESOURCE_GROUP_PATTERN)
    location: str = Field(pattern=r"^[a-z0-9]{2,40}$")
    name: str = Field(pattern=AIGATEWAY_NAME_PATTERN)


class GatewayDeleteCheck(StrictModel):
    """What deleting a gateway would remove, shown before the user confirms."""

    name: str
    resource_group: str
    location: str
    sku: str
    # The gateway the app manages now; deleting it sends setup back to choosing one.
    current: bool
    pools: int
    members: int
    keys: int


class GatewayDelete(TargetSelect):
    # The gateway name typed again; checked here too, not only in the UI.
    confirm_name: str = Field(min_length=1, max_length=50)


# -- trying a pool -------------------------------------------------------------------------


class ApiKeyView(StrictModel):
    """A runtime API key. Every key opens every alias on the gateway, not only one pool."""

    id: str
    display_name: str | None
    state: str | None
    expires: str | None
    # The test key this app creates.
    managed: bool


class ApiKeySecret(StrictModel):
    primary_key: str


# Paths of the gateway's OpenAI v1 surface a test call may use.
CallPath = Literal["chat/completions", "responses", "embeddings"]


class ApiTestRequest(ApiTestRequestBase):
    # A runtime API key of the gateway; a call without one is refused with a hint.
    key: SecretStr | None = Field(default=None, min_length=1, max_length=256)


class BenchmarkStart(StrictModel):
    spec: BenchmarkSpec
    # The runtime API key to call the pool with; never stored or returned.
    key: SecretStr = Field(min_length=1, max_length=256)
