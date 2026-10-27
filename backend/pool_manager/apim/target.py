"""Choosing the APIM service to manage, or creating one, from the setup wizard (locally and in
the public app). ``TargetChooser`` (target.py) saves the choice.

Every change here uses the signed-in user's own Azure rights:

- Creating registers the Microsoft.ApiManagement provider when needed, creates the resource group
  when it does not exist, and creates the service with ``If-None-Match: *``. It never changes an
  existing resource group or service.
- Deleting is limited to services this app created (the ``managed-by`` tag) and needs the name
  typed again. Azure soft-deletes the service and keeps it for 48 hours, during which it can be
  restored here; purging it for good is left to the portal or ``az apim deletedservice purge``.
  Resource groups are never deleted.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, get_args

from ..access import AUTHORIZATION_API_VERSION, Permissions, missing
from ..arm import ArmClient, ArmError, segment
from ..errors import ServiceError, arm_failure
from ..models import ResourceGroupView, TargetView
from ..target import TargetChooser, resource_group_of
from .access import APIM_READ_ACTIONS, APIM_WRITE_ACTIONS
from .models import (
    ApimCreate,
    ApimCreateOptions,
    ApimServiceView,
    ApimSku,
    DeletedServiceView,
    ServiceDelete,
    ServiceDeleteCheck,
    ServiceRestore,
    TargetSelect,
)

logger = logging.getLogger(__name__)

RESOURCES_API_VERSION = "2021-04-01"
PROVIDER = "Microsoft.ApiManagement"
# Marks services and resource groups this app created, like MANAGED_MARKER on backends.
MANAGED_TAG = "managed-by"
MANAGED_TAG_VALUE = "apim-backend-pool"

_WORKERS = 8
# Restoring ignores every property but ``restore``; the API still requires these fields.
_RESTORE_PLACEHOLDER = {
    "sku": {"name": "Developer", "capacity": 1},
    "publisherEmail": "restore@apim-backend-pool.invalid",
    "publisherName": "restore",
}
# Registering a resource provider usually takes well under a minute.
_REGISTER_POLLS = 12
_REGISTER_POLL_SECONDS = 5.0


def _service_path(subscription_id: str, resource_group: str, name: str) -> str:
    return (
        f"/subscriptions/{segment(subscription_id)}/resourceGroups/{segment(resource_group)}"
        f"/providers/{PROVIDER}/service/{segment(name)}"
    )


class ApimTargets:
    """The APIM half of the gateway step; ``TargetChooser`` has the shared half."""

    def __init__(self, targets: TargetChooser) -> None:
        self._targets = targets
        self._settings = targets.settings

    # -- reads -------------------------------------------------------------------------

    def services(self, tenant_id: str | None, subscription_id: str) -> list[ApimServiceView]:
        self._targets.require_local()
        arm = self._targets.arm(tenant_id)
        path = f"/subscriptions/{segment(subscription_id)}/providers/{PROVIDER}/service"
        try:
            items = list(arm.list_arm(path, api_version=self._settings.api_version))
        except ArmError as error:
            raise arm_failure(error, "list the APIM services of this subscription") from error
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=min(_WORKERS, len(items))) as executor:
            views = list(executor.map(lambda item: self._service_view(arm, item), items))
        return sorted(views, key=lambda view: view.name.casefold())

    def _service_view(self, arm: ArmClient, item: Mapping[str, Any]) -> ApimServiceView:
        service_id = str(item.get("id") or "")
        gaps: list[str] = []
        can_manage: bool | None = None
        try:
            permissions: Permissions = list(
                arm.list_arm(
                    f"{service_id}/providers/Microsoft.Authorization/permissions",
                    api_version=AUTHORIZATION_API_VERSION,
                )
            )
            gaps = missing(permissions, (*APIM_READ_ACTIONS, *APIM_WRITE_ACTIONS))
            can_manage = not gaps
        except ArmError as error:
            logger.info("Cannot read permissions on %s: %s", service_id, error.message)
        return ApimServiceView(
            id=service_id,
            name=str(item.get("name") or ""),
            resource_group=resource_group_of(service_id),
            location=str(item.get("location") or ""),
            sku=str((item.get("sku") or {}).get("name") or ""),
            provisioning_state=(item.get("properties") or {}).get("provisioningState"),
            managed=(item.get("tags") or {}).get(MANAGED_TAG) == MANAGED_TAG_VALUE,
            can_manage=can_manage,
            missing=gaps,
        )

    def create_options(self, tenant_id: str | None, subscription_id: str) -> ApimCreateOptions:
        self._targets.require_local()
        arm = self._targets.arm(tenant_id)
        subscription = f"/subscriptions/{segment(subscription_id)}"
        locations: dict[ApimSku, set[str]] = {sku: set() for sku in get_args(ApimSku)}
        try:
            groups = [
                ResourceGroupView(name=str(group["name"]), location=str(group["location"]))
                for group in arm.list_arm(
                    f"{subscription}/resourcegroups", api_version=RESOURCES_API_VERSION
                )
            ]
            # One entry per tier and region; restricted entries are not offered to this
            # subscription, and EUAP (canary) regions need special access.
            for item in arm.list_arm(
                f"{subscription}/providers/{PROVIDER}/skus", api_version=self._settings.api_version
            ):
                sku = item.get("name")
                if sku not in locations or item.get("restrictions"):
                    continue
                locations[sku].update(
                    str(location).casefold().replace(" ", "")
                    for location in item.get("locations") or []
                    if not str(location).casefold().endswith("euap")
                )
            provider = arm.get_arm_json(
                f"{subscription}/providers/{PROVIDER}", api_version=RESOURCES_API_VERSION
            )
        except ArmError as error:
            raise arm_failure(error, "read this subscription") from error
        return ApimCreateOptions(
            resource_groups=sorted(groups, key=lambda group: group.name.casefold()),
            locations={sku: sorted(values) for sku, values in locations.items()},
            provider_registered=str((provider or {}).get("registrationState")).casefold()
            == "registered",
        )

    # -- changes -----------------------------------------------------------------------

    def select(self, body: TargetSelect, actor: str) -> TargetView:
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        path = _service_path(body.subscription_id, body.resource_group, body.apim_name)
        try:
            found = arm.get_arm_json(path, api_version=self._settings.api_version)
        except ArmError as error:
            raise arm_failure(error, "read this APIM service") from error
        if found is None:
            raise ServiceError(
                404, f"APIM service {body.apim_name} not found in {body.resource_group}"
            )
        # ARM's casing, so the saved target matches the resource IDs ARM returns.
        resource_id = str(found.get("id") or path)
        target = {
            "subscription_id": body.subscription_id,
            "resource_group": resource_group_of(resource_id) or body.resource_group,
            "apim_name": str(found.get("name") or body.apim_name),
            "tenant_id": body.tenant_id or arm.subscription_tenant(body.subscription_id) or "",
        }
        self._save(target)
        logger.info("%s chose APIM service %s", actor, resource_id)
        return TargetView(
            product="apim",
            gateway_name=target["apim_name"],
            gateway_kind=None,
            subscription_id=target["subscription_id"],
            resource_group=target["resource_group"],
            gateway_url=None,
            tenant_id=target["tenant_id"] or None,
            source="browser" if self._settings.public else "saved",
            can_change=True,
        )

    def create(self, body: ApimCreate, actor: str) -> ApimServiceView:
        """Starts creating a service and selects it. The setup report follows provisioning."""
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        subscription = f"/subscriptions/{segment(body.subscription_id)}"
        group_path = f"{subscription}/resourceGroups/{segment(body.resource_group)}"
        path = _service_path(body.subscription_id, body.resource_group, body.name)
        try:
            self._ensure_provider(arm, subscription)
            if arm.get_arm_json(path, api_version=self._settings.api_version) is not None:
                raise ServiceError(
                    409, f"{body.name} already exists; choose it in the list instead"
                )
            check = arm.request(
                "POST",
                arm.arm_url(f"{subscription}/providers/{PROVIDER}/checkNameAvailability"),
                api_version=self._settings.api_version,
                json_body={"name": body.name},
            ).json()
            if not check.get("nameAvailable", True):
                raise ServiceError(409, str(check.get("message") or f"{body.name} is taken"))
            if arm.get_arm_json(group_path, api_version=RESOURCES_API_VERSION) is None:
                arm.request(
                    "PUT",
                    arm.arm_url(group_path),
                    api_version=RESOURCES_API_VERSION,
                    json_body={
                        "location": body.location,
                        "tags": {MANAGED_TAG: MANAGED_TAG_VALUE},
                    },
                )
                logger.info("%s created resource group %s", actor, group_path)
            response = arm.request(
                "PUT",
                arm.arm_url(path),
                api_version=self._settings.api_version,
                json_body={
                    "location": body.location,
                    "sku": {"name": body.sku, "capacity": 1},
                    # Members authenticate with APIM's managed identity.
                    "identity": {"type": "SystemAssigned"},
                    "tags": {MANAGED_TAG: MANAGED_TAG_VALUE},
                    "properties": {
                        "publisherEmail": body.publisher_email,
                        "publisherName": body.publisher_name,
                    },
                },
                headers={"If-None-Match": "*"},
            )
        except ArmError as error:
            raise arm_failure(error, "create this APIM service") from error
        logger.info("%s started creating APIM service %s (%s)", actor, path, body.sku)
        created: dict[str, Any] = {}
        if response.content and "json" in response.headers.get("content-type", ""):
            created = response.json()
        self._save(
            {
                "subscription_id": body.subscription_id,
                "resource_group": body.resource_group,
                "apim_name": body.name,
                "tenant_id": body.tenant_id or arm.subscription_tenant(body.subscription_id) or "",
            }
        )
        return ApimServiceView(
            id=str(created.get("id") or path),
            name=body.name,
            resource_group=body.resource_group,
            location=body.location,
            sku=body.sku,
            provisioning_state=(created.get("properties") or {}).get("provisioningState")
            or "Created",
            managed=True,
            can_manage=True,
        )

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

    def _save(self, target: Mapping[str, str]) -> None:
        self._targets.save("apim", target)

    # -- deleting and restoring ---------------------------------------------------------

    def _is_current(self, body: TargetSelect) -> bool:
        settings = self._settings
        return settings.has_target and (
            settings.subscription_id.casefold(),
            settings.resource_group.casefold(),
            settings.apim_name.casefold(),
        ) == (
            body.subscription_id.casefold(),
            body.resource_group.casefold(),
            body.apim_name.casefold(),
        )

    def _managed_service(self, arm: ArmClient, body: TargetSelect) -> dict[str, Any]:
        path = _service_path(body.subscription_id, body.resource_group, body.apim_name)
        try:
            found = arm.get_arm_json(path, api_version=self._settings.api_version)
        except ArmError as error:
            raise arm_failure(error, "read this APIM service") from error
        if found is None:
            raise ServiceError(
                404, f"APIM service {body.apim_name} not found in {body.resource_group}"
            )
        if (found.get("tags") or {}).get(MANAGED_TAG) != MANAGED_TAG_VALUE:
            raise ServiceError(
                403,
                f"Only APIM services this app created (tag {MANAGED_TAG}: {MANAGED_TAG_VALUE}) "
                "can be deleted here",
            )
        return found

    def delete_check(self, body: TargetSelect) -> ServiceDeleteCheck:
        """What deleting the service would remove, for the confirmation."""
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        found = self._managed_service(arm, body)
        service_id = str(found.get("id") or "")
        try:
            backends = list(
                arm.list_arm(f"{service_id}/backends", api_version=self._settings.pool_api_version)
            )
            apis = [
                item
                for item in arm.list_arm(
                    f"{service_id}/apis", api_version=self._settings.api_version
                )
                if ";rev=" not in str(item.get("name") or "")
            ]
        except ArmError as error:
            raise arm_failure(error, "read what this APIM service contains") from error
        pools = sum(1 for item in backends if (item.get("properties") or {}).get("type") == "Pool")
        return ServiceDeleteCheck(
            name=str(found.get("name") or body.apim_name),
            resource_group=resource_group_of(service_id) or body.resource_group,
            location=str(found.get("location") or ""),
            sku=str((found.get("sku") or {}).get("name") or ""),
            current=self._is_current(body),
            pools=pools,
            members=len(backends) - pools,
            apis=len(apis),
        )

    def delete(self, body: ServiceDelete, actor: str) -> None:
        """Soft-deletes a service this app created; Azure keeps it for 48 hours."""
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        found = self._managed_service(arm, body)
        name = str(found.get("name") or body.apim_name)
        if body.confirm_name.strip().casefold() != name.casefold():
            raise ServiceError(400, f"Type the service name ({name}) to confirm")
        path = _service_path(body.subscription_id, body.resource_group, body.apim_name)
        try:
            arm.request("DELETE", arm.arm_url(path), api_version=self._settings.api_version)
        except ArmError as error:
            raise arm_failure(error, "delete this APIM service") from error
        logger.warning("%s deleted APIM service %s (soft-delete, 48 h to restore)", actor, path)
        if self._is_current(body):
            self._targets.clear()

    def deleted_services(
        self, tenant_id: str | None, subscription_id: str
    ) -> list[DeletedServiceView]:
        self._targets.require_local()
        arm = self._targets.arm(tenant_id)
        path = f"/subscriptions/{segment(subscription_id)}/providers/{PROVIDER}/deletedservices"
        try:
            items = list(arm.list_arm(path, api_version=self._settings.api_version))
        except ArmError as error:
            raise arm_failure(error, "list recently deleted APIM services") from error
        views = []
        for item in items:
            record_id = str(item.get("id") or "")
            properties = item.get("properties") or {}
            service_id = str(properties.get("serviceId") or "")
            lowered = record_id.casefold().split("/")
            location = (
                record_id.split("/")[lowered.index("locations") + 1]
                if "locations" in lowered
                else ""
            )
            views.append(
                DeletedServiceView(
                    name=str(item.get("name") or ""),
                    location=location.casefold(),
                    display_location=str(item.get("location") or location),
                    resource_group=resource_group_of(service_id),
                    service_id=service_id,
                    deletion_date=properties.get("deletionDate"),
                    scheduled_purge_date=properties.get("scheduledPurgeDate"),
                )
            )
        return sorted(views, key=lambda view: view.deletion_date or "", reverse=True)

    def restore(self, body: ServiceRestore, actor: str) -> ApimServiceView:
        """Undeletes a soft-deleted service into its original resource group."""
        self._targets.require_changeable()
        arm = self._targets.arm(body.tenant_id)
        record_path = (
            f"/subscriptions/{segment(body.subscription_id)}/providers/{PROVIDER}"
            f"/locations/{segment(body.location)}/deletedservices/{segment(body.name)}"
        )
        try:
            record = arm.get_arm_json(record_path, api_version=self._settings.api_version)
            if record is None:
                raise ServiceError(404, f"{body.name} is no longer in Azure's deleted services")
            service_id = str((record.get("properties") or {}).get("serviceId") or "")
            group = resource_group_of(service_id)
            if not group:
                raise ServiceError(
                    502, f"Azure returned no original resource group for {body.name}"
                )
            path = _service_path(body.subscription_id, group, body.name)
            response = arm.request(
                "PUT",
                arm.arm_url(path),
                api_version=self._settings.api_version,
                json_body={
                    "location": record.get("location") or body.location,
                    "sku": _RESTORE_PLACEHOLDER["sku"],
                    "properties": {
                        "publisherEmail": _RESTORE_PLACEHOLDER["publisherEmail"],
                        "publisherName": _RESTORE_PLACEHOLDER["publisherName"],
                        "restore": True,
                    },
                },
            )
        except ArmError as error:
            raise arm_failure(error, "restore this APIM service") from error
        logger.warning("%s restored APIM service %s", actor, path)
        restored: dict[str, Any] = {}
        if response.content and "json" in response.headers.get("content-type", ""):
            restored = response.json()
        return ApimServiceView(
            id=str(restored.get("id") or path),
            name=body.name,
            resource_group=group,
            location=str(restored.get("location") or record.get("location") or body.location),
            sku=str((restored.get("sku") or {}).get("name") or ""),
            provisioning_state=(restored.get("properties") or {}).get("provisioningState"),
            managed=(restored.get("tags") or {}).get(MANAGED_TAG) == MANAGED_TAG_VALUE,
            can_manage=None,
        )
