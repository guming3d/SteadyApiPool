"""The routes both gateways share, under ``/api``: sign-in and setup, the chosen gateway,
Foundry deployments and model deployment.

Each product's own routes are under ``/api/apim`` (apim/routes.py) and ``/api/litellm``
(litellm/routes.py). Routes that work on a gateway sit on a ``bound`` router, which answers 409
until one (of the right product) is chosen.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response

from .access import AccessChecker, user_token
from .auth import acting_user, caller_name, current_principal, is_owner, owner, writer
from .benchmark import BenchmarkRunner
from .config import Settings, get_settings
from .deps import (
    User,
    get_access,
    get_benchmarks,
    get_discovery,
    get_request_settings,
    get_sessions,
    get_targets,
)
from .discovery import DeploymentDiscovery
from .entra import API_SCOPE_NAME, ARM_USER_SCOPE
from .errors import ServiceError
from .model_deploy import ModelDeployer
from .models import (
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
    ModelCapacity,
    Principal,
    Product,
    PublicConfig,
    SetupCheck,
    SetupProgress,
    SetupReport,
    SignInRequest,
    SubscriptionView,
    TargetView,
    TenantView,
)
from .sessions import Sessions
from .target import TargetChooser

Discovery = Annotated[DeploymentDiscovery, Depends(get_discovery)]
Access = Annotated[AccessChecker, Depends(get_access)]
Targets = Annotated[TargetChooser, Depends(get_targets)]
# Load tests, one at a time (per session in the public app).
Benchmarks = Annotated[BenchmarkRunner, Depends(get_benchmarks)]
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

PRODUCT_NAMES: dict[Product, str] = {"apim": "an APIM service", "litellm": "a LiteLLM gateway"}


def require_target(settings: SettingsDep) -> None:
    if not settings.has_target:
        raise ServiceError(409, "No gateway is chosen yet; choose, create or connect one in setup")


def require_product(product: Product) -> Any:
    """A dependency: a gateway of ``product`` is chosen."""

    def check(settings: SettingsDep) -> None:
        require_target(settings)
        if settings.product != product:
            raise ServiceError(
                409, f"The chosen gateway isn't {PRODUCT_NAMES[product]}; choose one in setup"
            )

    return Depends(check)


router = APIRouter()
# Shared routes that work on the chosen gateway, whichever product it is.
bound = APIRouter(dependencies=[Depends(require_target)])


# Unauthenticated: what the browser needs to sign in (the public app) or that it needn't.
@router.get("/config")
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


@router.get("/me")
def me(principal: Reader) -> Principal:
    return principal


# The public app: forgets the caller's sessions and Azure tokens. The browser then signs out of
# Entra too.
@router.post("/session/sign-out", status_code=204)
def session_sign_out(
    user: User,
    sessions: Annotated[Sessions | None, Depends(get_sessions)],
    _: Actor,
) -> Response:
    if user is None or sessions is None:
        raise ServiceError(400, "Only the public app has sessions to sign out of")
    sessions.drop(user.key)
    return Response(status_code=204)


# -- Foundry deployments ---------------------------------------------------------------------


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


# -- setup -----------------------------------------------------------------------------------


@router.get("/setup")
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
@router.get("/setup/progress")
def setup_progress(access: Access, caller: Caller) -> SetupProgress:
    return access.progress(caller)


# Local only: for machines without a local browser. Returns the code; the sign-in completes in
# the background and GET reports when it did.
@router.post("/setup/sign-in/device-code")
def setup_device_code(
    access: Access, _: Actor, body: SignInRequest | None = None
) -> DeviceCodePrompt:
    return access.start_device_code(body.tenant_id if body else None)


@router.get("/setup/sign-in/device-code")
def setup_device_code_status(access: Access, targets: Targets, _: Reader) -> DeviceCodeStatus:
    status = access.device_code_status()
    if status.state == "done":
        targets.forget()
    return status


@router.post("/setup/sign-out", status_code=204)
def setup_sign_out(access: Access, targets: Targets, _: Actor) -> Response:
    access.sign_out()
    targets.forget()
    return Response(status_code=204)


@router.post("/setup/sign-in", status_code=204)
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


# -- the chosen gateway (the products list, create and select theirs) ------------------------


@router.get("/target")
def get_target(targets: Targets, _: Reader) -> TargetView:
    return targets.current()


@router.get("/target/tenants")
def list_tenants(targets: Targets, _: Reader) -> list[TenantView]:
    return targets.tenants()


@router.get("/target/subscriptions")
def list_subscriptions(
    targets: Targets, _: Reader, tenant_id: TenantQuery = None
) -> list[SubscriptionView]:
    return targets.subscriptions(tenant_id)


router.include_router(bound)
