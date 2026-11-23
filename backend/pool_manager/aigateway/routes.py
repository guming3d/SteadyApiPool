"""AI Gateway's routes, under ``/api/aigateway``: pools (aliases) and members (models), the
pool's endpoint, the test key, the test call and load test, the dashboard, cost, removal, the
identity fix, and choosing, creating and deleting a gateway."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from ..access import user_token
from ..deps import (
    get_aigateway_cost,
    get_aigateway_monitor,
    get_aigateway_service,
    get_aigateway_targets,
    get_aigateway_tester,
)
from ..errors import ServiceError
from ..models import (
    ApiTestResult,
    BenchmarkRun,
    GatewayLoad,
    MetricsRange,
    ModelCheck,
    ModelCheckRequest,
    PoolMetrics,
    SetupCheck,
    TargetView,
)
from ..routes import (
    Access,
    Actor,
    Benchmarks,
    Owner,
    Reader,
    SubscriptionQuery,
    TenantQuery,
    Writer,
    require_product,
)
from .access import AiGatewayAccessChecker
from .api_test import AiGatewayTester
from .cost import AiGatewayCost
from .metrics import AiGatewayMonitor
from .models import (
    ApiKeySecret,
    ApiKeyView,
    ApiTestRequest,
    BackendView,
    BenchmarkStart,
    Capabilities,
    CleanupConfirm,
    GatewayCreate,
    GatewayCreateOptions,
    GatewayDelete,
    GatewayDeleteCheck,
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
)
from .service import AiGatewayService
from .target import AiGatewayTargets

Service = Annotated[AiGatewayService, Depends(get_aigateway_service)]
Tester = Annotated[AiGatewayTester, Depends(get_aigateway_tester)]
Monitor = Annotated[AiGatewayMonitor, Depends(get_aigateway_monitor)]
Cost = Annotated[AiGatewayCost, Depends(get_aigateway_cost)]
Targets = Annotated[AiGatewayTargets, Depends(get_aigateway_targets)]

router = APIRouter(prefix="/aigateway")
# Everything that works on the chosen AI Gateway.
bound = APIRouter(dependencies=[require_product("aigateway")])


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


@bound.delete("/backends/{backend_id}", status_code=204)
def delete_backend(
    backend_id: str, service: Service, principal: Writer, etag: str | None = None
) -> Response:
    service.delete_backend(backend_id, etag, principal.name)
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


@bound.get("/pools/{pool_id}/endpoint")
def pool_endpoint(pool_id: str, service: Service, _: Reader) -> PoolEndpoint:
    return service.endpoint(pool_id)


# Read-only: the gateway's telemetry of the pool's alias and its Foundry members' metrics.
@bound.get("/pools/{pool_id}/metrics")
def pool_metrics(
    pool_id: str,
    monitor: Monitor,
    _: Reader,
    range: MetricsRange = "1h",  # noqa: A002 - the query parameter's name
    refresh: bool = False,
) -> PoolMetrics:
    return monitor.get(pool_id, range, refresh=refresh)


# Read-only: all of the gateway's traffic.
@bound.get("/metrics")
def gateway_metrics(
    monitor: Monitor,
    _: Reader,
    range: MetricsRange = "1h",  # noqa: A002 - the query parameter's name
    refresh: bool = False,
) -> GatewayLoad:
    return monitor.load(range, refresh=refresh)


# Read-only: what removing the pool takes with it (its unused members).
@bound.get("/pools/{pool_id}/removal")
def pool_removal_plan(pool_id: str, service: Service, _: Reader) -> RemovalPlan:
    return service.pool_removal_plan(pool_id)


@bound.post("/pools/{pool_id}/remove")
def remove_pool(
    pool_id: str, body: RemovalConfirm, service: Service, principal: Writer
) -> RemovalResult:
    return service.remove_pool(pool_id, body, principal.name)


# Read-only: this month's spend (with the user's own Azure rights) and the request price.
@bound.get("/cost")
def service_cost(
    request: Request, cost: Cost, access: Access, principal: Reader, refresh: bool = False
) -> ServiceCost:
    user = access.user_arm(user_token(request.headers))
    return cost.get(user, principal.name, refresh=refresh)


# Read-only: everything this app created in the gateway, which cleanup would remove.
@bound.get("/cleanup")
def cleanup_plan(service: Service, _: Reader) -> RemovalPlan:
    return service.cleanup_plan()


@bound.post("/cleanup")
def cleanup(body: CleanupConfirm, service: Service, principal: Writer) -> RemovalResult:
    return service.cleanup(body, principal.name)


# -- trying a pool -------------------------------------------------------------------------
# Keys are listed, created and read with the user's own Azure rights (Easy Auth's token when
# deployed). Every key opens every pool on the gateway.


@bound.get("/keys")
def list_keys(request: Request, access: Access, tester: Tester, _: Reader) -> list[ApiKeyView]:
    return tester.keys(access.require_user(user_token(request.headers)))


@bound.post("/keys")
def create_key(request: Request, access: Access, tester: Tester, principal: Actor) -> ApiKeyView:
    user = access.require_user(user_token(request.headers))
    return tester.create_key(user, principal.name)


# POST so the key is never cached or prefetched, and needs the CSRF header.
@bound.post("/keys/{key_id}/secret")
def read_key(
    key_id: str, request: Request, access: Access, tester: Tester, principal: Actor
) -> ApiKeySecret:
    user = access.require_user(user_token(request.headers))
    return tester.secret(user, key_id, principal.name)


@bound.post("/pools/{pool_id}/test")
def test_pool(
    pool_id: str, body: ApiTestRequest, tester: Tester, principal: Actor
) -> ApiTestResult:
    return tester.call(pool_id, body, principal.name)


# A load test sends real, billed traffic through the pool, so only owners start one.
@bound.post("/pools/{pool_id}/benchmarks")
def start_benchmark(
    pool_id: str,
    body: BenchmarkStart,
    tester: Tester,
    benchmarks: Benchmarks,
    principal: Writer,
) -> BenchmarkRun:
    target = tester.benchmark_target(pool_id, body.spec.model, body.key.get_secret_value())
    return benchmarks.start(pool_id, body.spec, target, principal.name)


@bound.get("/pools/{pool_id}/benchmarks/{run_id}")
def get_benchmark(pool_id: str, run_id: str, benchmarks: Benchmarks, _: Owner) -> BenchmarkRun:
    return benchmarks.get(pool_id, run_id)


@bound.post("/pools/{pool_id}/benchmarks/{run_id}/stop")
def stop_benchmark(pool_id: str, run_id: str, benchmarks: Benchmarks, _: Writer) -> BenchmarkRun:
    return benchmarks.stop(pool_id, run_id)


@bound.post("/setup/identity")
def setup_identity(request: Request, access: Access, principal: Actor) -> SetupCheck:
    if not isinstance(access, AiGatewayAccessChecker):
        raise ServiceError(409, "The chosen gateway isn't an AI Gateway")
    return access.enable_identity(user_token(request.headers), principal.name)


router.include_router(bound)


# -- choosing, creating or deleting the gateway (local setup) --------------------------------


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


# Creates a billable resource with the user's own Azure rights, then selects the new gateway.
@router.post("/target/gateways")
def create_gateway(body: GatewayCreate, targets: Targets, principal: Actor) -> GatewayView:
    return targets.create(body, principal.name)


# Read-only: what deleting a gateway this app created would remove.
@router.post("/target/gateways/delete-check")
def check_gateway_delete(body: TargetSelect, targets: Targets, _: Actor) -> GatewayDeleteCheck:
    return targets.delete_check(body)


@router.post("/target/gateways/delete", status_code=204)
def delete_gateway(body: GatewayDelete, targets: Targets, principal: Actor) -> Response:
    targets.delete(body, principal.name)
    return Response(status_code=204)
