"""Request and response models both gateways share: the setup wizard, the target, Foundry
deployments, model checks, the test call's common fields, the Monitor tab and the load test.

What only one gateway has is in ``apim/models.py`` and ``litellm/models.py``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Pool and member IDs: letters, digits and hyphens, starting with a letter or digit. Pool IDs
# are also the model name clients send, so they stay URL- and YAML-safe.
RESOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{0,79}$"
_RESOURCE_ID = re.compile(RESOURCE_ID_PATTERN)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# The gateway product the app manages: Azure API Management, or a LiteLLM proxy.
Product = Literal["apim", "litellm"]


class PoolMemberWrite(StrictModel):
    backend_id: str = Field(pattern=RESOURCE_ID_PATTERN)
    # 0-based here; APIM stores ``priority + 1``, and LiteLLM's ``order`` is ``priority + 1``.
    priority: int = Field(ge=0, le=99)
    weight: int = Field(ge=0, le=100)


class PoolMemberView(StrictModel):
    backend_id: str
    priority: int
    weight: int


class ModelCheckRequest(StrictModel):
    members: list[str] = Field(min_length=1, max_length=30)


# Where a member's rate limits come from: the deployment's own ``rateLimits``, or its
# capacity at the older chat models' 1,000 TPM and 6 RPM a unit (newer models differ).
QuotaSource = Literal["rate_limits", "capacity"]


class MemberModel(StrictModel):
    backend_id: str
    # ARM ID of the Foundry deployment the member points at; ``None`` when it points elsewhere.
    deployment_id: str | None
    # Read from the deployment resource, never inferred from a deployment or backend name.
    model_name: str | None
    model_version: str | None
    # Why a Foundry deployment's model could not be read. Such a member fails the check.
    problem: str | None
    # The deployment's type (GlobalStandard, ...), capacity units and the rate limits Azure
    # enforces on it, per minute; None when it can't be read (e.g. provisioned throughput).
    deployment_type: str | None = None
    capacity: int | None = None
    tpm: int | None = None
    rpm: int | None = None
    quota_source: QuotaSource | None = None


class ModelCheck(StrictModel):
    members: list[MemberModel]
    # Distinct model names, in member order.
    model_names: list[str]
    ok: bool
    message: str | None
    # What the pool's Foundry deployments allow together (each deployment once): failover and
    # weighted pools alike spread over them. None when no member's limits could be read.
    total_tpm: int | None = None
    total_rpm: int | None = None


# LiteLLM only. ``aca``: a Container App (created here or not); ``external``: any other LiteLLM
# proxy the user connected by URL, e.g. on AKS.
GatewayKind = Literal["aca", "external"]


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
    # APIM: turn on the service's system-assigned identity.
    "enable_apim_identity",
    # LiteLLM: an external gateway whose master key isn't known here (yet).
    "connect_gateway",
    "grant_foundry_access",
]
SetupKind = Literal[
    "sign_in",
    "azure_token",
    "gateway_target",
    "gateway_access",
    # LiteLLM only: the gateway answers, its database is connected, the master key works.
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
    # The kind of gateway chosen; None until one is.
    product: Product | None
    user: SetupIdentity | None
    checks: list[SetupCheck]
    # Nothing needs action; blocked or pending checks are not ready either.
    ready: bool
    # The app can work with the gateway (read the APIM service, or reach LiteLLM's admin API),
    # so the rest of the UI works.
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

GUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
# Resource group names: letters, digits, underscores, hyphens, periods and parentheses; no
# trailing period.
RESOURCE_GROUP_PATTERN = r"^[-\w.()]{0,89}[-\w()]$"
# ``browser``: the public app, where each user's browser remembers their own choice.
# ``guest``: the public app's read-only guest view, which shows one configured gateway.
TargetSource = Literal["environment", "saved", "browser", "guest", "none"]


class TargetView(StrictModel):
    """The gateway the app manages and where that choice comes from."""

    product: Product | None
    # The APIM service's or the LiteLLM gateway's name.
    gateway_name: str | None
    gateway_kind: GatewayKind | None
    subscription_id: str | None
    resource_group: str | None
    # External LiteLLM gateways only; a Container App's URL is read from Azure.
    gateway_url: str | None
    tenant_id: str | None
    source: TargetSource
    # Locally without a fixed gateway in the environment, and always in the public app; never in
    # the Easy Auth deployment.
    can_change: bool
    # LiteLLM: an AKS gateway the app deployed: its cluster, and whether the app is still
    # installing it.
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


class ResourceGroupView(StrictModel):
    name: str
    location: str


class SignInRequest(StrictModel):
    """Local browser sign-in, optionally to one tenant (e.g. to list its subscriptions)."""

    tenant_id: str | None = Field(default=None, pattern=GUID_PATTERN)


# -- trying a pool -----------------------------------------------------------------------------

# Path below the pool's endpoint, e.g. chat/completions. Plain segments only.
_TEST_PATH = re.compile(r"[A-Za-z0-9_~-][A-Za-z0-9._~-]*(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*")
MAX_TEST_BODY = 64 * 1024


class ApiTestRequestBase(StrictModel):
    """What every test call has; each gateway adds how it authenticates."""

    path: str = Field(default="chat/completions", max_length=200)
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
    # Response headers that explain the result: request IDs, which member or region answered,
    # retries and fallbacks, rate limits, Retry-After.
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
    """What clients of the pool saw, from the gateway: APIM's metrics of the APIs bound to the
    pool, or LiteLLM's spans (one per attempt, grouped into requests by their trace)."""

    requests: int
    success: int
    # 429 answered to clients: every member was throttled or cooling down.
    throttled: int
    client_errors: int
    server_errors: int
    # LiteLLM: requests that succeeded only because it retried on another member. APIM's
    # metrics can't tell, so None there.
    failovers: int | None = None
    # Share of requests not answered with a 5xx; None without traffic.
    availability: float | None
    # Average time the gateway took to answer (LiteLLM: from the first attempt's start to the
    # last one's end).
    duration_ms: float | None
    # Average time the members took (LiteLLM: of the attempts that succeeded).
    backend_duration_ms: float | None
    traffic: list[TrafficPoint]
    latency: list[LatencyPoint]


