"""Deploying a LiteLLM gateway on a new AKS cluster, with the signed-in user's own Azure rights.

Three phases, each followed by the deployment's status (``AksDeployer.status``):

1. azure: one ARM deployment (``ai-gateway-<name>``, tagged with the platform) of
   templates/litellm-aks.json: the cluster (Entra ID with Azure RBAC, workload identity, the
   managed Gateway API), the gateway's identity federated with LiteLLM's service account,
   PostgreSQL, Redis for production, Application Insights, and RBAC Cluster Admin on the cluster
   for the user deploying it.
2. cluster: AKS Run Command installs LiteLLM, its OpenTelemetry Collector, cert-manager and the
   HTTPS Gateway (gateway_template.aks_bootstrap), with a token the user's sign-in gets for the
   AKS server app. The install can run again safely, so it starts again after a restart.
3. certificate: Let's Encrypt issues the certificate for https://<name>.<region>.cloudapp.azure.com;
   once LiteLLM answers there, the gateway is connected like any external one.

The secrets (master and salt keys, the Postgres password) are made once, before the deployment,
and kept in the local secret store until the install is done; afterwards only the master key
stays there, under the gateway's URL. They reach the cluster only inside Run Command's file
context and are never logged. The cluster is the user's to maintain after that.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..access import AUTHORIZATION_API_VERSION, missing
from ..arm import ArmClient, ArmError, segment
from ..config import Settings
from ..errors import ServiceError, arm_failure
from ..secret_store import SecretStore
from .gateway import MANAGED_TAG, MANAGED_TAG_VALUE
from .gateway_deploy import (
    DEPLOYMENTS_API_VERSION,
    PROVIDERS,
    GatewayDeployer,
    deployment_path,
    group_path,
)
from .gateway_template import (
    AKS_COMMAND,
    AKS_NODE_SIZE,
    AksInstall,
    aks_bootstrap,
    aks_fqdn,
    aks_parameters,
    aks_secrets,
    aks_template,
    context_zip,
)
from .models import GatewayCreate, GatewayDeployStatus

logger = logging.getLogger(__name__)

CONTAINER_SERVICE_API_VERSION = "2025-05-01"
COMPUTE_API_VERSION = "2024-07-01"
SKUS_API_VERSION = "2021-07-01"
NETWORK_API_VERSION = "2024-05-01"
INSIGHTS_API_VERSION = "2020-02-02"
REDIS_API_VERSION = "2025-04-01"
# Azure CLI's (and the local sign-in's) audience for an Entra ID cluster's Kubernetes API.
AKS_SERVER_SCOPE = "6dae42f8-4368-4678-94ff-3960e28e3630/.default"
AKS_PROVIDERS = (*PROVIDERS[1:], "Microsoft.ContainerService", "Microsoft.Network")
PLATFORM_TAG = "ai-gateway-platform"
ROLE_ASSIGNMENT_WRITE = "Microsoft.Authorization/roleAssignments/write"
# The node VM family's quota name, and vCPUs per node (Standard_D4ds_v5).
_VM_FAMILY = "standardDDSv5Family"
_VCPUS_PER_NODE = 4
# Lines of the install's output kept for the status.
_LOG_LINES = 40


def cluster_path(subscription_id: str, resource_group: str, name: str) -> str:
    return (
        f"{group_path(subscription_id, resource_group)}"
        f"/providers/Microsoft.ContainerService/managedClusters/{segment(name)}"
    )


def _pending_key(cluster_id: str) -> str:
    # SecretStore keys are case-folded URLs; this one never collides with a gateway's.
    return f"aks-install:{cluster_id}"


@dataclass(frozen=True)
class AksFinished:
    """A gateway whose install is done: what the app saves to reach it."""

    url: str
    cluster_id: str
    principal_id: str
    app_insights_id: str


class AksDeployer:
    def __init__(
        self,
        settings: Settings,
        base: GatewayDeployer,
        secrets: SecretStore | None,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._base = base
        self._secrets = secrets
        self._transport = transport
        self._clock = clock

    # -- before anything is created -------------------------------------------------------

    def preflight(self, arm: ArmClient, body: GatewayCreate) -> list[str]:
        """Read-only checks for what an AKS deployment needs; returns the VM size's zones."""
        subscription = f"/subscriptions/{segment(body.subscription_id)}"
        nodes = 3 if body.profile == "production" else 1
        try:
            dns = arm.get_arm_json(
                f"{subscription}/providers/Microsoft.Network/locations/{segment(body.location)}"
                "/CheckDnsNameAvailability",
                api_version=NETWORK_API_VERSION,
                params={"domainNameLabel": body.name},
            )
            if dns is not None and dns.get("available") is False:
                raise ServiceError(
                    409,
                    f"{aks_fqdn(body.name, body.location)} is taken; choose another name "
                    "(it becomes the gateway's HTTPS address)",
                )
            zones = self._vm_zones(arm, subscription, body.location)
            self._quota(arm, subscription, body.location, nodes * _VCPUS_PER_NODE)
            scope = group_path(body.subscription_id, body.resource_group)
            if arm.get_arm_json(scope, api_version="2021-04-01") is None:
                scope = subscription
            permissions: list[Mapping[str, Any]] = list(
                arm.list_arm(
                    f"{scope}/providers/Microsoft.Authorization/permissions",
                    api_version=AUTHORIZATION_API_VERSION,
                )
            )
        except ArmError as error:
            raise arm_failure(error, "check this AKS deployment") from error
        if missing(permissions, [ROLE_ASSIGNMENT_WRITE]):
            raise ServiceError(
                403,
                "Deploying on AKS grants you Azure Kubernetes Service RBAC Cluster Admin on the "
                "new cluster, which needs Owner or User Access Administrator on the resource "
                f"group; your account lacks {ROLE_ASSIGNMENT_WRITE} there",
            )
        return zones

    def _vm_zones(self, arm: ArmClient, subscription: str, location: str) -> list[str]:
        """The node size's availability zones, or why the subscription can't use it there."""
        skus = arm.list_arm(
            f"{subscription}/providers/Microsoft.Compute/skus",
            api_version=SKUS_API_VERSION,
            params={"$filter": f"location eq '{location}'"},
        )
        for sku in skus:
            if sku.get("resourceType") != "virtualMachines" or sku.get("name") != AKS_NODE_SIZE:
                continue
            restricted = {
                str(item.get("type"))
                for item in sku.get("restrictions") or []
                if item.get("reasonCode") == "NotAvailableForSubscription"
            }
            if "Location" in restricted:
                raise ServiceError(
                    409,
                    f"{AKS_NODE_SIZE} isn't available to this subscription in {location}; "
                    "choose another region",
                )
            info = (sku.get("locationInfo") or [{}])[0]
            blocked = {
                zone
                for item in sku.get("restrictions") or []
                if item.get("type") == "Zone"
                for zone in (item.get("restrictionInfo") or {}).get("zones") or []
            }
            return sorted(str(zone) for zone in info.get("zones") or [] if zone not in blocked)
        raise ServiceError(409, f"{AKS_NODE_SIZE} isn't offered in {location}")

    def _quota(self, arm: ArmClient, subscription: str, location: str, needed: int) -> None:
        found = arm.get_arm_json(
            f"{subscription}/providers/Microsoft.Compute/locations/{segment(location)}/usages",
            api_version=COMPUTE_API_VERSION,
        )
        for usage in (found or {}).get("value") or []:
            name = str((usage.get("name") or {}).get("value") or "")
            if name.casefold() not in {_VM_FAMILY.casefold(), "cores"}:
                continue
            free = int(usage.get("limit") or 0) - int(usage.get("currentValue") or 0)
            if free < needed:
                label = (usage.get("name") or {}).get("localizedValue") or name
                raise ServiceError(
                    409,
                    f"Not enough vCPU quota in {location}: {label} has {max(free, 0)} free, the "
                    f"cluster needs {needed}. Request more in the portal (Quotas) or choose "
                    "another region",
                )

    # -- deploying ------------------------------------------------------------------------

    def start(self, arm: ArmClient, body: GatewayCreate, deployer: str, actor: str) -> str:
        """Starts the deployment and returns the gateway's URL; ``status`` follows it."""
        if self._secrets is None:
            raise ServiceError(500, "The app has no store for the gateway's keys")
        if not deployer:
            raise ServiceError(401, "Sign in again: your account's object ID isn't known")
        cluster = cluster_path(body.subscription_id, body.resource_group, body.name)
        try:
            self._base.check(
                arm, body, existing=cluster, existing_api=CONTAINER_SERVICE_API_VERSION
            )
            zones = self.preflight(arm, body)
            self._base.provision(arm, body, actor, providers=AKS_PROVIDERS)
            secrets = aks_secrets()
            # Kept before Azure sees anything, so a gateway never runs on keys the app lost.
            self._secrets.put(_pending_key(cluster), json.dumps(secrets))
            self._base.put_deployment(
                arm,
                body,
                template=aks_template(),
                parameters=aks_parameters(
                    name=body.name,
                    location=body.location,
                    profile=body.profile,
                    deployer=deployer,
                    zones=zones,
                    postgres_password=secrets["postgres_password"],
                ),
                tags={MANAGED_TAG: MANAGED_TAG_VALUE, PLATFORM_TAG: "aks"},
            )
        except ArmError as error:
            raise arm_failure(error, "deploy this gateway") from error
        logger.info(
            "%s started deploying gateway %s on AKS (%s) in %s",
            actor,
            body.name,
            body.profile,
            body.resource_group,
        )
        return f"https://{aks_fqdn(body.name, body.location)}"

    def is_aks(self, arm: ArmClient, subscription_id: str, resource_group: str, name: str) -> bool:
        try:
            found = arm.get_arm_json(
                deployment_path(subscription_id, resource_group, name),
                api_version=DEPLOYMENTS_API_VERSION,
            )
        except ArmError:
            return False
        return bool(((found or {}).get("tags") or {}).get(PLATFORM_TAG) == "aks")

    def status(
        self, arm: ArmClient, subscription_id: str, resource_group: str, name: str
    ) -> tuple[GatewayDeployStatus, AksFinished | None]:
        status = self._base.status(arm, subscription_id, resource_group, name).model_copy(
            update={"platform": "aks"}
        )
        if status.state != "succeeded":
            return status, None
        cluster = cluster_path(subscription_id, resource_group, name)
        try:
            outputs = self._outputs(arm, subscription_id, resource_group, name)
            url = f"https://{outputs['fqdn']}"
            finished = AksFinished(
                url=url,
                cluster_id=str(outputs.get("clusterId") or cluster),
                principal_id=str(outputs.get("principalId") or ""),
                app_insights_id=str(outputs.get("appInsightsId") or ""),
            )
            pending = self._pending(cluster)
            if pending is None:
                if self._secrets is not None and self._secrets.get(url):
                    return status.model_copy(update={"phase": "done"}), finished
                return self._failed(
                    status,
                    "cluster",
                    "The app no longer has this gateway's keys (they were kept in memory "
                    "only). Delete it and deploy again, or connect it with its master key.",
                )
            command = pending.get("command_id")
            if not command:
                pending["command_id"] = self._run(arm, cluster, name, outputs, pending)
                self._keep(cluster, pending)
                return status.model_copy(update={"state": "running", "phase": "cluster"}), None
            result = self._result(arm, cluster, str(command))
        except ArmError as error:
            raise arm_failure(error, "follow the gateway's install") from error
        if result is None:
            return status.model_copy(update={"state": "running", "phase": "cluster"}), None
        state, exit_code, logs = result
        tail = "\n".join(logs.strip().splitlines()[-_LOG_LINES:]) or None
        if state != "succeeded" or exit_code != 0:
            return self._failed(
                status.model_copy(update={"log_tail": tail}),
                "cluster",
                f"Installing LiteLLM in the cluster failed (exit code {exit_code}); the end of "
                "its output is below. Try the install again once the cause is fixed.",
            )
        status = status.model_copy(update={"log_tail": tail})
        if not self._answers(url):
            return status.model_copy(update={"state": "running", "phase": "certificate"}), None
        assert self._secrets is not None
        self._secrets.put(url, pending["master_key"])
        self._secrets.forget(_pending_key(cluster))
        logger.info("Gateway %s on AKS answers at %s", name, url)
        return status.model_copy(update={"phase": "done"}), finished

    def retry_install(self, subscription_id: str, resource_group: str, name: str) -> None:
        """Runs the install again on the next status: after fixing what made it fail."""
        cluster = cluster_path(subscription_id, resource_group, name)
        pending = self._pending(cluster)
        if pending is None:
            raise ServiceError(404, f"{name} has no install to run again")
        pending.pop("command_id", None)
        self._keep(cluster, pending)

    def forget(self, cluster_id: str, url: str) -> None:
        if self._secrets is not None:
            self._secrets.forget(_pending_key(cluster_id))
            self._secrets.forget(url)

    # -- the phases' details --------------------------------------------------------------

    def _failed(
        self, status: GatewayDeployStatus, phase: str, error: str
    ) -> tuple[GatewayDeployStatus, None]:
        return status.model_copy(update={"state": "failed", "phase": phase, "error": error}), None

    def _pending(self, cluster_id: str) -> dict[str, Any] | None:
        if self._secrets is None:
            return None
        raw = self._secrets.get(_pending_key(cluster_id))
        if not raw:
            return None
        value: dict[str, Any] = json.loads(raw)
        return value

    def _keep(self, cluster_id: str, pending: dict[str, Any]) -> None:
        assert self._secrets is not None
        self._secrets.put(_pending_key(cluster_id), json.dumps(pending))

    def _outputs(
        self, arm: ArmClient, subscription_id: str, resource_group: str, name: str
    ) -> dict[str, Any]:
        found = arm.get_arm_json(
            deployment_path(subscription_id, resource_group, name),
            api_version=DEPLOYMENTS_API_VERSION,
        )
        outputs = ((found or {}).get("properties") or {}).get("outputs") or {}
        return {key: (value or {}).get("value") for key, value in outputs.items()}

    def _run(
        self,
        arm: ArmClient,
        cluster: str,
        name: str,
        outputs: dict[str, Any],
        pending: dict[str, Any],
    ) -> str:
        """Starts the install with Run Command; returns the command's ID."""
        found = arm.get_arm_json(cluster, api_version=CONTAINER_SERVICE_API_VERSION) or {}
        profile = str((found.get("tags") or {}).get("ai-gateway-profile") or "dev")
        insights = (
            arm.get_arm_json(str(outputs["appInsightsId"]), api_version=INSIGHTS_API_VERSION) or {}
        )
        redis_host = str(outputs.get("redisHost") or "")
        redis_password = ""
        if redis_host:
            redis = str(found.get("id") or cluster).rsplit("/providers/", 1)[0]
            redis_name = redis_host.split(".", 1)[0]
            keys = arm.request(
                "POST",
                arm.arm_url(
                    f"{redis}/providers/Microsoft.Cache/redisEnterprise/{segment(redis_name)}"
                    "/databases/default/listKeys"
                ),
                api_version=REDIS_API_VERSION,
            ).json()
            redis_password = str(keys.get("primaryKey") or "")
        claims = arm.token_claims()
        email = str(claims.get("upn") or claims.get("email") or "")
        install = AksInstall(
            name=name,
            profile="production" if profile == "production" else "dev",
            fqdn=str(outputs["fqdn"]),
            identity_client_id=str(outputs["identityClientId"]),
            postgres_host=str(outputs["postgresHost"]),
            app_insights_connection_string=str(
                (insights.get("properties") or {}).get("ConnectionString") or ""
            ),
            redis_host=redis_host,
            redis_password=redis_password,
            email=email if "@" in email else "",
        )
        files = aks_bootstrap(install, pending)
        response = arm.request(
            "POST",
            arm.arm_url(f"{cluster}/runCommand"),
            api_version=CONTAINER_SERVICE_API_VERSION,
            json_body={
                "command": AKS_COMMAND,
                "context": context_zip(files),
                "clusterToken": arm.token_for(AKS_SERVER_SCOPE),
            },
        )
        location = response.headers.get("Location") or ""
        command = urlsplit(location).path.rstrip("/").rsplit("/", 1)[-1]
        if not command:
            command = str((response.json() if response.content else {}).get("id") or "")
        if not command:
            raise ArmError(502, "RunCommand", "AKS didn't say where to follow the install")
        logger.info("Installing gateway %s in its cluster (command %s)", name, command)
        return command

    def _result(
        self, arm: ArmClient, cluster: str, command: str
    ) -> tuple[str, int | None, str] | None:
        """The install's outcome, or None while it runs."""
        response = arm.request(
            "GET",
            arm.arm_url(f"{cluster}/commandResults/{segment(command)}"),
            api_version=CONTAINER_SERVICE_API_VERSION,
        )
        if response.status_code == 202 or not response.content:
            return None
        properties = response.json().get("properties") or {}
        state = str(properties.get("provisioningState") or "").casefold()
        if state not in {"succeeded", "failed", "canceled"}:
            return None
        exit_code = properties.get("exitCode")
        return (
            state,
            int(exit_code) if isinstance(exit_code, int) else None,
            str(properties.get("logs") or properties.get("reason") or ""),
        )

    def _answers(self, url: str) -> bool:
        """LiteLLM answers on the HTTPS address with a trusted certificate."""
        try:
            with httpx.Client(transport=self._transport, timeout=10.0) as client:
                response = client.get(f"{url}/health/liveliness")
        except httpx.HTTPError:
            return False
        return response.status_code == 200
