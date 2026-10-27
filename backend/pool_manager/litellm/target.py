"""Choosing a LiteLLM gateway, deploying one, or connecting one, from the local setup wizard.

The user picks a gateway in a subscription (a Container App, or an AKS cluster this app deployed
LiteLLM on), deploys a new one on Container Apps or AKS, or connects one that runs elsewhere by
its URL and master key. ``TargetChooser`` (target.py) saves the choice.

Every change here uses the signed-in user's own Azure rights:

- Deploying registers the resource providers the template needs, creates the resource group when
  it does not exist, and runs one ARM deployment (gateway_deploy.py). It never changes an
  existing resource group or gateway.
- Deleting is limited to gateways this app deployed (the ``managed-by`` tag) and needs the name
  typed again. It deletes the resources tagged as that gateway's, starting with the Container
  App, and never the resource group. Azure keeps no copy: the gateway's database (pools, keys,
  spend) goes with it.
- Connecting checks the URL and master key against the gateway itself and keeps the key in the
  OS-encrypted local store (secret_store.py), never in the state file.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..access import AUTHORIZATION_API_VERSION, Permissions, missing
from ..arm import ArmClient, ArmError, segment
from ..errors import ServiceError, arm_failure
from ..models import ResourceGroupView, TargetView
from ..secret_store import SecretStore
from ..target import TargetChooser, resource_group_of
from .access import GATEWAY_ACTIONS
from .aks_deploy import CONTAINER_SERVICE_API_VERSION, AksDeployer, cluster_path
from .client import LiteLLMClient, LiteLLMError
from .gateway import GATEWAY_TAG, MANAGED_TAG, MANAGED_TAG_VALUE, container_app_info
from .gateway_deploy import (
    DEPLOYMENTS_API_VERSION,
    RESOURCES_API_VERSION,
    GatewayDeployer,
    app_path,
    deployment_path,
    group_path,
)
from .gateway_template import aks_fqdn
from .models import (
    GatewayConnect,
    GatewayCreate,
    GatewayCreateOptions,
    GatewayDelete,
    GatewayDeleteCheck,
    GatewayDeployStatus,
    GatewayProfile,
    GatewayView,
    TargetSelect,
)

logger = logging.getLogger(__name__)

_WORKERS = 8
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Delete order and API versions of what the templates create: the Container App first (its
# environment can't go while it has apps), the environment or the AKS cluster (which Azure takes
# many minutes over, so it finishes on its own), then the rest.
_DELETE_ORDER = {
    "microsoft.app/containerapps": (0, "2025-01-01"),
    "microsoft.app/managedenvironments": (1, "2025-01-01"),
    "microsoft.containerservice/managedclusters": (1, CONTAINER_SERVICE_API_VERSION),
    "microsoft.dbforpostgresql/flexibleservers": (2, "2024-08-01"),
    "microsoft.cache/redisenterprise": (2, "2025-04-01"),
    "microsoft.managedidentity/userassignedidentities": (3, "2023-01-31"),
    "microsoft.insights/components": (3, "2020-02-02"),
    "microsoft.operationalinsights/workspaces": (4, "2023-09-01"),
}


def _is_litellm(app: Mapping[str, Any]) -> bool:
    """Deployed by this app, or running LiteLLM's image."""
    if (app.get("tags") or {}).get(MANAGED_TAG) == MANAGED_TAG_VALUE:
        return True
    containers = ((app.get("properties") or {}).get("template") or {}).get("containers") or []
    return any("litellm" in str(c.get("image") or "").casefold() for c in containers)


def _profile(app: Mapping[str, Any]) -> GatewayProfile | None:
    value = (app.get("tags") or {}).get("ai-gateway-profile")
    return value if value in {"dev", "production"} else None


