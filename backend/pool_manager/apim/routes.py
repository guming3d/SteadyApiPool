"""APIM's routes, under ``/api/apim``: pools and members (backends), APIs and their policy
binding, subscription keys, the test call and load test, the dashboard, cost, removal, the
identity fix, and choosing, creating, deleting and restoring a service."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from ..access import user_token
from ..deps import (
    get_apim_cost,
    get_apim_monitor,
    get_apim_service,
    get_apim_targets,
    get_apim_tester,
)
from ..errors import ServiceError
from ..models import (
    ApiTestResult,
    BenchmarkRun,
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
from .access import ApimAccessChecker
from .api_test import ApimTester
from .cost import ApimCost
from .metrics import ApimMonitor
from .models import (
    ApiBindingWrite,
    ApimCreate,
    ApimCreateOptions,
    ApimServiceView,
    ApiTestRequest,
    ApiView,
    BackendView,
    BenchmarkStart,
    Capabilities,
    CleanupConfirm,
    DeletedServiceView,
    MemberBackendWrite,
    PolicyPreview,
    PolicyView,
    PoolApiCreate,
    PoolView,
    PoolWrite,
    RemovalConfirm,
    RemovalPlan,
    RemovalResult,
    ServiceCost,
    ServiceDelete,
    ServiceDeleteCheck,
    ServiceRestore,
    SubscriptionKeyView,
    SubscriptionSecret,
    TargetSelect,
)
from .service import ApimService
from .target import ApimTargets

Service = Annotated[ApimService, Depends(get_apim_service)]
Tester = Annotated[ApimTester, Depends(get_apim_tester)]
Monitor = Annotated[ApimMonitor, Depends(get_apim_monitor)]
Cost = Annotated[ApimCost, Depends(get_apim_cost)]
Targets = Annotated[ApimTargets, Depends(get_apim_targets)]

router = APIRouter(prefix="/apim")
# Everything that works on the chosen APIM service.
bound = APIRouter(dependencies=[require_product("apim")])


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


# Read-only Azure Monitor metrics for the pool's APIs and its Foundry members.
@bound.get("/pools/{pool_id}/metrics")
def pool_metrics(
    pool_id: str,
    monitor: Monitor,
    _: Reader,
    range: MetricsRange = "1h",  # noqa: A002 - the query parameter's name
    refresh: bool = False,
) -> PoolMetrics:
    return monitor.get(pool_id, range, refresh=refresh)


@bound.post("/pools/{pool_id}/api")
def create_pool_api(
    pool_id: str, body: PoolApiCreate, service: Service, principal: Writer
) -> ApiView:
    return service.create_pool_api(pool_id, body, principal.name)


@bound.delete("/pools/{pool_id}", status_code=204)
def delete_pool(
    pool_id: str, service: Service, principal: Writer, etag: str | None = None
) -> Response:
    service.delete_pool(pool_id, etag, principal.name)
    return Response(status_code=204)


# Read-only: what removing the pool takes with it (its APIs, unused members).
@bound.get("/pools/{pool_id}/removal")
def pool_removal_plan(pool_id: str, service: Service, _: Reader) -> RemovalPlan:
    return service.pool_removal_plan(pool_id)


@bound.post("/pools/{pool_id}/remove")
def remove_pool(
    pool_id: str, body: RemovalConfirm, service: Service, principal: Writer
) -> RemovalResult:
    return service.remove_pool(pool_id, body, principal.name)


# Read-only: this month's spend (with the user's own Azure rights) and the tier's list price.
@bound.get("/cost")
def service_cost(
    request: Request, cost: Cost, access: Access, principal: Reader, refresh: bool = False
) -> ServiceCost:
    user = access.user_arm(user_token(request.headers))
    return cost.get(user, principal.name, refresh=refresh)


# Read-only: everything this app created in the service, which cleanup would remove.
@bound.get("/cleanup")
def cleanup_plan(service: Service, _: Reader) -> RemovalPlan:
    return service.cleanup_plan()


@bound.post("/cleanup")
def cleanup(body: CleanupConfirm, service: Service, principal: Writer) -> RemovalResult:
    return service.cleanup(body, principal.name)


@bound.get("/apis")
def list_apis(service: Service, _: Reader) -> list[ApiView]:
    return service.list_apis()


@bound.get("/apis/{api_id}/policy")
def get_policy(api_id: str, service: Service, _: Reader) -> PolicyView:
    return service.get_policy(api_id)


# Read-only: computes the policy a binding would write so it can be reviewed or exported first.
@bound.post("/apis/{api_id}/binding/preview")
def preview_binding(
    api_id: str, body: ApiBindingWrite, service: Service, _: Reader
) -> PolicyPreview:
    return service.preview_binding(api_id, body)


@bound.put("/apis/{api_id}/binding")
def put_binding(api_id: str, body: ApiBindingWrite, service: Service, principal: Writer) -> ApiView:
    return service.put_binding(api_id, body, principal.name)


@bound.delete("/apis/{api_id}/binding", status_code=204)
def delete_binding(
    api_id: str, service: Service, principal: Writer, etag: str | None = None
) -> Response:
    service.delete_binding(api_id, etag, principal.name)
    return Response(status_code=204)


# -- trying a pool API ---------------------------------------------------------------------
# Keys are read and created with the user's own Azure rights (Easy Auth's token when deployed).


@bound.get("/apis/{api_id}/keys")
def list_keys(
    api_id: str, request: Request, access: Access, tester: Tester, _: Reader
) -> list[SubscriptionKeyView]:
    return tester.keys(access.require_user(user_token(request.headers)), api_id)


@bound.post("/apis/{api_id}/keys")
def create_key(
    api_id: str, request: Request, access: Access, tester: Tester, principal: Actor
) -> SubscriptionKeyView:
    user = access.require_user(user_token(request.headers))
    return tester.create_key(user, api_id, principal.name)


# POST so the key is never cached or prefetched, and needs the CSRF header.
@bound.post("/apis/{api_id}/keys/{key_id}/secret")
def read_key(
    api_id: str, key_id: str, request: Request, access: Access, tester: Tester, principal: Actor
) -> SubscriptionSecret:
    user = access.require_user(user_token(request.headers))
    return tester.secret(user, api_id, key_id, principal.name)


@bound.post("/apis/{api_id}/test")
def test_api(api_id: str, body: ApiTestRequest, tester: Tester, principal: Actor) -> ApiTestResult:
    return tester.call(api_id, body, principal.name)


# A load test sends real, billed traffic through the API, so only owners start one.
@bound.post("/apis/{api_id}/benchmarks")
def start_benchmark(
    api_id: str,
    body: BenchmarkStart,
    tester: Tester,
    benchmarks: Benchmarks,
    principal: Writer,
) -> BenchmarkRun:
    key = body.subscription_key.get_secret_value() if body.subscription_key else None
    pool_id, target = tester.benchmark_target(api_id, body.spec.model, body.api_version, key)
    return benchmarks.start(pool_id, body.spec, target, principal.name, api_id=api_id)


@bound.get("/apis/{api_id}/benchmarks/{run_id}")
def get_benchmark(api_id: str, run_id: str, benchmarks: Benchmarks, _: Owner) -> BenchmarkRun:
    return benchmarks.get(None, run_id, api_id=api_id)


@bound.post("/apis/{api_id}/benchmarks/{run_id}/stop")
def stop_benchmark(api_id: str, run_id: str, benchmarks: Benchmarks, _: Writer) -> BenchmarkRun:
    return benchmarks.stop(None, run_id, api_id=api_id)


@bound.post("/setup/apim-identity")
def setup_apim_identity(request: Request, access: Access, principal: Actor) -> SetupCheck:
    if not isinstance(access, ApimAccessChecker):
        raise ServiceError(409, "The chosen gateway isn't an APIM service")
    return access.enable_apim_identity(user_token(request.headers), principal.name)


router.include_router(bound)


# -- choosing or creating the APIM service (local, and the public app) ------------------------


@router.put("/target")
def select_target(body: TargetSelect, targets: Targets, principal: Actor) -> TargetView:
    return targets.select(body, principal.name)


@router.get("/target/services")
def list_services(
    targets: Targets, _: Reader, subscription_id: SubscriptionQuery, tenant_id: TenantQuery = None
) -> list[ApimServiceView]:
    return targets.services(tenant_id, subscription_id)


@router.get("/target/create-options")
def create_options(
    targets: Targets, _: Reader, subscription_id: SubscriptionQuery, tenant_id: TenantQuery = None
) -> ApimCreateOptions:
    return targets.create_options(tenant_id, subscription_id)


# Creates billable resources with the user's own Azure rights, then selects the new service.
@router.post("/target/services")
def create_service(body: ApimCreate, targets: Targets, principal: Actor) -> ApimServiceView:
    return targets.create(body, principal.name)


# Read-only: what deleting a service this app created would remove.
@router.post("/target/services/delete-check")
def check_service_delete(body: TargetSelect, targets: Targets, _: Actor) -> ServiceDeleteCheck:
    return targets.delete_check(body)


# Soft-deletes a service this app created (Azure keeps it 48 hours); the name must be retyped.
@router.post("/target/services/delete", status_code=204)
def delete_service(body: ServiceDelete, targets: Targets, principal: Actor) -> Response:
    targets.delete(body, principal.name)
    return Response(status_code=204)


@router.get("/target/deleted")
def list_deleted_services(
    targets: Targets, _: Reader, subscription_id: SubscriptionQuery, tenant_id: TenantQuery = None
) -> list[DeletedServiceView]:
    return targets.deleted_services(tenant_id, subscription_id)


@router.post("/target/deleted/restore")
def restore_service(body: ServiceRestore, targets: Targets, principal: Actor) -> ApimServiceView:
    return targets.restore(body, principal.name)
