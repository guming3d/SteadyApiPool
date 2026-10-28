"""Request and response models for pools on LiteLLM.

A pool is a LiteLLM model group: one row in the gateway's model table per member, all with the
pool's ID as their ``model_name``. Members are identified by IDs this app gives them; a member
in two pools is two rows.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, model_validator

from ..models import (
    GUID_PATTERN,
    RESOURCE_GROUP_PATTERN,
    ApiTestRequestBase,
    CostNote,
    GatewayKind,
    MonthToDate,
    PoolMemberView,
    PoolMemberWrite,
    ResourceGroupView,
    StrictModel,
)

# Model names clients may send for a pool (aliases): what OpenAI and Azure deployment names use.
MODEL_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"


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
    # The traffic the gateway was deployed (or last resized) for; None for one from before the
    # sizer, or one this app didn't deploy.
    sized_for: GatewaySizing | None = None
    # Application Insights component that receives the gateway's OpenTelemetry spans.
    app_insights_id: str | None
    discovery_subscriptions: list[str]


CostItemKind = Literal[
    "container_apps", "environment", "postgres", "redis", "aks_nodes", "aks_tier"
]


class CostItem(StrictModel):
    kind: CostItemKind
    # What is priced, e.g. "1.25 vCPU, 2.5 GiB x 1 replica" or "B1ms + 32 GB".
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


# Container App names: lower case letters, digits and hyphens, a letter first, no trailing or
# double hyphen; the template derives resource names (psql-<name>-..., id-<name>) from it.
GATEWAY_NAME_PATTERN = r"^[a-z](?:[a-z0-9]|-[a-z0-9]){1,30}$"

# ``dev``: one replica and no Redis; ``production``: 2-10 replicas sharing cooldowns in Redis.
GatewayProfile = Literal["dev", "production"]

# Where a gateway the app deploys runs: Azure Container Apps, or a new AKS cluster.
GatewayPlatform = Literal["aca", "aks"]


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


class GatewayCreateOptions(StrictModel):
    resource_groups: list[ResourceGroupView]
    # Regions where this subscription can create Container Apps environments.
    locations: list[str]
    # Resource providers the template needs that are not registered yet; creating registers them.
    unregistered_providers: list[str]


class TargetSelect(StrictModel):
    tenant_id: str | None = Field(default=None, pattern=GUID_PATTERN)
    subscription_id: str = Field(pattern=GUID_PATTERN)
    resource_group: str = Field(pattern=RESOURCE_GROUP_PATTERN)
    gateway_name: str = Field(min_length=2, max_length=32, pattern=r"^[a-z0-9-]+$")


# How fast the models behind the gateway answer (sizing.SPEEDS), unless the scenario gives the
# average response time itself.
ModelSpeed = Literal["fast", "standard", "reasoning"]


class GatewaySizing(StrictModel):
    """The traffic a gateway is sized for (sizing.plan): its peak, not its average."""

    peak_rpm: int = Field(ge=1, le=1_000_000)
    input_tokens: int = Field(ge=1, le=2_000_000)
    output_tokens: int = Field(ge=1, le=200_000)
    # The longest a call may take, retries aside: LiteLLM's request_timeout.
    timeout_seconds: int = Field(ge=10, le=3600)
    streaming: bool = True
    model_speed: ModelSpeed = "standard"
    # Overrides the speed preset: the average seconds a call takes, start to last token.
    response_seconds: float | None = Field(default=None, gt=0, le=3600)
    headroom_percent: int = Field(default=25, ge=0, le=200)
    # One more replica than the peak needs, at least two, and zones for AKS nodes.
    zone_redundant: bool = True
    kubernetes_team: bool = False
    self_hosted_models: bool = False
    private_network: bool = False


# What the plan says about itself; the UI words each code (litellm/messages.ts, plan*).
PlanNoteCode = Literal[
    "platform_simple",
    "platform_team",
    "platform_gpu",
    "platform_timeout",
    "platform_scale",
    "platform_chosen",
    "private_aks",
    "private_manual",
    "call_exceeds_timeout",
    "single_replica",
    "redis_buffer",
    "foundry_quota",
    "outbound_capped",
    "pool_lowered",
    "aca_timeout",
    "aca_replicas",
    "aks_too_big",
    "database_too_small",
]


class PlanNote(StrictModel):
    code: PlanNoteCode
    values: dict[str, float | str] = Field(default_factory=dict)


class AksPlan(StrictModel):
    node_size: str
    node_vcpus: int
    pods_per_node: int
    node_min: int
    node_max: int
    # Standard (uptime SLA) when zone redundant, else Free.
    tier: Literal["Free", "Standard"]
    outbound_ips: int
    # SNAT ports each node gets, and what it would need with every call slowed to the timeout.
    outbound_ports: int
    ports_needed: int


class GatewayPlan(StrictModel):
    """What sizing.plan derives from a GatewaySizing for one platform, with its arithmetic."""

    platform: GatewayPlatform
    # Kept as a label (tags, the gateway panel): "dev" for a single replica, else "production".
    profile: GatewayProfile
    rps: float
    tpm: int
    call_seconds: float
    in_flight: int
    # Calls in flight if every call ran to the timeout (Foundry slowed down).
    in_flight_slow: int
    cpu_ms: float
    # What one replica serves at the target CPU, and how many calls it holds at most.
    replica_rps: float
    replica_in_flight: int
    bound_by: Literal["cpu", "in_flight"]
    peak_replicas: int
    min_replicas: int
    max_replicas: int
    # Container Apps' HTTP rule: requests a second per replica before it adds one.
    scale_rps: int
    replica_cpu: float
    replica_memory_gib: float
    bandwidth_mbps: float
    postgres_sku: str
    postgres_tier: Literal["Burstable", "GeneralPurpose"]
    postgres_connections: int
    postgres_limit: int
    db_pool: int
    redis_sku: str | None
    aks: AksPlan | None
    request_timeout: int
    route_timeout: int
    reasons: list[PlanNote]
    warnings: list[PlanNote]
    # Anything here keeps the plan from being deployed on this platform.
    problems: list[PlanNote]


class GatewayPlans(StrictModel):
    recommended: GatewayPlatform
    aca: GatewayPlan
    aks: GatewayPlan


# What resizing a gateway touches, one item each (resize.py).
ResizeItem = Literal[
    "replicas",
    "scale_rule",
    "replica_size",
    "nodes",
    "pods",
    "database",
    "node_size",
    "outbound",
    "tier",
    "redis",
    "timeout",
]
# apply: the app changes it; optional: only when asked (the database restarts); manual: the app
# leaves it to the user (``command`` says how, when there is one).
ResizeAction = Literal["apply", "optional", "manual"]


class ResizeChange(StrictModel):
    item: ResizeItem
    current: str
    proposed: str
    action: ResizeAction
    command: str | None = None


class ResizePlan(StrictModel):
    """What resizing the gateway for a scenario would change, read without changing anything."""

    platform: GatewayPlatform
    plan: GatewayPlan
    # Only what differs from the gateway as it is now.
    changes: list[ResizeChange]
    # The gateway as it was read; applying refuses a gateway that has changed since.
    etag: str
    # The scenario it was sized for before, if any.
    sized_for: GatewaySizing | None
    problems: list[str]


class ResizeApply(StrictModel):
    sizing: GatewaySizing
    etag: str = Field(min_length=1, max_length=128)
    # Also resize the database: the server restarts.
    database: bool = False


class ResizeResult(StrictModel):
    applied: list[ResizeItem]
    # AKS: the Run Command changing LiteLLM's pods, followed by ``GET .../resize/{command}``.
    command_id: str | None


class ResizeCommand(StrictModel):
    state: Literal["running", "succeeded", "failed"]
    exit_code: int | None
    log_tail: str | None


class GatewayCreate(StrictModel):
    tenant_id: str | None = Field(default=None, pattern=GUID_PATTERN)
    subscription_id: str = Field(pattern=GUID_PATTERN)
    # Created in ``location`` when it does not exist yet.
    resource_group: str = Field(pattern=RESOURCE_GROUP_PATTERN)
    location: str = Field(pattern=r"^[a-z0-9]{2,40}$")
    name: str = Field(max_length=32, pattern=GATEWAY_NAME_PATTERN)
    sizing: GatewaySizing
    platform: GatewayPlatform = "aca"
    # When a resource fails: stop the deployment and delete what it created (cleanup.py).
    cleanup_on_failure: bool = True


class GatewayPreflight(StrictModel):
    """What would keep a deployment there from succeeding, read before anything is created."""

    location: str
    # What would be deployed, and its retail price in the region (None when prices are down).
    plan: GatewayPlan
    list_price: ListPrice | None
    problems: list[str]
    # Common regions where nothing is in the way; only looked up when there are problems.
    open_regions: list[str]


class GatewayConnect(StrictModel):
    """A LiteLLM proxy this app didn't deploy (e.g. on AKS), reached by URL and master key."""

    tenant_id: str | None = Field(default=None, pattern=GUID_PATTERN)
    # Searched for Foundry deployments; the gateway itself needs no subscription.
    subscription_id: str = Field(pattern=GUID_PATTERN)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    url: HttpUrl
    master_key: SecretStr = Field(min_length=8, max_length=512)
    # The managed identity's object ID the gateway calls Foundry as, for the access check.
    principal_id: str | None = Field(default=None, pattern=GUID_PATTERN)
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


