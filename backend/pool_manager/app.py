"""FastAPI entry point: JSON API under ``/api`` and the built frontend at ``/``."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .access import AccessChecker, user_token
from .api_test import ApiTester
from .auth import acting_user, caller_name, current_principal, is_owner, owner, writer
from .benchmark import BenchmarkRunner
from .config import Settings, get_settings
from .cost import CostReader
from .deps import (
    User,
    get_access,
    get_arm,
    get_benchmarks,
    get_cost,
    get_discovery,
    get_monitor,
    get_request_settings,
    get_service,
    get_sessions,
    get_targets,
    get_tester,
    get_user,
    get_verifier,
)
from .discovery import DeploymentDiscovery
from .entra import API_SCOPE_NAME, ARM_USER_SCOPE
from .gateway_template import aks_bundle
from .metrics import PoolMonitor
from .model_deploy import ModelDeployer
from .models import (
    AksBundle,
    AliasView,
    AliasWrite,
    ApiTestRequest,
    ApiTestResult,
    BackendView,
    BenchmarkRun,
    BenchmarkStart,
    Capabilities,
    CleanupConfirm,
    ConfigPreview,
    ConfigView,
    DeployableModel,
    DeploymentImport,
    DeploymentImportResult,
    DeploymentView,
    DeviceCodePrompt,
    DeviceCodeStatus,
    FoundryAccessRequest,
    FoundryAccountView,
    FoundryDeploymentCreate,
    FoundryDeploymentStatus,
    GatewayConnect,
    GatewayCreate,
    GatewayCreateOptions,
    GatewayDelete,
    GatewayDeleteCheck,
    GatewayDeployStatus,
    GatewayView,
    MemberBackendWrite,
    MetricsRange,
    ModelCapacity,
    ModelCheck,
    ModelCheckRequest,
    PoolEndpoint,
    PoolMetrics,
    PoolView,
    PoolWrite,
    Principal,
    PublicConfig,
    RemovalConfirm,
    RemovalPlan,
    RemovalResult,
    ServiceCost,
    SetupCheck,
    SetupProgress,
    SetupReport,
    SignInRequest,
    SubscriptionView,
    TargetSelect,
    TargetView,
    TenantView,
    VirtualKeySecret,
    VirtualKeyView,
)
from .service import PoolManagerService, ServiceError
from .sessions import Sessions
from .target import TargetChooser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# azure-identity logs every token request and its headers at INFO.
logging.getLogger("azure").setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).parent / "static"

# Re-exported: tests and tools override these factories through app.dependency_overrides.
__all__ = [
    "app",
    "get_access",
    "get_arm",
    "get_benchmarks",
    "get_discovery",
    "get_monitor",
    "get_request_settings",
    "get_service",
    "get_sessions",
    "get_settings",
    "get_targets",
    "get_tester",
    "get_user",
    "get_verifier",
]


Service = Annotated[PoolManagerService, Depends(get_service)]
Discovery = Annotated[DeploymentDiscovery, Depends(get_discovery)]
Access = Annotated[AccessChecker, Depends(get_access)]
Targets = Annotated[TargetChooser, Depends(get_targets)]
Tester = Annotated[ApiTester, Depends(get_tester)]
Benchmarks = Annotated[BenchmarkRunner, Depends(get_benchmarks)]
Monitor = Annotated[PoolMonitor, Depends(get_monitor)]
Cost = Annotated[CostReader, Depends(get_cost)]
# In the public app, with the gateway the caller's browser chose.
SettingsDep = Annotated[Settings, Depends(get_request_settings)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Reader = Annotated[Principal, Depends(current_principal)]
Writer = Annotated[Principal, Depends(writer)]
Owner = Annotated[Principal, Depends(owner)]
# Setup fixes run with the user's own Azure rights, so they need sign-in but not POOL_OWNERS.
Actor = Annotated[Principal, Depends(acting_user)]
# Only the caller's name: no Azure call, so it answers while their other requests wait on ARM.
Caller = Annotated[str, Depends(caller_name)]

GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
TenantQuery = Annotated[str | None, Query(pattern=GUID)]
SubscriptionQuery = Annotated[str, Query(pattern=GUID)]


def require_target(settings: SettingsDep) -> None:
    if not settings.has_target:
        raise ServiceError(409, "No gateway is chosen yet; deploy, choose or connect one in setup")


api = APIRouter(prefix="/api")
# Everything that works on the gateway, which locally may not be chosen yet.
bound = APIRouter(dependencies=[Depends(require_target)])


# Unauthenticated: what the browser needs to sign in (the public app) or that it needn't.
@api.get("/config")
def public_config(settings: AppSettings) -> PublicConfig:
    if not settings.public:
        return PublicConfig(mode="local" if settings.local else "deployed")
    client_id = settings.entra_client_id
    return PublicConfig(
        mode="public",
        client_id=client_id,
        # Any work or school account; the token's tenant is checked on every request.
        authority=f"{settings.entra_authority.rstrip('/')}/organizations",
        api_scope=f"api://{client_id}/{API_SCOPE_NAME}",
        consent_scopes=[ARM_USER_SCOPE],
        guest=settings.guest_enabled,
    )


@api.get("/me")
def me(principal: Reader) -> Principal:
    return principal


# The public app: forgets the caller's sessions and Azure tokens. The browser then signs out of
# Entra too.
@api.post("/session/sign-out", status_code=204)
def session_sign_out(
    user: User,
    sessions: Annotated[Sessions | None, Depends(get_sessions)],
    _: Actor,
) -> Response:
    if user is None or sessions is None:
        raise ServiceError(400, "Only the public app has sessions to sign out of")
    sessions.drop(user.key)
    return Response(status_code=204)


@bound.get("/capabilities")
def capabilities(service: Service, _: Reader) -> Capabilities:
    return service.capabilities()


@bound.get("/backends")
def list_backends(service: Service, _: Reader) -> list[BackendView]:
    return service.list_backends()


@bound.get("/backends/{backend_id}")
def get_backend(backend_id: str, service: Service, _: Reader) -> BackendView:
    return service.get_backend(backend_id)


@bound.put("/backends/{backend_id}")
def put_backend(
    backend_id: str, body: MemberBackendWrite, service: Service, principal: Writer
) -> BackendView:
    return service.put_backend(backend_id, body, principal.name)


# Only members no pool uses yet; members of pools go when their pools drop them.
@bound.delete("/backends/{backend_id}", status_code=204)
def delete_backend(backend_id: str, service: Service, principal: Writer) -> Response:
    service.delete_backend(backend_id, principal.name)
    return Response(status_code=204)


@bound.get("/pools")
def list_pools(service: Service, _: Reader) -> list[PoolView]:
    return service.list_pools()


# Read-only: the same-model check that saving a pool enforces, for the editor to show first.
@bound.post("/pools/model-check")
def check_pool_models(body: ModelCheckRequest, service: Service, _: Reader) -> ModelCheck:
    return service.check_models(body.members)


@bound.get("/pools/{pool_id}")
def get_pool(pool_id: str, service: Service, _: Reader) -> PoolView:
    return service.get_pool(pool_id)


@bound.put("/pools/{pool_id}")
def put_pool(pool_id: str, body: PoolWrite, service: Service, principal: Writer) -> PoolView:
    return service.put_pool(pool_id, body, principal.name)


# Read-only: the LiteLLM config a save would write, to review or export first.
@bound.post("/pools/{pool_id}/preview")
def preview_pool(pool_id: str, body: PoolWrite, service: Service, _: Reader) -> ConfigPreview:
    return service.preview_pool(pool_id, body)


# The pool as LiteLLM config.yaml describes it (no secrets).
@bound.get("/pools/{pool_id}/config")
def pool_config(pool_id: str, service: Service, _: Reader) -> ConfigView:
    return service.config(pool_id)


# Read-only: the gateway's spans for the pool and its Foundry members' metrics.
@bound.get("/pools/{pool_id}/metrics")
def pool_metrics(
    pool_id: str,
    monitor: Monitor,
    _: Reader,
    range: MetricsRange = "1h",  # noqa: A002 - the query parameter's name
    refresh: bool = False,
) -> PoolMetrics:
    return monitor.get(pool_id, range, refresh=refresh)


# How clients call the pool: base URL and model names.
@bound.get("/pools/{pool_id}/endpoint")
def pool_endpoint(pool_id: str, service: Service, _: Reader) -> PoolEndpoint:
    return service.endpoint(pool_id)


@bound.post("/pools/{pool_id}/aliases")
def add_alias(pool_id: str, body: AliasWrite, service: Service, principal: Writer) -> PoolEndpoint:
    return service.add_alias(pool_id, body.name, principal.name)


@bound.delete("/pools/{pool_id}/aliases/{name}")
def remove_alias(pool_id: str, name: str, service: Service, principal: Writer) -> PoolEndpoint:
    return service.remove_alias(pool_id, name, principal.name)


@bound.get("/aliases")
def list_aliases(service: Service, _: Reader) -> list[AliasView]:
    return service.list_aliases()


# Read-only: what removing the pool takes with it (aliases, test keys).
@bound.get("/pools/{pool_id}/removal")
def pool_removal_plan(pool_id: str, service: Service, _: Reader) -> RemovalPlan:
    return service.pool_removal_plan(pool_id)


@bound.post("/pools/{pool_id}/remove")
def remove_pool(
    pool_id: str, body: RemovalConfirm, service: Service, principal: Writer
) -> RemovalResult:
    return service.remove_pool(pool_id, body, principal.name)


# Read-only: this month's spend (with the user's own Azure rights) and the list price.
@bound.get("/cost")
def service_cost(
    request: Request, cost: Cost, access: Access, principal: Reader, refresh: bool = False
) -> ServiceCost:
    user = access.user_arm(user_token(request.headers))
    return cost.get(user, principal.name, refresh=refresh)


# Read-only: everything this app created on the gateway, which cleanup would remove.
@bound.get("/cleanup")
def cleanup_plan(service: Service, _: Reader) -> RemovalPlan:
    return service.cleanup_plan()


@bound.post("/cleanup")
def cleanup(body: CleanupConfirm, service: Service, principal: Writer) -> RemovalResult:
    return service.cleanup(body, principal.name)


# -- trying a pool -------------------------------------------------------------------------
# Keys are LiteLLM virtual keys, read and made with the master key, so only owners see them.


@bound.get("/pools/{pool_id}/keys")
def list_keys(pool_id: str, tester: Tester, _: Owner) -> list[VirtualKeyView]:
    return tester.keys(pool_id)


# The only time the key itself is returned.
@bound.post("/pools/{pool_id}/keys")
def create_key(pool_id: str, tester: Tester, principal: Writer) -> VirtualKeySecret:
    return tester.create_key(pool_id, principal.name)


@bound.post("/pools/{pool_id}/test")
def test_pool(
    pool_id: str, body: ApiTestRequest, tester: Tester, principal: Actor
) -> ApiTestResult:
    return tester.call(pool_id, body, principal.name)


# A load test sends real, billed traffic with the caller's key, so only owners start one.
@bound.post("/pools/{pool_id}/benchmarks")
def start_benchmark(
    pool_id: str,
    body: BenchmarkStart,
    tester: Tester,
    benchmarks: Benchmarks,
    principal: Writer,
) -> BenchmarkRun:
    base_url, model = tester.target(pool_id, body.spec.model)
    return benchmarks.start(
        pool_id,
        body.spec,
        f"{base_url}/chat/completions",
        body.key.get_secret_value(),
        model,
        principal.name,
    )


@bound.get("/pools/{pool_id}/benchmarks/{run_id}")
def get_benchmark(pool_id: str, run_id: str, benchmarks: Benchmarks, _: Owner) -> BenchmarkRun:
    return benchmarks.get(pool_id, run_id)


@bound.post("/pools/{pool_id}/benchmarks/{run_id}/stop")
def stop_benchmark(pool_id: str, run_id: str, benchmarks: Benchmarks, _: Writer) -> BenchmarkRun:
    return benchmarks.stop(pool_id, run_id)


@bound.get("/deployments")
def list_deployments(
    discovery: Discovery, _: Reader, refresh: bool = False
) -> list[DeploymentView]:
    return discovery.list(refresh=refresh)


@bound.post("/deployments/import")
def import_deployments(
    body: DeploymentImport, discovery: Discovery, principal: Writer
) -> DeploymentImportResult:
    return discovery.import_deployments(body, principal.name)


# Creating Foundry deployments. Reads and writes go out with the user's own ARM token, so Azure
# decides what they may do; the app identity stays read-only on Foundry accounts.


def _deployer(settings: SettingsDep, discovery: Discovery) -> ModelDeployer:
    # Built per request from the request's own dependencies, so overriding discovery (tests)
    # is enough; it keeps no state of its own.
    return ModelDeployer(settings, discovery)


Deployer = Annotated[ModelDeployer, Depends(_deployer)]


@bound.get("/foundry/accounts")
def foundry_accounts(deployer: Deployer, _: Reader) -> list[FoundryAccountView]:
    return deployer.accounts()


@bound.get("/foundry/models")
def foundry_models(
    request: Request, account_id: str, deployer: Deployer, access: Access, _: Reader
) -> list[DeployableModel]:
    return deployer.models(access.require_user(user_token(request.headers)), account_id)


@bound.get("/foundry/capacity")
def foundry_capacity(
    request: Request,
    account_id: str,
    model: str,
    version: str,
    deployer: Deployer,
    access: Access,
    _: Reader,
) -> ModelCapacity:
    user = access.require_user(user_token(request.headers))
    return deployer.capacity(user, account_id, model, version)


@bound.post("/foundry/deployments")
def create_foundry_deployment(
    request: Request,
    body: FoundryDeploymentCreate,
    deployer: Deployer,
    access: Access,
    principal: Actor,
) -> FoundryDeploymentStatus:
    user = access.require_user(user_token(request.headers))
    return deployer.create(user, body, principal.name)


@bound.get("/foundry/deployments/status")
def foundry_deployment_status(
    request: Request,
    account_id: str,
    deployment: str,
    deployer: Deployer,
    access: Access,
    _: Reader,
) -> FoundryDeploymentStatus:
    user = access.require_user(user_token(request.headers))
    return deployer.status(user, account_id, deployment)


@api.get("/setup")
def setup_report(
    request: Request,
    access: Access,
    settings: SettingsDep,
    principal: Reader,
    refresh: bool = False,
) -> SetupReport:
    if refresh:
        access.forget()
    return access.report(
        user_token(request.headers),
        principal=principal.name,
        owner=is_owner(request, settings),
    )


# Polled while GET /setup runs, which can take a while: each of its steps calls ARM.
@api.get("/setup/progress")
def setup_progress(access: Access, caller: Caller) -> SetupProgress:
    return access.progress(caller)


# Local only: for machines without a local browser. Returns the code; the sign-in completes in
# the background and GET reports when it did.
@api.post("/setup/sign-in/device-code")
def setup_device_code(
    access: Access, _: Actor, body: SignInRequest | None = None
) -> DeviceCodePrompt:
    return access.start_device_code(body.tenant_id if body else None)


@api.get("/setup/sign-in/device-code")
def setup_device_code_status(access: Access, targets: Targets, _: Reader) -> DeviceCodeStatus:
    status = access.device_code_status()
    if status.state == "done":
        targets.forget()
    return status


@api.post("/setup/sign-out", status_code=204)
def setup_sign_out(access: Access, targets: Targets, _: Actor) -> Response:
    access.sign_out()
    targets.forget()
    return Response(status_code=204)


@api.post("/setup/sign-in", status_code=204)
def setup_sign_in(
    access: Access, targets: Targets, _: Actor, body: SignInRequest | None = None
) -> Response:
    access.sign_in(body.tenant_id if body else None)
    targets.forget()
    return Response(status_code=204)


# Read-only: whether the gateway's identity can call these accounts, e.g. before importing them.
@bound.post("/setup/foundry-access")
def setup_foundry_access(
    body: FoundryAccessRequest, request: Request, access: Access, _: Reader
) -> list[SetupCheck]:
    return access.foundry_checks(user_token(request.headers), body.account_ids)


@bound.post("/setup/foundry-access/grant")
def setup_grant_foundry_access(
    body: FoundryAccessRequest, request: Request, access: Access, principal: Actor
) -> list[SetupCheck]:
    return access.grant_foundry_access(
        user_token(request.headers), body.account_ids, principal.name
    )


api.include_router(bound)


# -- choosing, deploying or connecting the gateway (local) --------------------------------


@api.get("/target")
def get_target(targets: Targets, _: Reader) -> TargetView:
    return targets.current()


@api.put("/target")
def select_target(body: TargetSelect, targets: Targets, principal: Actor) -> TargetView:
    return targets.select(body, principal.name)


@api.get("/target/tenants")
def list_tenants(targets: Targets, _: Reader) -> list[TenantView]:
    return targets.tenants()


@api.get("/target/subscriptions")
def list_subscriptions(
    targets: Targets, _: Reader, tenant_id: TenantQuery = None
) -> list[SubscriptionView]:
    return targets.subscriptions(tenant_id)


@api.get("/target/gateways")
def list_gateways(
    targets: Targets, _: Reader, subscription_id: SubscriptionQuery, tenant_id: TenantQuery = None
) -> list[GatewayView]:
    return targets.gateways(tenant_id, subscription_id)


@api.get("/target/create-options")
def create_options(
    targets: Targets, _: Reader, subscription_id: SubscriptionQuery, tenant_id: TenantQuery = None
) -> GatewayCreateOptions:
    return targets.create_options(tenant_id, subscription_id)


# Deploys billable resources with the user's own Azure rights, then selects the new gateway.
@api.post("/target/gateways")
def create_gateway(body: GatewayCreate, targets: Targets, principal: Actor) -> GatewayView:
    return targets.create(body, principal.name)


@api.get("/target/deploy-status")
def deploy_status(
    targets: Targets,
    _: Reader,
    subscription_id: SubscriptionQuery,
    resource_group: str,
    name: str,
    tenant_id: TenantQuery = None,
) -> GatewayDeployStatus:
    return targets.deploy_status(tenant_id, subscription_id, resource_group, name)


# Files that deploy the same gateway on an existing AKS cluster; nothing is written.
@api.get("/target/aks-bundle")
def get_aks_bundle(
    _: Reader,
    subscription_id: SubscriptionQuery,
    resource_group: Annotated[str, Query(pattern=r"^[-\w.()]{0,89}[-\w()]$")],
    cluster: Annotated[str, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")],
    name: Annotated[str, Query(pattern=r"^[a-z](?:[a-z0-9]|-[a-z0-9]){1,30}$")],
    location: Annotated[str, Query(pattern=r"^[a-z0-9]{2,40}$")],
    namespace: Annotated[str, Query(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")] = "litellm",
) -> AksBundle:
    return aks_bundle(
        name=name,
        namespace=namespace,
        cluster=cluster,
        resource_group=resource_group,
        subscription_id=subscription_id,
        location=location,
    )


# Checks an external gateway's URL and master key, then selects it.
@api.post("/target/connect")
def connect_gateway(body: GatewayConnect, targets: Targets, principal: Actor) -> TargetView:
    return targets.connect(body, principal.name)


@api.post("/target/disconnect", status_code=204)
def disconnect_gateway(targets: Targets, principal: Actor) -> Response:
    targets.disconnect(principal.name)
    return Response(status_code=204)


# Read-only: what deleting a gateway this app deployed would remove.
@api.post("/target/gateways/delete-check")
def check_gateway_delete(body: TargetSelect, targets: Targets, _: Actor) -> GatewayDeleteCheck:
    return targets.delete_check(body)


# Deletes a gateway this app deployed, and its resources; the name must be retyped.
@api.post("/target/gateways/delete", status_code=204)
def delete_gateway(body: GatewayDelete, targets: Targets, principal: Actor) -> Response:
    targets.delete(body, principal.name)
    return Response(status_code=204)


# An AKS gateway whose install in the cluster failed: run it again (it is safe to repeat).
@api.post("/target/gateways/retry-install", status_code=204)
def retry_install(body: TargetSelect, targets: Targets, _: Actor) -> Response:
    targets.retry_install(body)
    return Response(status_code=204)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # Fail fast on missing or unsafe configuration.
    if settings.auth_mode == "disabled" and settings.system_trust:
        # Before any HTTPS client exists: ARM (httpx) and sign-in (requests) both pick it up.
        import truststore

        truststore.inject_into_ssl()
    yield


app = FastAPI(title="SteadyApiPool", lifespan=lifespan)
app.include_router(api)


@app.exception_handler(ServiceError)
async def service_error(_: Request, error: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=error.status, content={"detail": error.message})


# MSAL renews sign-ins in a hidden iframe that lands on this page, so only it may be framed, and
# only by this origin.
REDIRECT_PAGE = "/redirect.html"


@lru_cache
def _content_security_policy(authority: str, framed: bool) -> str:
    """Only this origin's scripts (plus the inline first-paint script, by hash), and Entra for
    signing in, so an injected script has nowhere to load from or send tokens to."""
    index = STATIC_DIR / "index.html"
    inline = (
        re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", index.read_text("utf-8"), re.S)
        if index.is_file()
        else []
    )
    hashes = " ".join(
        f"'sha256-{base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()}'"
        for script in inline
    )
    login = authority.rstrip("/")
    ancestors = "'self'" if framed else "'none'"
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' {hashes}".strip(),
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            f"connect-src 'self' {login}",
            f"frame-src 'self' {login}",
            f"frame-ancestors {ancestors}",
            "base-uri 'self'",
            f"form-action 'self' {login}",
            "object-src 'none'",
        ]
    )


@app.middleware("http")
async def no_store(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    framed = request.url.path == REDIRECT_PAGE
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN" if framed else "DENY")
    # As the routes see it (tests override the factory).
    settings: Settings = request.app.dependency_overrides.get(get_settings, get_settings)()
    if settings.public:
        response.headers.setdefault(
            "Content-Security-Policy", _content_security_policy(settings.entra_authority, framed)
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str) -> Response:
    if path.startswith("api/"):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    candidate = (STATIC_DIR / path).resolve()
    if path and candidate.is_file() and STATIC_DIR.resolve() in candidate.parents:
        return FileResponse(candidate)
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return JSONResponse(
            status_code=503, content={"detail": "Frontend not built; run npm run build"}
        )
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
