"""Deploying a LiteLLM gateway on Container Apps, with the signed-in user's own Azure rights.

One ARM deployment (``ai-gateway-<name>``) of the compiled template in templates/, into a
resource group that is created when missing (tagged ``managed-by``) and never changed when it
exists. Resource providers the template needs are registered first. A gateway that exists, or
whose deployment is running, is never deployed over: a new deployment would bring new secrets,
and the salt key that encrypts stored member keys must not change.

Nothing is created before two checks pass, so a region that can't take the gateway doesn't leave
half of it behind:

1. ``require_capacity``: what the region offers and the subscription's quotas there, for every
   resource the template creates (capacity.py), all problems at once, with regions that fit.
2. ``deploy`` asks ARM to validate the deployment as it will be sent (the providers' own
   preflight: sizes, names, policies) before it sends it. An empty resource group it created for
   the deployment goes again when validation fails.

A deployment started with ``cleanup_on_failure`` is tagged so; when one of its resources fails,
target.py stops it (``cancel``) and deletes what it created (cleanup.py).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from ..arm import ArmClient, ArmError, segment
from ..config import Settings
from ..errors import ServiceError, arm_failure
from .capacity import CapacityCheck, CapacityReport
from .gateway import GATEWAY_TAG, MANAGED_TAG, MANAGED_TAG_VALUE
from .gateway_template import aca_parameters, aca_template
from .models import DeployStep, GatewayCreate, GatewayDeployStatus, GatewayPlan
from .sizing import describe, plan

logger = logging.getLogger(__name__)

RESOURCES_API_VERSION = "2021-04-01"
DEPLOYMENTS_API_VERSION = "2024-03-01"
# What the template creates; Microsoft.Cache only when the plan has Redis.
PROVIDERS = (
    "Microsoft.App",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights",
    "Microsoft.ManagedIdentity",
    "Microsoft.DBforPostgreSQL",
)
REDIS_PROVIDER = "Microsoft.Cache"
# On the deployment: delete what it created if one of its resources fails.
CLEANUP_TAG = "ai-gateway-on-failure"
CLEANUP_VALUE = "delete"
_REGISTER_POLLS = 24
_REGISTER_POLL_SECONDS = 5.0
# Polls of a validation Azure runs as a long-running operation.
_VALIDATE_POLLS = 60
_VALIDATE_POLL_SECONDS = 2.0


def deployment_name(gateway: str) -> str:
    return f"ai-gateway-{gateway}"


def group_path(subscription_id: str, resource_group: str) -> str:
    return f"/subscriptions/{segment(subscription_id)}/resourceGroups/{segment(resource_group)}"


def app_path(subscription_id: str, resource_group: str, name: str) -> str:
    return (
        f"{group_path(subscription_id, resource_group)}"
        f"/providers/Microsoft.App/containerApps/{segment(name)}"
    )


def deployment_path(subscription_id: str, resource_group: str, name: str) -> str:
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


def _error_messages(error: Any) -> list[str]:
    """Every innermost message of an ARM error object: validation reports each problem."""
    if not isinstance(error, dict):
        return []
    found = [
        message for detail in error.get("details") or [] for message in _error_messages(detail)
    ]
    if found:
        return found
    single = _error_message(error)
    return [single] if single else []


def _severity(state: str) -> int:
    state = state.casefold()
    return 2 if state in {"failed", "canceled"} else 0 if state == "succeeded" else 1


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


class GatewayDeployer:
    def __init__(self, settings: Settings, sleep: Callable[[float], None] = time.sleep) -> None:
        self._settings = settings
        self._sleep = sleep
        self.capacity = CapacityCheck()

    # -- before anything is created -------------------------------------------------------

    @staticmethod
    def plan(body: GatewayCreate) -> GatewayPlan:
        """What the deployment creates, from its scenario; refused when it can't be built."""
        found = plan(body.sizing, body.platform)
        if found.problems:
            raise ServiceError(
                409,
                f"{' '.join(_sentence(describe(note)) for note in found.problems)} "
                "Nothing was created.",
            )
        return found

    def preflight(
        self, arm: ArmClient, body: GatewayCreate, found: GatewayPlan
    ) -> tuple[list[str], list[str]]:
        """What would keep this deployment from succeeding there, without changing anything,
        and the regions where nothing would."""
        if found.problems:
            return [describe(note) for note in found.problems], []
        try:
            report = self.capacity.problems(arm, body.subscription_id, body.location, found)
            regions = (
                self.capacity.open_regions(arm, body.subscription_id, found, body.location)
                if report.problems
                else []
            )
        except ArmError as error:
            raise arm_failure(error, "check this region") from error
        return report.problems, regions

    def require_capacity(
        self, arm: ArmClient, body: GatewayCreate, found: GatewayPlan
    ) -> CapacityReport:
        """Refuses a region that can't take every resource the deployment creates."""
        report = self.capacity.problems(arm, body.subscription_id, body.location, found)
        if report.problems:
            regions = self.capacity.open_regions(arm, body.subscription_id, found, body.location)
            hint = f" Regions where it fits: {', '.join(regions[:5])}." if regions else ""
            raise ServiceError(
                409,
                f"The gateway can't be deployed in {body.location}, so nothing was created. "
                f"{' '.join(_sentence(problem) for problem in report.problems)}{hint}",
            )
        return report

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

    # -- deploying ------------------------------------------------------------------------

    def start(self, arm: ArmClient, body: GatewayCreate, actor: str) -> None:
        """Starts the deployment; ``status`` follows it."""
        found = self.plan(body)
        try:
            self.check(
                arm,
                body,
                existing=app_path(body.subscription_id, body.resource_group, body.name),
                existing_api=self._settings.container_apps_api_version,
            )
            self.require_capacity(arm, body, found)
            created = self.provision(
                arm, body, actor, providers=PROVIDERS, redis=found.redis_sku is not None
            )
            self.deploy(
                arm,
                body,
                template=aca_template(),
                parameters=aca_parameters(body.name, body.location, body.sizing, found),
                tags={MANAGED_TAG: MANAGED_TAG_VALUE},
                created_group=created,
            )
        except ArmError as error:
            raise arm_failure(error, "deploy this gateway") from error
        logger.info(
            "%s started deploying gateway %s (%d-%d replicas) in %s",
            actor,
            body.name,
            found.min_replicas,
            found.max_replicas,
            group_path(body.subscription_id, body.resource_group),
        )

    def check(
        self, arm: ArmClient, body: GatewayCreate, *, existing: str, existing_api: str
    ) -> None:
        """Refuses what a deployment would collide with: a gateway of that name, its deployment
        still running, leftovers of an earlier one."""
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

    def provision(
        self,
        arm: ArmClient,
        body: GatewayCreate,
        actor: str,
        *,
        providers: tuple[str, ...],
        redis: bool,
    ) -> bool:
        """Registers the resource providers the template needs and creates the resource group
        when it is missing (tagged as this app's); an existing group is never changed. Returns
        whether it created the group."""
        missing = self.unregistered(arm, body.subscription_id, redis=redis, providers=providers)
        if missing:
            logger.info("%s registers %s", actor, ", ".join(missing))
            self._register(arm, body.subscription_id, missing)
        group = group_path(body.subscription_id, body.resource_group)
        if arm.get_arm_json(group, api_version=RESOURCES_API_VERSION) is not None:
            return False
        arm.request(
            "PUT",
            arm.arm_url(group),
            api_version=RESOURCES_API_VERSION,
            json_body={"location": body.location, "tags": {MANAGED_TAG: MANAGED_TAG_VALUE}},
        )
        logger.info("%s created resource group %s", actor, group)
        return True

    def deploy(
        self,
        arm: ArmClient,
        body: GatewayCreate,
        *,
        template: dict[str, Any],
        parameters: dict[str, Any],
        tags: dict[str, str],
        created_group: bool,
    ) -> None:
        """Validates the deployment with ARM, then starts it."""
        if body.cleanup_on_failure:
            tags = {**tags, CLEANUP_TAG: CLEANUP_VALUE}
        deployment = {
            "properties": {"mode": "Incremental", "template": template, "parameters": parameters},
            "tags": tags,
        }
        path = deployment_path(body.subscription_id, body.resource_group, body.name)
        try:
            self._validate(arm, path, deployment)
        except ServiceError:
            if created_group:
                self._drop_empty_group(arm, body)
            raise
        arm.request(
            "PUT", arm.arm_url(path), api_version=DEPLOYMENTS_API_VERSION, json_body=deployment
        )

    def _validate(self, arm: ArmClient, path: str, deployment: dict[str, Any]) -> None:
        response = arm.request(
            "POST",
            arm.arm_url(f"{path}/validate"),
            api_version=DEPLOYMENTS_API_VERSION,
            json_body=deployment,
            accept={400},
        )
        location = response.headers.get("Location")
        for _ in range(_VALIDATE_POLLS):
            if response.status_code != 202 or not location:
                break
            self._sleep(_VALIDATE_POLL_SECONDS)
            response = arm.get_url(location, accept={400})
        if response.status_code == 202:
            raise ServiceError(504, "Azure didn't finish checking the deployment; try again")
        result = response.json() if response.content else {}
        error = result.get("error") or ((result.get("properties") or {}).get("error"))
        if response.status_code == 400 or error:
            problems = _error_messages(error) or ["Azure refused the deployment"]
            raise ServiceError(
                409,
                "Azure's check of the deployment failed, so nothing was created. "
                + " ".join(_sentence(problem) for problem in problems),
            )

    def _drop_empty_group(self, arm: ArmClient, body: GatewayCreate) -> None:
        """Deletes the resource group this deployment just created, if it is still empty."""
        group = group_path(body.subscription_id, body.resource_group)
        try:
            if next(
                iter(arm.list_arm(f"{group}/resources", api_version=RESOURCES_API_VERSION)), None
            ):
                return
            arm.request("DELETE", arm.arm_url(group), api_version=RESOURCES_API_VERSION)
            logger.info("Deleted the empty resource group %s again", group)
        except ArmError as error:
            logger.warning("Could not delete the empty resource group %s: %s", group, error.message)

    def cancel(self, arm: ArmClient, subscription_id: str, resource_group: str, name: str) -> None:
        """Stops a running deployment: ARM starts no more of its resources."""
        try:
            arm.request(
                "POST",
                arm.arm_url(f"{deployment_path(subscription_id, resource_group, name)}/cancel"),
                api_version=DEPLOYMENTS_API_VERSION,
            )
            logger.warning("Stopped the deployment of gateway %s after a resource failed", name)
        except ArmError as error:
            # Already finished (409): nothing to stop.
            logger.info("Could not stop the deployment of %s: %s", name, error.message)

    # -- following it ---------------------------------------------------------------------

    def status(
        self, arm: ArmClient, subscription_id: str, resource_group: str, name: str
    ) -> GatewayDeployStatus:
        path = deployment_path(subscription_id, resource_group, name)
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
            raise arm_failure(error, "read the gateway's deployment") from error
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
        ordered = sorted(steps.values(), key=lambda step: (step.resource_type, step.name))
        reason = _error_message(properties.get("error"))
        failed_step = next((step.error for step in ordered if step.error), None)
        if state == "canceled" or not reason:
            # Stopped after a resource failed: that resource's error says why.
            stopped = "The deployment was stopped" if state == "canceled" else None
            reason = failed_step or reason or stopped
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
            steps=ordered,
            error=reason,
            auto_cleanup=(deployment.get("tags") or {}).get(CLEANUP_TAG) == CLEANUP_VALUE,
        )