class DeployCleanup(StrictModel):
    """Deleting what a failed deployment created."""

    state: Literal["running", "done", "failed"]
    # What was deleted, and what Azure is still deleting (an environment takes its time).
    deleted: list[str]
    deleting: list[str]
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
    # Deployed with cleanup_on_failure; then cleanup follows a failure of the Azure resources.
    auto_cleanup: bool = False
    cleanup: DeployCleanup | None = None


class AksBundleFile(StrictModel):
    path: str
    content: str


class AksBundle(StrictModel):
    """Files that deploy the gateway on an existing AKS cluster with LiteLLM's Helm chart."""

    name: str
    namespace: str
    files: list[AksBundleFile]


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


class ApiTestRequest(ApiTestRequestBase):
    # The virtual key to call the pool with; a call without one is refused with a hint.
    key: SecretStr | None = Field(default=None, min_length=1, max_length=256)


# Limits per run, against cost and mistakes (the CLI can lift them with --i-know).
BENCHMARK_MAX_USERS = 50

BENCHMARK_MAX_SECONDS = 600

BENCHMARK_MAX_REQUESTS = 5000

PromptSize = Literal["short", "medium", "long"]


class BenchmarkSpec(StrictModel):
    """A closed-loop load: ``users`` simulated users, each sending a request, waiting for the
    answer and sending the next, until ``duration_seconds`` or ``max_requests``."""

    users: int = Field(default=2, ge=1, le=BENCHMARK_MAX_USERS)
    duration_seconds: int = Field(default=30, ge=1, le=BENCHMARK_MAX_SECONDS)
    max_requests: int = Field(default=BENCHMARK_MAX_REQUESTS, ge=1, le=BENCHMARK_MAX_REQUESTS)
    # Users join evenly over this many seconds.
    ramp_up_seconds: int = Field(default=0, ge=0, le=BENCHMARK_MAX_SECONDS)
    # A cap on requests started per second; None for as fast as the users go.
    rate: float | None = Field(default=None, gt=0, le=1000)
    prompt_size: PromptSize = "short"
    max_tokens: int = Field(default=128, ge=1, le=4096)
    # Streaming measures the time to the first token.
    stream: bool = True
    # The pool ID or one of its aliases; None for the pool ID.
    model: str | None = Field(default=None, max_length=200)


