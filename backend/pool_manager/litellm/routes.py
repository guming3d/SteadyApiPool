"""LiteLLM's routes, under ``/api/litellm``: pools and members, aliases, the config view, virtual
keys, the test call and load test, the dashboard, cost, removal, and choosing, deploying,
connecting and deleting a gateway."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from ..access import user_token
from ..deps import (
    get_benchmarks,
    get_litellm_cost,
    get_litellm_monitor,
    get_litellm_service,
    get_litellm_targets,
    get_litellm_tester,
)
from ..models import (
    ApiTestResult,
    MetricsRange,
    ModelCheck,
    ModelCheckRequest,
    PoolMetrics,
    TargetView,
)
from ..routes import (
    Access,
    Actor,
    Owner,
    Reader,
    SubscriptionQuery,
    TenantQuery,
    Writer,
    require_product,
)
from .api_test import LiteLLMTester
from .benchmark import BenchmarkRunner
from .cost import LiteLLMCost
from .gateway_template import aks_bundle
from .metrics import LiteLLMMonitor
from .models import (
    AksBundle,
    AliasView,
    AliasWrite,
    ApiTestRequest,
    BackendView,
    BenchmarkRun,
    BenchmarkStart,
    Capabilities,
    CleanupConfirm,
    ConfigPreview,
    ConfigView,
    GatewayConnect,
    GatewayCreate,
    GatewayCreateOptions,
    GatewayDelete,
    GatewayDeleteCheck,
    GatewayDeployStatus,
    GatewayPreflight,
    GatewayView,
    MemberBackendWrite,
    PoolEndpoint,
    PoolView,
    PoolWrite,
    RemovalConfirm,
    RemovalPlan,
    RemovalResult,
    ServiceCost,
    TargetSelect,
    VirtualKeySecret,
    VirtualKeyView,
)
from .service import LiteLLMService
from .target import LiteLLMTargets

Service = Annotated[LiteLLMService, Depends(get_litellm_service)]
Tester = Annotated[LiteLLMTester, Depends(get_litellm_tester)]
Benchmarks = Annotated[BenchmarkRunner, Depends(get_benchmarks)]
Monitor = Annotated[LiteLLMMonitor, Depends(get_litellm_monitor)]
Cost = Annotated[LiteLLMCost, Depends(get_litellm_cost)]
Targets = Annotated[LiteLLMTargets, Depends(get_litellm_targets)]

router = APIRouter(prefix="/litellm")
# Everything that works on the chosen LiteLLM gateway.
bound = APIRouter(dependencies=[require_product("litellm")])


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


router.include_router(bound)


# -- choosing, deploying or connecting a gateway (local) --------------------------------------


@router.put("/target")
def select_target(body: TargetSelect, targets: Targets, principal: Actor) -> TargetView:
    return targets.select(body, principal.name)


@router.get("/target/gateways")
def list_gateways(
    targets: Targets, _: Reader, subscription_id: SubscriptionQuery, tenant_id: TenantQuery = None
) -> list[GatewayView]:
    return targets.gateways(tenant_id, subscription_id)


@router.get("/target/create-options")
def create_options(
    targets: Targets, _: Reader, subscription_id: SubscriptionQuery, tenant_id: TenantQuery = None
) -> GatewayCreateOptions:
    return targets.create_options(tenant_id, subscription_id)


# Deploys billable resources with the user's own Azure rights, then selects the new gateway.
@router.post("/target/gateways")
def create_gateway(body: GatewayCreate, targets: Targets, principal: Actor) -> GatewayView:
    return targets.create(body, principal.name)


@router.post("/target/gateways/preflight")
def gateway_preflight(body: GatewayCreate, targets: Targets, _: Reader) -> GatewayPreflight:
    """Read-only: what would keep a deployment there from succeeding."""
    return targets.preflight(body)


@router.get("/target/deploy-status")
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
@router.get("/target/aks-bundle")
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
@router.post("/target/connect")
def connect_gateway(body: GatewayConnect, targets: Targets, principal: Actor) -> TargetView:
    return targets.connect(body, principal.name)


@router.post("/target/disconnect", status_code=204)
def disconnect_gateway(targets: Targets, principal: Actor) -> Response:
    targets.disconnect(principal.name)
    return Response(status_code=204)


# Read-only: what deleting a gateway this app deployed would remove.
@router.post("/target/gateways/delete-check")
def check_gateway_delete(body: TargetSelect, targets: Targets, _: Actor) -> GatewayDeleteCheck:
    return targets.delete_check(body)


# Deletes a gateway this app deployed, and its resources; the name must be retyped.
@router.post("/target/gateways/delete", status_code=204)
def delete_gateway(body: GatewayDelete, targets: Targets, principal: Actor) -> Response:
    targets.delete(body, principal.name)
    return Response(status_code=204)


# An AKS gateway whose install in the cluster failed: run it again (it is safe to repeat).
@router.post("/target/gateways/retry-install", status_code=204)
def retry_install(body: TargetSelect, targets: Targets, _: Actor) -> Response:
    targets.retry_install(body)
    return Response(status_code=204)