class MemberMetrics(StrictModel):
    """A member: its Foundry deployment's own metrics, which count every caller of the
    deployment, not only this gateway; and on LiteLLM the gateway's attempts on it (spans)."""

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
    # LiteLLM, from the gateway's spans; None on APIM and without Application Insights.
    gateway_attempts: int | None = None
    gateway_failures: int | None = None
    gateway_throttled: int | None = None


class PoolMetrics(StrictModel):
    pool_id: str
    range: MetricsRange
    interval: str
    start: str
    end: str
    # What clients reach the pool by: the APIs bound to it (APIM; none means no gateway
    # numbers), or its model names, the pool ID and its aliases (LiteLLM).
    names: list[str]
    gateway: GatewayMetrics | None
    # Why gateway metrics are missing: "no_app_insights" or "forbidden" (LiteLLM), or Azure's
    # message.
    gateway_error: str | None
    members: list[MemberMetrics]


# -- gateway dashboard -----------------------------------------------------------------------

# What a load series measures. APIM: ``capacity`` (classic tiers), ``cpu`` and ``memory`` (v2
# tiers). LiteLLM: ``replicas``, ``cpu`` and ``memory`` of each replica's allocation (Container
# Apps), ``node_cpu`` and ``node_memory`` (AKS), and its database's ``db_cpu``, ``db_memory``
# and ``db_connections``.
LoadMetric = Literal[
    "capacity",
    "cpu",
    "memory",
    "replicas",
    "node_cpu",
    "node_memory",
    "db_cpu",
    "db_memory",
    "db_connections",
]
LoadUnit = Literal["percent", "count"]


class LoadPoint(StrictModel):
    time: str
    average: float | None
    # The highest value in the time bin; None where Azure reports no maximum.
    peak: float | None


class LoadSeries(StrictModel):
    metric: LoadMetric
    unit: LoadUnit
    # The resource's name: the APIM service, the Container App, the cluster or the database.
    resource: str
    average: float | None
    peak: float | None
    # The last bin with a value.
    latest: float | None
    # Where it stops: the most replicas the gateway may scale to; None when there is none.
    limit: float | None = None
    points: list[LoadPoint]


class LoadProblem(StrictModel):
    resource: str
    # "forbidden": its metrics; "forbidden_resources": listing the resource group (LiteLLM, to
    # find the database); else Azure's message.
    error: str


class GatewayLoad(StrictModel):
    """The gateway's own dashboard: every client's traffic through it, and the load on the
    infrastructure it runs on."""

    range: MetricsRange
    interval: str
    start: str
    end: str
    # All its traffic: APIM's metrics of the whole service, or LiteLLM's spans of every model.
    traffic: GatewayMetrics | None
    # As ``PoolMetrics.gateway_error``.
    traffic_error: str | None
    series: list[LoadSeries]
    problems: list[LoadProblem]


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
    # The model name sent: LiteLLM, the pool ID or one of its aliases (None for the pool ID);
    # APIM, the pool's model (the member's URL fixes the deployment).
    model: str | None = Field(default=None, max_length=200)


class BenchmarkMember(StrictModel):
    # The member as the gateway's answer names it: LiteLLM's row ID (x-litellm-model-id), on
    # APIM the answering deployment's region (x-ms-region); None when the answer didn't say.
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
    # Requests answered after a failed attempt on another member, where the gateway says so
    # (LiteLLM's x-litellm-attempted-fallbacks; APIM retries without saying, so always 0).
    failovers: int
    members: list[BenchmarkMember]
    timeline: list[BenchmarkPoint]


class BenchmarkRun(StrictModel):
    id: str
    pool_id: str
    # The API the run called (APIM); None on LiteLLM, which is called by the pool's names.
    api_id: str | None = None
    state: Literal["running", "done", "stopped", "failed"]
    spec: BenchmarkSpec
    model: str
    started_at: str
    error: str | None
    summary: BenchmarkSummary