class BenchmarkStart(StrictModel):
    spec: BenchmarkSpec
    # The virtual key to call the pool with; never stored or returned.
    key: SecretStr = Field(min_length=1, max_length=256)


class BenchmarkMember(StrictModel):
    # LiteLLM's row ID (x-litellm-model-id); None when the gateway didn't say.
    member: str | None
    requests: int
    share: float
    latency_p50_ms: float | None
    errors: int


class BenchmarkPoint(StrictModel):
    # Seconds since the start.
    second: int
    requests: int
    errors: int
    throttled: int
    latency_p50_ms: float | None
    users: int


class BenchmarkSummary(StrictModel):
    elapsed_seconds: float
    requests: int
    successes: int
    throttled: int
    # Other failures: HTTP status (as text) or "timeout" / "connection" -> count.
    errors: dict[str, int]
    requests_per_second: float
    success_rate: float | None
    latency_p50_ms: float | None
    latency_p90_ms: float | None
    latency_p95_ms: float | None
    latency_p99_ms: float | None
    latency_max_ms: float | None
    ttft_p50_ms: float | None
    ttft_p95_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    output_tokens_per_second: float
    # Requests LiteLLM answered after a failed attempt on another member.
    failovers: int
    members: list[BenchmarkMember]
    timeline: list[BenchmarkPoint]


class BenchmarkRun(StrictModel):
    id: str
    pool_id: str
    state: Literal["running", "done", "stopped", "failed"]
    spec: BenchmarkSpec
    model: str
    started_at: str
    error: str | None
    summary: BenchmarkSummary


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
