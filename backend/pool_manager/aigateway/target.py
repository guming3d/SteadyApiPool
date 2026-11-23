"""Choosing the AI Gateway to manage, or creating one, from the local setup wizard.
``TargetChooser`` (target.py) saves the choice once the gateway is one the app can work with now
(``_check_gateway``: not still being created, not failed); else the switch is refused with 424
and the reason, unless the user chooses it anyway (``force``).

Every change here uses the signed-in user's own Azure rights:

- Creating registers the Microsoft.ApiManagement provider when needed, creates the resource group
  when it does not exist, and creates the gateway (Standard, with a system-assigned identity,
  tagged ``managed-by: steadyapipool``) with ``If-None-Match: *``. It never changes an existing
  resource group or gateway.
- Deleting is limited to gateways this app created (the tag) and needs the name typed again.
  Resource groups are never deleted.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..access import AUTHORIZATION_API_VERSION, Permissions, missing
from ..arm import ArmClient, ArmError, segment
from ..errors import ServiceError, arm_failure
from ..models import ResourceGroupView, TargetView
from ..target import TargetChooser, resource_group_of
from .access import AIGATEWAY_READ_ACTIONS, AIGATEWAY_WRITE_ACTIONS
from .models import (
    GatewayCreate,
    GatewayCreateOptions,
    GatewayDelete,
    GatewayDeleteCheck,
    GatewayView,
    TargetSelect,
)
from .resources import MANAGED_TAG, WORKSPACE

logger = logging.getLogger(__name__)

RESOURCES_API_VERSION = "2021-04-01"
PROVIDER = "Microsoft.ApiManagement"
RESOURCE_TYPE = "aigateways"
SKU = "Standard"

_WORKERS = 8
# Registering a resource provider usually takes well under a minute.
_REGISTER_POLLS = 12
_REGISTER_POLL_SECONDS = 5.0


def gateway_path(subscription_id: str, resource_group: str, name: str) -> str:
    return (
        f"/subscriptions/{segment(subscription_id)}/resourceGroups/{segment(resource_group)}"
        f"/providers/{PROVIDER}/{RESOURCE_TYPE}/{segment(name)}"
    )


def is_managed_gateway(item: Mapping[str, Any]) -> bool:
    return (item.get("tags") or {}).get(MANAGED_TAG[0]) == MANAGED_TAG[1]


def short_location(value: object) -> str:
    """``East US 2`` and ``eastus2`` alike as ``eastus2``."""
    return str(value or "").casefold().replace(" ", "")


def _check_gateway(gateway: Mapping[str, Any]) -> None:
    """A gateway the app can work with now; else 424 with what is wrong."""
    name = str(gateway.get("name") or "")
    state = str((gateway.get("properties") or {}).get("provisioningState") or "")
    if state.casefold() in {"created", "creating", "activating", "accepted", "provisioning"}:
        raise ServiceError(424, f"{name} is still being created ({state}); it takes a few minutes.")
    if state.casefold() in {"failed", "canceled", "deleting"}:
        raise ServiceError(424, f"{name} is {state} in Azure, so the app can't work with it.")


class AiGatewayTargets:
    """The AI Gateway half of the gateway step; ``TargetChooser`` has the shared half."""

    def __init__(self, targets: TargetChooser) -> None:
        self._targets = targets
        self._settings = targets.settings
        self._version = targets.settings.aigateway_api_version

    # -- reads -------------------------------------------------------------------------

    def gateways(self, tenant_id: str | None, subscription_id: str) -> list[GatewayView]:
        self._targets.require_local()
        arm = self._targets.arm(tenant_id)
        path = f"/subscriptions/{segment(subscription_id)}/providers/{PROVIDER}/{RESOURCE_TYPE}"
        try:
            items = list(arm.list_arm(path, api_version=self._version))
        except ArmError as error:
            raise arm_failure(error, "list the AI Gateways of this subscription") from error
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=min(_WORKERS, len(items))) as executor:
            views = list(executor.map(lambda item: self._view(arm, item), items))
        return sorted(views, key=lambda view: view.name.casefold())

    def _view(self, arm: ArmClient, item: Mapping[str, Any]) -> GatewayView:
        gateway_id = str(item.get("id") or "")
        gaps: list[str] = []
        can_manage: bool | None = None
        try:
            permissions: Permissions = list(
                arm.list_arm(
                    f"{gateway_id}/providers/Microsoft.Authorization/permissions",
                    api_version=AUTHORIZATION_API_VERSION,
                )
            )
            gaps = missing(permissions, (*AIGATEWAY_READ_ACTIONS, *AIGATEWAY_WRITE_ACTIONS))
            can_manage = not gaps
        except ArmError as error:
            logger.info("Cannot read permissions on %s: %s", gateway_id, error.message)
        properties = item.get("properties") or {}
        host = (properties.get("frontend") or {}).get("defaultHostname")
        return GatewayView(
            id=gateway_id,
            name=str(item.get("name") or ""),
            resource_group=resource_group_of(gateway_id),
            location=short_location(item.get("location")),
            sku=str((item.get("sku") or {}).get("name") or SKU),
            url=f"https://{host}" if host else None,
            provisioning_state=properties.get("provisioningState"),
            managed=is_managed_gateway(item),
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
            provider = (
                arm.get_arm_json(
                    f"{subscription}/providers/{PROVIDER}", api_version=RESOURCES_API_VERSION
                )
                or {}
            )
        except ArmError as error:
            raise arm_failure(error, "read this subscription") from error
        locations = sorted(
            {
                short_location(location)
                for item in provider.get("resourceTypes") or []
                if str(item.get("resourceType") or "").casefold() == RESOURCE_TYPE
                for location in item.get("locations") or []
                # EUAP (canary) regions need special access.
                if not short_location(location).endswith("euap")
            }
        )
        return GatewayCreateOptions(
            resource_groups=sorted(groups, key=lambda group: group.name.casefold()),
            locations=locations,
            provider_registered=str(provider.get("registrationState")).casefold() == "registered",
        )

    # -- changes -----------------------------------------------------------------------

    def select(self, body: TargetSelect, actor: str) -> TargetView:
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        path = gateway_path(body.subscription_id, body.resource_group, body.aigateway_name)
        try:
            found = arm.get_arm_json(path, api_version=self._version)
        except ArmError as error:
            raise arm_failure(error, "read this AI Gateway") from error
        if found is None:
            raise ServiceError(
                404, f"AI Gateway {body.aigateway_name} not found in {body.resource_group}"
            )
        if not body.force:
            _check_gateway(found)
        resource_id = str(found.get("id") or path)
        target = {
            "subscription_id": body.subscription_id,
            "resource_group": resource_group_of(resource_id) or body.resource_group,
            "aigateway_name": str(found.get("name") or body.aigateway_name),
            "tenant_id": body.tenant_id or arm.subscription_tenant(body.subscription_id) or "",
        }
        self._targets.save("aigateway", target)
        logger.info("%s chose AI Gateway %s", actor, resource_id)
        return self._target_view(target)

    @staticmethod
    def _target_view(target: Mapping[str, str]) -> TargetView:
        return TargetView(
            product="aigateway",
            gateway_name=target["aigateway_name"],
            gateway_kind=None,
            subscription_id=target["subscription_id"],
            resource_group=target["resource_group"],
            gateway_url=None,
            tenant_id=target["tenant_id"] or None,
            source="saved",
            can_change=True,
        )

    def create(self, body: GatewayCreate, actor: str) -> GatewayView:
        """Starts creating a gateway and selects it. The setup report follows provisioning."""
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        subscription = f"/subscriptions/{segment(body.subscription_id)}"
        group_path = f"{subscription}/resourceGroups/{segment(body.resource_group)}"
        path = gateway_path(body.subscription_id, body.resource_group, body.name)
        try:
            self._ensure_provider(arm, subscription)
            if arm.get_arm_json(path, api_version=self._version) is not None:
                raise ServiceError(
                    409, f"{body.name} already exists; choose it in the list instead"
                )
            self._check_name(arm, subscription, body)
            if arm.get_arm_json(group_path, api_version=RESOURCES_API_VERSION) is None:
                arm.request(
                    "PUT",
                    arm.arm_url(group_path),
                    api_version=RESOURCES_API_VERSION,
                    json_body={"location": body.location, "tags": dict([MANAGED_TAG])},
                )
                logger.info("%s created resource group %s", actor, group_path)
            response = arm.request(
                "PUT",
                arm.arm_url(path),
                api_version=self._version,
                json_body={
                    "location": body.location,
                    "sku": {"name": SKU},
                    # Foundry members are called as the gateway's own identity.
                    "identity": {"type": "SystemAssigned"},
                    "tags": dict([MANAGED_TAG]),
                    "properties": {"publicNetworkAccess": "Enabled"},
                },
                headers={"If-None-Match": "*"},
            )
        except ArmError as error:
            raise arm_failure(error, "create this AI Gateway") from error
        logger.info("%s started creating AI Gateway %s", actor, path)
        created: dict[str, Any] = {}
        if response.content and "json" in response.headers.get("content-type", ""):
            created = response.json()
        self._targets.save(
            "aigateway",
            {
                "subscription_id": body.subscription_id,
                "resource_group": body.resource_group,
                "aigateway_name": body.name,
                "tenant_id": body.tenant_id or arm.subscription_tenant(body.subscription_id) or "",
            },
        )
        return GatewayView(
            id=str(created.get("id") or path),
            name=body.name,
            resource_group=body.resource_group,
            location=body.location,
            sku=SKU,
            url=None,
            provisioning_state=(created.get("properties") or {}).get("provisioningState")
            or "Creating",
            managed=True,
            can_manage=True,
        )

    def _check_name(self, arm: ArmClient, subscription: str, body: GatewayCreate) -> None:
        """The name becomes a host name, so it must be free; a check Azure can't answer is
        left to the create itself."""
        try:
            check = arm.request(
                "POST",
                arm.arm_url(
                    f"{subscription}/providers/{PROVIDER}/locations/{segment(body.location)}"
                    "/checkNameAvailability"
                ),
                api_version=self._version,
                json_body={"name": body.name, "type": f"{PROVIDER}/{RESOURCE_TYPE}"},
            ).json()
        except (ArmError, ValueError) as error:
            logger.info("Cannot check whether %s is free: %s", body.name, error)
            return
        if check.get("nameAvailable") is False:
            raise ServiceError(409, str(check.get("message") or f"{body.name} is taken"))

    def _ensure_provider(self, arm: ArmClient, subscription: str) -> None:
        path = f"{subscription}/providers/{PROVIDER}"

        def state() -> str:
            found = arm.get_arm_json(path, api_version=RESOURCES_API_VERSION) or {}
            return str(found.get("registrationState") or "").casefold()

        current = state()
        if current == "registered":
            return
        if current != "registering":
            arm.request("POST", arm.arm_url(f"{path}/register"), api_version=RESOURCES_API_VERSION)
        for _ in range(_REGISTER_POLLS):
            self._targets.sleep(_REGISTER_POLL_SECONDS)
            if state() == "registered":
                return
        raise ServiceError(
            503, f"{PROVIDER} is still being registered in this subscription; try again shortly"
        )

    # -- deleting ----------------------------------------------------------------------

    def _is_current(self, body: TargetSelect) -> bool:
        settings = self._settings
        return settings.has_target and (
            settings.subscription_id.casefold(),
            settings.resource_group.casefold(),
            settings.aigateway_name.casefold(),
        ) == (
            body.subscription_id.casefold(),
            body.resource_group.casefold(),
            body.aigateway_name.casefold(),
        )

    def _managed_gateway(self, arm: ArmClient, body: TargetSelect) -> dict[str, Any]:
        path = gateway_path(body.subscription_id, body.resource_group, body.aigateway_name)
        try:
            found = arm.get_arm_json(path, api_version=self._version)
        except ArmError as error:
            raise arm_failure(error, "read this AI Gateway") from error
        if found is None:
            raise ServiceError(
                404, f"AI Gateway {body.aigateway_name} not found in {body.resource_group}"
            )
        if not is_managed_gateway(found):
            raise ServiceError(
                403,
                f"Only AI Gateways this app created (tag {MANAGED_TAG[0]}: {MANAGED_TAG[1]}) "
                "can be deleted here",
            )
        return found

    def delete_check(self, body: TargetSelect) -> GatewayDeleteCheck:
        """What deleting the gateway would remove, for the confirmation."""
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        found = self._managed_gateway(arm, body)
        gateway_id = str(found.get("id") or "")
        try:
            aliases = list(
                arm.list_arm(f"{gateway_id}{WORKSPACE}/aliases", api_version=self._version)
            )
            models = list(
                arm.list_arm(f"{gateway_id}{WORKSPACE}/models", api_version=self._version)
            )
            keys = list(arm.list_arm(f"{gateway_id}/apiKeys", api_version=self._version))
        except ArmError as error:
            raise arm_failure(error, "read what this AI Gateway contains") from error
        return GatewayDeleteCheck(
            name=str(found.get("name") or body.aigateway_name),
            resource_group=resource_group_of(gateway_id) or body.resource_group,
            location=short_location(found.get("location")),
            sku=str((found.get("sku") or {}).get("name") or SKU),
            current=self._is_current(body),
            pools=len(aliases),
            members=len(models),
            # The built-in master key goes with the gateway but isn't one anybody created.
            keys=sum(1 for key in keys if str(key.get("name") or "").casefold() != "master"),
        )

    def delete(self, body: GatewayDelete, actor: str) -> None:
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        found = self._managed_gateway(arm, body)
        name = str(found.get("name") or body.aigateway_name)
        if body.confirm_name.strip().casefold() != name.casefold():
            raise ServiceError(400, f"Type the gateway name ({name}) to confirm")
        path = gateway_path(body.subscription_id, body.resource_group, body.aigateway_name)
        try:
            arm.request("DELETE", arm.arm_url(path), api_version=self._version)
        except ArmError as error:
            raise arm_failure(error, "delete this AI Gateway") from error
        logger.warning("%s deleted AI Gateway %s", actor, path)
        if self._is_current(body):
            self._targets.clear()