class LiteLLMTargets:
    """The LiteLLM half of the gateway step; ``TargetChooser`` has the shared half."""

    def __init__(
        self,
        targets: TargetChooser,
        *,
        gateway_transport: httpx.BaseTransport | None = None,
        secrets: SecretStore | None = None,
    ) -> None:
        self._targets = targets
        self._settings = targets.settings
        # How connecting reaches the gateway (tests replace it).
        self._gateway_transport = gateway_transport
        self._secrets = secrets
        self._deployer = GatewayDeployer(targets.settings, targets.sleep)
        self._aks = AksDeployer(
            targets.settings, self._deployer, secrets, transport=gateway_transport
        )

    # -- reads -------------------------------------------------------------------------

    def gateways(self, tenant_id: str | None, subscription_id: str) -> list[GatewayView]:
        """LiteLLM gateways on Container Apps in the subscription, and the AKS clusters this
        app deployed LiteLLM on."""
        self._targets.require_local()
        arm = self._targets.arm(tenant_id)
        subscription = f"/subscriptions/{segment(subscription_id)}"
        try:
            items = [
                item
                for item in arm.list_arm(
                    f"{subscription}/providers/Microsoft.App/containerApps",
                    api_version=self._settings.container_apps_api_version,
                )
                if _is_litellm(item)
            ]
        except ArmError as error:
            if error.code != "MissingSubscriptionRegistration":  # No Container Apps yet.
                raise arm_failure(error, "list the Container Apps of this subscription") from error
            items = []
        try:
            clusters = [
                item
                for item in arm.list_arm(
                    f"{subscription}/providers/Microsoft.ContainerService/managedClusters",
                    api_version=CONTAINER_SERVICE_API_VERSION,
                )
                if (item.get("tags") or {}).get(MANAGED_TAG) == MANAGED_TAG_VALUE
            ]
        except ArmError as error:
            logger.info("Cannot list the AKS clusters of %s: %s", subscription_id, error.message)
            clusters = []
        views = [self._cluster_view(item) for item in clusters]
        if items:
            with ThreadPoolExecutor(max_workers=min(_WORKERS, len(items))) as executor:
                views += list(executor.map(lambda item: self._gateway_view(arm, item), items))
        return sorted(views, key=lambda view: view.name.casefold())

    def _cluster_view(self, item: Mapping[str, Any]) -> GatewayView:
        cluster_id = str(item.get("id") or "")
        name = str(item.get("name") or "")
        location = str(item.get("location") or "")
        state = str((item.get("properties") or {}).get("provisioningState") or "") or None
        return GatewayView(
            id=cluster_id,
            name=name,
            resource_group=resource_group_of(cluster_id),
            location=location,
            url=f"https://{aks_fqdn(name, location)}",
            profile=_profile(item),
            provisioning_state=state,
            managed=True,
            can_manage=None,
            platform="aks",
        )

    def _gateway_view(self, arm: ArmClient, item: Mapping[str, Any]) -> GatewayView:
        app_id = str(item.get("id") or "")
        gaps: list[str] = []
        can_manage: bool | None = None
        try:
            permissions: Permissions = list(
                arm.list_arm(
                    f"{app_id}/providers/Microsoft.Authorization/permissions",
                    api_version=AUTHORIZATION_API_VERSION,
                )
            )
            gaps = missing(permissions, GATEWAY_ACTIONS)
            can_manage = not gaps
        except ArmError as error:
            logger.info("Cannot read permissions on %s: %s", app_id, error.message)
        info = container_app_info(item, str(item.get("name") or ""))
        return GatewayView(
            id=app_id,
            name=info.name,
            resource_group=resource_group_of(app_id),
            location=info.location or "",
            url=info.url,
            profile=_profile(item),
            provisioning_state=info.provisioning_state,
            managed=info.managed,
            can_manage=can_manage,
            missing=gaps,
        )

    def create_options(self, tenant_id: str | None, subscription_id: str) -> GatewayCreateOptions:
        self._targets.require_local()
        arm = self._targets.arm(tenant_id)
        subscription = f"/subscriptions/{segment(subscription_id)}"
        try:
            groups = [
                ResourceGroupView(name=str(group["name"]), location=str(group["location"]))
                for group in arm.list_arm(
                    f"{subscription}/resourcegroups", api_version=RESOURCES_API_VERSION
                )
            ]
            provider = arm.get_arm_json(
                f"{subscription}/providers/Microsoft.App", api_version=RESOURCES_API_VERSION
            )
            unregistered = self._deployer.unregistered(arm, subscription_id, redis=True)
        except ArmError as error:
            raise arm_failure(error, "read this subscription") from error
        types = (provider or {}).get("resourceTypes") or []
        environments: dict[str, Any] = next(
            (t for t in types if str(t.get("resourceType")).casefold() == "managedenvironments"),
            {},
        )
        locations = sorted(
            {
                str(location).casefold().replace(" ", "")
                for location in environments.get("locations") or []
                if not str(location).casefold().replace(" ", "").endswith("euap")
            }
        )
        return GatewayCreateOptions(
            resource_groups=sorted(groups, key=lambda group: group.name.casefold()),
            locations=locations,
            unregistered_providers=unregistered,
        )

    # -- changes -----------------------------------------------------------------------

    def _save(self, target: Mapping[str, str]) -> None:
        self._targets.save("litellm", target)

    def select(self, body: TargetSelect, actor: str) -> TargetView:
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        path = app_path(body.subscription_id, body.resource_group, body.gateway_name)
        try:
            found = arm.get_arm_json(path, api_version=self._settings.container_apps_api_version)
        except ArmError as error:
            raise arm_failure(error, "read this Container App") from error
        if found is None:
            cluster = self._managed_cluster(arm, body)
            if cluster is not None:
                return self._select_cluster(arm, body, cluster, actor)
            raise ServiceError(
                404, f"Container App {body.gateway_name} not found in {body.resource_group}"
            )
        if not _is_litellm(found):
            raise ServiceError(409, f"{body.gateway_name} doesn't run LiteLLM")
        # ARM's casing, so the saved target matches the resource IDs ARM returns.
        resource_id = str(found.get("id") or path)
        target = {
            "subscription_id": body.subscription_id,
            "resource_group": resource_group_of(resource_id) or body.resource_group,
            "gateway_name": str(found.get("name") or body.gateway_name),
            "tenant_id": body.tenant_id or arm.subscription_tenant(body.subscription_id) or "",
        }
        self._save(target)
        logger.info("%s chose gateway %s", actor, resource_id)
        return self._view(target)

    def _managed_cluster(self, arm: ArmClient, body: TargetSelect) -> dict[str, Any] | None:
        """The AKS cluster of that name, when this app deployed LiteLLM on it."""
        try:
            found = arm.get_arm_json(
                cluster_path(body.subscription_id, body.resource_group, body.gateway_name),
                api_version=CONTAINER_SERVICE_API_VERSION,
            )
        except ArmError as error:
            raise arm_failure(error, "read this AKS cluster") from error
        if found and (found.get("tags") or {}).get(MANAGED_TAG) == MANAGED_TAG_VALUE:
            return found
        return None

    def _select_cluster(
        self, arm: ArmClient, body: TargetSelect, cluster: Mapping[str, Any], actor: str
    ) -> TargetView:
        """Chooses an AKS gateway this app deployed, with the master key it keeps for it."""
        name = str(cluster.get("name") or body.gateway_name)
        url = f"https://{aks_fqdn(name, str(cluster.get('location') or ''))}"
        if self._secrets is None or not self._secrets.get(url):
            raise ServiceError(
                409,
                f"This machine doesn't have {name}'s master key. Choose Connect a gateway, enter "
                f"{url} and the key, which you can read with: kubectl -n litellm get secret "
                "litellm-masterkey -o jsonpath='{.data.masterkey}' | base64 -d",
            )
        cluster_id = str(cluster.get("id") or "")
        group = resource_group_of(cluster_id) or body.resource_group
        try:
            identity = arm.get_arm_json(
                f"{group_path(body.subscription_id, group)}/providers"
                f"/Microsoft.ManagedIdentity/userAssignedIdentities/id-{segment(name)}",
                api_version="2023-01-31",
            )
        except ArmError as error:
            raise arm_failure(error, "read the gateway's identity") from error
        target = {
            "subscription_id": body.subscription_id,
            "resource_group": group,
            "gateway_name": name,
            "tenant_id": body.tenant_id or arm.subscription_tenant(body.subscription_id) or "",
            "gateway_url": url,
            "gateway_principal_id": str(
                ((identity or {}).get("properties") or {}).get("principalId") or ""
            ),
            "gateway_app_insights_id": str(
                (cluster.get("tags") or {}).get("ai-gateway-app-insights") or ""
            ),
            "gateway_cluster_id": cluster_id,
        }
        self._save(target)
        logger.info("%s chose AKS gateway %s", actor, cluster_id)
        return self._view(target)

    def _view(self, target: Mapping[str, str]) -> TargetView:
        return TargetView(
            product="litellm",
            gateway_name=target.get("gateway_name") or None,
            gateway_kind="external" if target.get("gateway_url") else "aca",
            subscription_id=target.get("subscription_id") or None,
            resource_group=target.get("resource_group") or None,
            gateway_url=target.get("gateway_url") or None,
            tenant_id=target.get("tenant_id") or None,
            source="browser" if self._settings.public else "saved",
            can_change=True,
            cluster_id=target.get("gateway_cluster_id") or None,
            provisioning=bool(target.get("gateway_provisioning")),
        )

    def create(self, body: GatewayCreate, actor: str) -> GatewayView:
        """Starts deploying a gateway and selects it. Setup follows the deployment."""
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        if body.platform == "aks":
            return self._create_aks(arm, body, actor)
        self._deployer.start(arm, body, actor)
        self._save(
            {
                "subscription_id": body.subscription_id,
                "resource_group": body.resource_group,
                "gateway_name": body.name,
                "tenant_id": body.tenant_id or arm.subscription_tenant(body.subscription_id) or "",
            }
        )
        return GatewayView(
            id=app_path(body.subscription_id, body.resource_group, body.name),
            name=body.name,
            resource_group=body.resource_group,
            location=body.location,
            url=None,
            profile=body.profile,
            provisioning_state="Deploying",
            managed=True,
            can_manage=True,
        )

    def _create_aks(self, arm: ArmClient, body: GatewayCreate, actor: str) -> GatewayView:
        """Starts deploying a gateway on a new AKS cluster and selects it; it is reached by its
        URL, like a gateway the user connected, once the install in the cluster is done."""
        deployer = str(arm.token_claims().get("oid") or "")
        url = self._aks.start(arm, body, deployer, actor)
        cluster_id = cluster_path(body.subscription_id, body.resource_group, body.name)
        self._save(
            {
                "subscription_id": body.subscription_id,
                "resource_group": body.resource_group,
                "gateway_name": body.name,
                "tenant_id": body.tenant_id or arm.subscription_tenant(body.subscription_id) or "",
                "gateway_url": url,
                "gateway_cluster_id": cluster_id,
                "gateway_provisioning": "aks",
                "gateway_started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        return GatewayView(
            id=cluster_id,
            name=body.name,
            resource_group=body.resource_group,
            location=body.location,
            url=url,
            profile=body.profile,
            provisioning_state="Deploying",
            managed=True,
            can_manage=True,
            platform="aks",
        )

    def deploy_status(
        self, tenant_id: str | None, subscription_id: str, resource_group: str, name: str
    ) -> GatewayDeployStatus:
        self._targets.require_local()
        arm = self._targets.arm(tenant_id)
        if not self._aks.is_aks(arm, subscription_id, resource_group, name):
            return self._deployer.status(arm, subscription_id, resource_group, name)
        status, finished = self._aks.status(arm, subscription_id, resource_group, name)
        # The saved target, not this chooser's settings: those are from before the deployment
        # started, until the app rebuilds what is bound to the gateway.
        saved = self._targets.saved() or {}
        if (
            finished is not None
            and saved.get("gateway_provisioning")
            and saved.get("gateway_name", "").casefold() == name.casefold()
            and saved.get("resource_group", "").casefold() == resource_group.casefold()
        ):
            self._save(
                {
                    "subscription_id": subscription_id,
                    "resource_group": resource_group,
                    "gateway_name": name,
                    "tenant_id": saved.get("tenant_id", ""),
                    "gateway_url": finished.url,
                    "gateway_principal_id": finished.principal_id,
                    "gateway_app_insights_id": finished.app_insights_id,
                    "gateway_cluster_id": finished.cluster_id,
                }
            )
            logger.info("Gateway %s on AKS is ready at %s", name, finished.url)
        return status

    def retry_install(self, body: TargetSelect) -> None:
        """Runs an AKS gateway's install in its cluster again, after it failed."""
        self._targets.require_changeable()
        self._aks.retry_install(body.subscription_id, body.resource_group, body.gateway_name)

    def connect(self, body: GatewayConnect, actor: str) -> TargetView:
        """Checks an external gateway's URL and master key, then selects it."""
        self._targets.require_changeable()
        url = str(body.url).rstrip("/")
        parts = urlsplit(url)
        if parts.scheme != "https" and (parts.hostname or "") not in _LOCAL_HOSTS:
            raise ServiceError(400, "Use the gateway's HTTPS URL (plain HTTP only for localhost)")
        if parts.path not in {"", "/"} or parts.query:
            raise ServiceError(400, "Use the gateway's base URL, without a path")
        master_key = body.master_key.get_secret_value()
        client = LiteLLMClient(url, master_key, transport=self._gateway_transport)
        try:
            health = client.readiness()
            client.models()
        except LiteLLMError as error:
            if error.status in {401, 403}:
                raise ServiceError(400, "The gateway refused that master key") from error
            raise ServiceError(
                502, f"Cannot reach a LiteLLM gateway at {url}: {error.message}"
            ) from error
        finally:
            client.close()
        if str(health.get("db") or "").casefold() not in {"connected", ""}:
            raise ServiceError(
                409,
                f"{url} has no database ({health.get('db')}); pools need STORE_MODEL_IN_DB",
            )
        if self._secrets is None:
            raise ServiceError(500, "The app has no store for gateway keys")
        self._secrets.put(url, master_key)
        arm = self._targets.arm(body.tenant_id)
        target = {
            "subscription_id": body.subscription_id,
            "gateway_name": body.name,
            "gateway_url": url,
            "gateway_principal_id": body.principal_id or "",
            "gateway_app_insights_id": body.app_insights_id or "",
            "tenant_id": body.tenant_id or arm.subscription_tenant(body.subscription_id) or "",
        }
        self._save(target)
        logger.info("%s connected gateway %s at %s", actor, body.name, url)
        return self._view(target)

    def disconnect(self, actor: str) -> None:
        """Forgets the external gateway and its key; the gateway itself is not touched."""
        self._targets.require_changeable()
        if not self._settings.external:
            raise ServiceError(400, "Only a connected gateway can be disconnected")
        if self._secrets is not None:
            self._secrets.forget(self._settings.gateway_url)
        logger.info("%s disconnected gateway %s", actor, self._settings.gateway_name)
        self._targets.clear()

    # -- deleting ----------------------------------------------------------------------

    def _is_current(self, body: TargetSelect) -> bool:
        settings = self._settings
        # A gateway someone connected is never deleted here; one the app deployed on AKS is.
        return (
            settings.has_target
            and (not settings.external or bool(settings.gateway_cluster_id))
            and (
                settings.subscription_id.casefold(),
                settings.resource_group.casefold(),
                settings.gateway_name.casefold(),
            )
            == (
                body.subscription_id.casefold(),
                body.resource_group.casefold(),
                body.gateway_name.casefold(),
            )
        )

    def _managed(self, arm: ArmClient, body: TargetSelect) -> dict[str, Any]:
        path = app_path(body.subscription_id, body.resource_group, body.gateway_name)
        try:
            found = arm.get_arm_json(path, api_version=self._settings.container_apps_api_version)
            if found is None:
                found = arm.get_arm_json(
                    cluster_path(body.subscription_id, body.resource_group, body.gateway_name),
                    api_version=CONTAINER_SERVICE_API_VERSION,
                )
            if found is None:
                # A deployment that failed before its Container App existed still left
                # resources; its deployment record, tagged like them, stands in for the app.
                found = arm.get_arm_json(
                    deployment_path(body.subscription_id, body.resource_group, body.gateway_name),
                    api_version=DEPLOYMENTS_API_VERSION,
                )
                if found is not None:
                    found = {
                        **found,
                        "name": body.gateway_name,
                        "id": "",
                        "location": "",
                        "deployment_only": True,
                    }
        except ArmError as error:
            raise arm_failure(error, "read this gateway") from error
        if found is None:
            raise ServiceError(
                404, f"Gateway {body.gateway_name} not found in {body.resource_group}"
            )
        if (found.get("tags") or {}).get(MANAGED_TAG) != MANAGED_TAG_VALUE:
            raise ServiceError(
                403,
                f"Only gateways this app deployed (tag {MANAGED_TAG}: {MANAGED_TAG_VALUE}) "
                "can be deleted here",
            )
        return found

    def _deleting(self, arm: ArmClient, resource_id: str, api_version: str) -> bool:
        found = arm.get_arm_json(resource_id, api_version=api_version)
        state = str(((found or {}).get("properties") or {}).get("provisioningState") or "")
        return state.casefold() in {"scheduledfordelete", "deleting"}

    def _resources(self, arm: ArmClient, body: TargetSelect, name: str) -> list[dict[str, Any]]:
        """The resources tagged as this gateway's, in the order they are deleted."""
        path = f"{group_path(body.subscription_id, body.resource_group)}/resources"
        try:
            items = list(
                arm.list_arm(
                    path,
                    api_version=RESOURCES_API_VERSION,
                    params={"$filter": f"tagName eq '{GATEWAY_TAG}' and tagValue eq '{name}'"},
                )
            )
        except ArmError as error:
            raise arm_failure(error, "list the gateway's resources") from error
        owned = [
            item
            for item in items
            if (item.get("tags") or {}).get(MANAGED_TAG) == MANAGED_TAG_VALUE
            and (item.get("tags") or {}).get(GATEWAY_TAG) == name
            and str(item.get("type") or "").casefold() in _DELETE_ORDER
        ]
        return sorted(owned, key=lambda item: _DELETE_ORDER[str(item["type"]).casefold()][0])

    def delete_check(self, body: TargetSelect) -> GatewayDeleteCheck:
        """What deleting the gateway would remove, for the confirmation."""
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        found = self._managed(arm, body)
        name = str(found.get("name") or body.gateway_name)
        return GatewayDeleteCheck(
            name=name,
            resource_group=resource_group_of(str(found.get("id") or "")) or body.resource_group,
            location=str(found.get("location") or ""),
            current=self._is_current(body),
            deployed=not found.get("deployment_only"),
            pools=None,
            resources=[
                f"{item.get('type')}/{item.get('name')}"
                for item in self._resources(arm, body, name)
            ],
        )

    def delete(self, body: GatewayDelete, actor: str) -> None:
        """Deletes a gateway this app deployed and its resources; not the resource group."""
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        found = self._managed(arm, body)
        name = str(found.get("name") or body.gateway_name)
        if body.confirm_name.strip().casefold() != name.casefold():
            raise ServiceError(400, f"Type the gateway name ({name}) to confirm")
        deleted: list[str] = []
        failed: list[tuple[str, ServiceError]] = []
        cluster: Mapping[str, Any] | None = None
        for item in self._resources(arm, body, name):
            resource = str(item.get("name"))
            kind = str(item["type"]).casefold()
            order, api_version = _DELETE_ORDER[kind]
            if kind == "microsoft.containerservice/managedclusters":
                cluster = item
            try:
                if order == 1 and self._deleting(arm, str(item["id"]), api_version):
                    # Asked before; Azure is still on it.
                    deleted.append(resource)
                    continue
                response = arm.request(
                    "DELETE",
                    arm.arm_url(str(item["id"])),
                    api_version=api_version,
                    allow_not_found=True,
                )
                # Only the Container App is waited for: its environment can't go before it.
                # Everything else, the environment included (Azure takes 10-20 minutes over
                # one), finishes on its own.
                if order == 0:
                    arm.wait(response, timeout_seconds=600)
                deleted.append(resource)
            except ArmError as error:
                failed.append((resource, arm_failure(error, f"delete {resource}")))
                if order == 0:
                    break
        if failed:
            done = ", ".join(deleted) or "nothing"
            reasons = "; ".join(f"{resource}: {error.message}" for resource, error in failed)
            raise ServiceError(failed[0][1].status, f"Could not delete {reasons}. Deleted: {done}.")
        try:
            # The deployment's record goes too, so the name can be deployed again cleanly.
            arm.request(
                "DELETE",
                arm.arm_url(deployment_path(body.subscription_id, body.resource_group, name)),
                api_version=DEPLOYMENTS_API_VERSION,
                allow_not_found=True,
            )
        except ArmError as error:
            logger.warning("Could not delete the deployment record of %s: %s", name, error.message)
        if cluster is not None:
            # The keys this machine kept for an AKS gateway go with it.
            location = str(cluster.get("location") or "")
            self._aks.forget(str(cluster.get("id") or ""), f"https://{aks_fqdn(name, location)}")
        logger.warning("%s deleted gateway %s and %s", actor, name, ", ".join(deleted))
        if self._is_current(body):
            self._targets.clear()
