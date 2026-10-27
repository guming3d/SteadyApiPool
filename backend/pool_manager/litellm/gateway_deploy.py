"""Deploying a LiteLLM gateway on Container Apps, with the signed-in user's own Azure rights.

One ARM deployment (``ai-gateway-<name>``) of the compiled template in templates/, into a
resource group that is created when missing (tagged ``managed-by``) and never changed when it
exists. Resource providers the template needs are registered first. A gateway that exists, or
whose deployment is running, is never deployed over: a new deployment would bring new secrets,
and the salt key that encrypts stored member keys must not change.

Before anything is created, the region is checked for PostgreSQL Flexible Server, which many
subscriptions may not create in every region: a region Azure would refuse is refused here, with
the regions that are open, instead of failing the deployment halfway.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..arm import ArmClient, ArmError, segment
from ..config import Settings
from ..errors import ServiceError
from ..models import DeployStep, GatewayCreate, GatewayDeployStatus
from .gateway import GATEWAY_TAG, MANAGED_TAG, MANAGED_TAG_VALUE
from .gateway_template import aca_parameters, aca_template

logger = logging.getLogger(__name__)

RESOURCES_API_VERSION = "2021-04-01"
DEPLOYMENTS_API_VERSION = "2024-03-01"
# What the template creates; Microsoft.Cache only for the production profile.
PROVIDERS = (
    "Microsoft.App",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights",
    "Microsoft.ManagedIdentity",
    "Microsoft.DBforPostgreSQL",
)
REDIS_PROVIDER = "Microsoft.Cache"
POSTGRES_API_VERSION = "2024-08-01"
# The server version in infra/gateway/litellm-aca.bicep.
POSTGRES_VERSION = "16"
# Offered instead when the chosen region can't take the gateway's database.
ALTERNATIVE_REGIONS = (
    "eastus2",
    "swedencentral",
    "westus3",
    "centralus",
    "westeurope",
    "northeurope",
    "uksouth",
    "australiaeast",
    "japaneast",
    "koreacentral",
)
_REGISTER_POLLS = 24
_REGISTER_POLL_SECONDS = 5.0


def deployment_name(gateway: str) -> str:
    return f"ai-gateway-{gateway}"


def translate(error: ArmError, action: str) -> ServiceError:
    if error.status == 401:
        return ServiceError(401, f"Sign in again to {action}: {error.message}")
    if error.status == 403:
        return ServiceError(403, f"Your Azure account cannot {action}: {error.message}")
    if error.status in {400, 404, 409}:
        return ServiceError(error.status, error.message)
    return ServiceError(502, f"Azure returned {error.status} {error.code}: {error.message}")


def group_path(subscription_id: str, resource_group: str) -> str:
    return f"/subscriptions/{segment(subscription_id)}/resourceGroups/{segment(resource_group)}"


def app_path(subscription_id: str, resource_group: str, name: str) -> str:
    return (
        f"{group_path(subscription_id, resource_group)}"
        f"/providers/Microsoft.App/containerApps/{segment(name)}"
    )


def _deployment_path(subscription_id: str, resource_group: str, name: str) -> str:
    return (
        f"{group_path(subscription_id, resource_group)}"
        f"/providers/Microsoft.Resources/deployments/{segment(deployment_name(name))}"
    )


def _error_message(error: Any) -> str | None:
    """The innermost useful message of an ARM error object."""
    if not isinstance(error, dict):
        return None
    for detail in error.get("details") or []:
        if found := _error_message(detail):
            return found
    message = str(error.get("message") or "")
    code = str(error.get("code") or "")
    return f"{code}: {message}" if code and message else message or code or None


def deployment_path(subscription_id: str, resource_group: str, name: str) -> str:
    return _deployment_path(subscription_id, resource_group, name)


def _severity(state: str) -> int:
    state = state.casefold()
    return 2 if state in {"failed", "canceled"} else 0 if state == "succeeded" else 1


class GatewayDeployer:
    def __init__(self, settings: Settings, sleep: Callable[[float], None] = time.sleep) -> None:
        self._settings = settings
        self._sleep = sleep

    def postgres_blocked(self, arm: ArmClient, subscription_id: str, location: str) -> str | None:
        """Why the subscription can't create the gateway's database in ``location``, if it can't.

        Read from the region's PostgreSQL capabilities. When they can't be read the deployment
        goes ahead, and Azure reports what is wrong.
        """
        try:
            found = arm.get_arm_json(
                f"/subscriptions/{segment(subscription_id)}/providers/Microsoft.DBforPostgreSQL"
                f"/locations/{segment(location)}/capabilities",
                api_version=POSTGRES_API_VERSION,
            )
        except ArmError as error:
            logger.info("Cannot read PostgreSQL capabilities in %s: %s", location, error.message)
            return None
        capabilities = (found or {}).get("value") or []
        if not capabilities:
            return None
        capability = capabilities[0] or {}
        versions = {
            str(item.get("name")) for item in capability.get("supportedServerVersions") or []
        }
        if str(capability.get("restricted") or "").casefold() == "enabled" or (
            POSTGRES_VERSION not in versions
        ):
            reason = str(capability.get("reason") or "").strip()
            return reason or f"PostgreSQL {POSTGRES_VERSION} isn't offered there"
        return None

    def _leftovers(
        self, arm: ArmClient, subscription_id: str, resource_group: str, name: str
    ) -> list[str]:
        """Resources still tagged as a gateway of this name, e.g. an environment Azure is still
        deleting: a deployment would collide with them."""
        group = group_path(subscription_id, resource_group)
        if arm.get_arm_json(group, api_version=RESOURCES_API_VERSION) is None:
            return []
        return [
            str(item.get("name"))
            for item in arm.list_arm(
                f"{group}/resources",
                api_version=RESOURCES_API_VERSION,
                params={"$filter": f"tagName eq '{GATEWAY_TAG}' and tagValue eq '{name}'"},
            )
        ]

    def open_regions(self, arm: ArmClient, subscription_id: str, exclude: str) -> list[str]:
        candidates = [r for r in ALTERNATIVE_REGIONS if r.casefold() != exclude.casefold()]
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            blocked = list(
                pool.map(lambda r: self.postgres_blocked(arm, subscription_id, r), candidates)
            )
        return [region for region, reason in zip(candidates, blocked, strict=True) if not reason]

    def unregistered(
        self,
        arm: ArmClient,
        subscription_id: str,
        *,
        redis: bool,
        providers: tuple[str, ...] = PROVIDERS,
    ) -> list[str]:
        return self._unregistered_of(
            arm, subscription_id, [*providers, *([REDIS_PROVIDER] if redis else [])]
        )

    def _unregistered_of(
        self, arm: ArmClient, subscription_id: str, providers: list[str]
    ) -> list[str]:
        missing = []
        for provider in providers:
            found = arm.get_arm_json(
                f"/subscriptions/{segment(subscription_id)}/providers/{provider}",
                api_version=RESOURCES_API_VERSION,
            )
            if str((found or {}).get("registrationState") or "").casefold() != "registered":
                missing.append(provider)
        return missing

    def _register(self, arm: ArmClient, subscription_id: str, providers: list[str]) -> None:
        subscription = f"/subscriptions/{segment(subscription_id)}"
        for provider in providers:
            arm.request(
                "POST",
                arm.arm_url(f"{subscription}/providers/{provider}/register"),
                api_version=RESOURCES_API_VERSION,
            )
        for _ in range(_REGISTER_POLLS):
            if not self._unregistered_of(arm, subscription_id, providers):
                return
            self._sleep(_REGISTER_POLL_SECONDS)
        raise ServiceError(
            503, f"{', '.join(providers)} are still being registered; try again shortly"
        )

    def start(self, arm: ArmClient, body: GatewayCreate, actor: str) -> None:
        """Starts the deployment; ``status`` follows it."""
        try:
            self.check(
                arm,
                body,
                existing=app_path(body.subscription_id, body.resource_group, body.name),
                existing_api=self._settings.container_apps_api_version,
            )
            self.provision(arm, body, actor, providers=PROVIDERS)
            self.put_deployment(
                arm,
                body,
                template=aca_template(),
                parameters=aca_parameters(body.name, body.location, body.profile),
                tags={MANAGED_TAG: MANAGED_TAG_VALUE},
            )
        except ArmError as error:
            raise translate(error, "deploy this gateway") from error
        logger.info(
            "%s started deploying gateway %s (%s) in %s",
            actor,
            body.name,
            body.profile,
            group_path(body.subscription_id, body.resource_group),
        )

    def check(
        self, arm: ArmClient, body: GatewayCreate, *, existing: str, existing_api: str
    ) -> None:
        """Refuses what a deployment would collide with or Azure would refuse halfway: a gateway
        of that name, its deployment still running, leftovers, a region without PostgreSQL."""
        if arm.get_arm_json(existing, api_version=existing_api):
            raise ServiceError(409, f"{body.name} already exists; choose it in the list")
        running = self.status(arm, body.subscription_id, body.resource_group, body.name)
        if running.state in {"running", "succeeded"}:
            raise ServiceError(409, f"{body.name} is already being deployed")
        if leftovers := self._leftovers(arm, body.subscription_id, body.resource_group, body.name):
            raise ServiceError(
                409,
                f"Resources of an earlier {body.name} are still in {body.resource_group} "
                f"({', '.join(leftovers)}), perhaps still being deleted. Choose another "
                "name, or wait until they are gone.",
            )
        if reason := self.postgres_blocked(arm, body.subscription_id, body.location):
            regions = self.open_regions(arm, body.subscription_id, body.location)
            hint = (
                f" Regions open to this subscription: {', '.join(regions[:5])}." if regions else ""
            )
            raise ServiceError(
                409,
                f"The gateway's database (PostgreSQL Flexible Server) can't be created in "
                f"{body.location} with this subscription: {reason}{hint}",
            )

    def provision(
        self, arm: ArmClient, body: GatewayCreate, actor: str, *, providers: tuple[str, ...]
    ) -> None:
        """Registers the resource providers the template needs and creates the resource group
        when it is missing (tagged as this app's); an existing group is never changed."""
        missing = self.unregistered(
            arm, body.subscription_id, redis=body.profile == "production", providers=providers
        )
        if missing:
            logger.info("%s registers %s", actor, ", ".join(missing))
            self._register(arm, body.subscription_id, missing)
        group = group_path(body.subscription_id, body.resource_group)
        if arm.get_arm_json(group, api_version=RESOURCES_API_VERSION) is None:
            arm.request(
                "PUT",
                arm.arm_url(group),
                api_version=RESOURCES_API_VERSION,
                json_body={"location": body.location, "tags": {MANAGED_TAG: MANAGED_TAG_VALUE}},
            )
            logger.info("%s created resource group %s", actor, group)

    def put_deployment(
        self,
        arm: ArmClient,
        body: GatewayCreate,
        *,
        template: dict[str, Any],
        parameters: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        arm.request(
            "PUT",
            arm.arm_url(_deployment_path(body.subscription_id, body.resource_group, body.name)),
            api_version=DEPLOYMENTS_API_VERSION,
            json_body={
                "properties": {
                    "mode": "Incremental",
                    "template": template,
                    "parameters": parameters,
                },
                "tags": tags,
            },
        )

    def status(
        self, arm: ArmClient, subscription_id: str, resource_group: str, name: str
    ) -> GatewayDeployStatus:
        path = _deployment_path(subscription_id, resource_group, name)
        try:
            deployment = arm.get_arm_json(path, api_version=DEPLOYMENTS_API_VERSION)
            if deployment is None:
                return GatewayDeployStatus(
                    name=name, state="none", started_at=None, steps=[], error=None
                )
            operations = list(
                arm.list_arm(f"{path}/operations", api_version=DEPLOYMENTS_API_VERSION)
            )
        except ArmError as error:
            raise translate(error, "read the gateway's deployment") from error
        properties = deployment.get("properties") or {}
        state = str(properties.get("provisioningState") or "").casefold()
        # ARM lists an operation per attempt and per child resource; show each resource once,
        # in its worst state.
        steps: dict[tuple[str, str], DeployStep] = {}
        for operation in operations:
            details = operation.get("properties") or {}
            target = details.get("targetResource") or {}
            if not target.get("resourceType"):
                continue
            step = DeployStep(
                resource_type=str(target.get("resourceType")),
                name=str(target.get("resourceName") or ""),
                state=str(details.get("provisioningState") or ""),
                error=_error_message((details.get("statusMessage") or {}).get("error")),
            )
            key = (step.resource_type.casefold(), step.name.casefold())
            seen = steps.get(key)
            if seen is None or _severity(step.state) > _severity(seen.state):
                steps[key] = step
        return GatewayDeployStatus(
            name=name,
            state=(
                "succeeded"
                if state == "succeeded"
                else "failed"
                if state in {"failed", "canceled"}
                else "running"
            ),
            started_at=(deployment.get("systemData") or {}).get("createdAt")
            or properties.get("timestamp"),
            steps=sorted(steps.values(), key=lambda step: (step.resource_type, step.name)),
            error=_error_message(properties.get("error")),
        )
