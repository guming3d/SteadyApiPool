"""Deleting the resources a gateway deployment created.

Two callers: deleting a gateway (target.py, after the user typed its name), and cleaning up after
a deployment whose Azure resources failed, when it was started with ``cleanup_on_failure``. Both
delete only the resources tagged as this app's (``managed-by``) and as that gateway's
(``ai-gateway: <name>``), never the resource group: the deployment refuses a name whose tagged
resources are still there, so whatever carries the tag came from it.

The cleanup after a failure runs in the background (``GatewayCleanup``), because Azure refuses
some deletes until the resources they depend on are gone (a Container Apps environment while its
app exists) or while a resource is still being created after the deployment was stopped. It
keeps going, a pass at a time, until nothing tagged is left but what Azure is already deleting.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..arm import ArmClient, ArmError
from .aks_deploy import CONTAINER_SERVICE_API_VERSION
from .gateway import GATEWAY_TAG, MANAGED_TAG, MANAGED_TAG_VALUE
from .gateway_deploy import group_path
from .models import DeployCleanup

logger = logging.getLogger(__name__)

RESOURCES_API_VERSION = "2021-04-01"
# Delete order and API versions of what the templates create: the Container App first (its
# environment can't go while it has apps), the environment or the AKS cluster (which Azure takes
# many minutes over, so it finishes on its own), then the rest.
DELETE_ORDER = {
    "microsoft.app/containerapps": (0, "2025-01-01"),
    "microsoft.app/managedenvironments": (1, "2025-01-01"),
    "microsoft.containerservice/managedclusters": (1, CONTAINER_SERVICE_API_VERSION),
    "microsoft.dbforpostgresql/flexibleservers": (2, "2024-08-01"),
    "microsoft.cache/redisenterprise": (2, "2025-04-01"),
    "microsoft.managedidentity/userassignedidentities": (3, "2023-01-31"),
    "microsoft.insights/components": (3, "2020-02-02"),
    "microsoft.operationalinsights/workspaces": (4, "2023-09-01"),
    # A zone-redundant environment's: Azure refuses it until the environment is gone.
    "microsoft.network/virtualnetworks": (5, "2024-05-01"),
}
AKS_CLUSTER_TYPE = "microsoft.containerservice/managedclusters"
NETWORK_TYPE = "microsoft.network/virtualnetworks"
# How long the cleanup after a failure keeps trying, and how long it waits between passes.
CLEANUP_TIMEOUT_SECONDS = 45 * 60
CLEANUP_RETRY_SECONDS = 30.0


def tagged_resources(
    arm: ArmClient, subscription_id: str, resource_group: str, name: str
) -> list[dict[str, Any]]:
    """The resources tagged as this gateway's, in the order they are deleted."""
    items = arm.list_arm(
        f"{group_path(subscription_id, resource_group)}/resources",
        api_version=RESOURCES_API_VERSION,
        params={"$filter": f"tagName eq '{GATEWAY_TAG}' and tagValue eq '{name}'"},
    )
    owned = [
        item
        for item in items
        if (item.get("tags") or {}).get(MANAGED_TAG) == MANAGED_TAG_VALUE
        and (item.get("tags") or {}).get(GATEWAY_TAG) == name
        and str(item.get("type") or "").casefold() in DELETE_ORDER
    ]
    return sorted(owned, key=lambda item: DELETE_ORDER[str(item["type"]).casefold()][0])


def being_deleted(arm: ArmClient, item: Mapping[str, Any]) -> bool:
    """Azure is already deleting it (asked before; an environment takes 10-20 minutes)."""
    _, api_version = DELETE_ORDER[str(item["type"]).casefold()]
    found = arm.get_arm_json(str(item["id"]), api_version=api_version)
    state = str(((found or {}).get("properties") or {}).get("provisioningState") or "")
    return state.casefold() in {"scheduledfordelete", "deleting"}


@dataclass
class DeletePass:
    deleted: list[str] = field(default_factory=list)
    # Already being deleted by Azure; nothing more to ask.
    deleting: list[str] = field(default_factory=list)
    failed: list[tuple[str, ArmError]] = field(default_factory=list)
    cluster: Mapping[str, Any] | None = None


def delete_resources(arm: ArmClient, items: list[dict[str, Any]]) -> DeletePass:
    """Asks Azure to delete each of ``items`` (in ``tagged_resources``' order) once."""
    done = DeletePass()
    for item in items:
        resource = str(item.get("name"))
        kind = str(item["type"]).casefold()
        order, api_version = DELETE_ORDER[kind]
        if kind == AKS_CLUSTER_TYPE:
            done.cluster = item
        try:
            if order == 1 and being_deleted(arm, item):
                done.deleting.append(resource)
                continue
            response = arm.request(
                "DELETE",
                arm.arm_url(str(item["id"])),
                api_version=api_version,
                allow_not_found=True,
            )
            # Only the Container App is waited for: its environment can't go before it.
            # Everything else, the environment included, finishes on its own.
            if order == 0:
                arm.wait(response, timeout_seconds=600)
            done.deleted.append(resource)
        except ArmError as error:
            done.failed.append((resource, error))
            if order == 0:
                break
    return done


class GatewayCleanup:
    """Cleanups after failed deployments, one per deployment, each on its own thread.

    Process-wide (deps.py keeps one across gateway switches), so a status poll finds the
    cleanup an earlier poll started. ``run`` starts a job (tests run it inline).
    """

    def __init__(
        self,
        *,
        run: Callable[[Callable[[], None]], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run = run or _on_thread
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, DeployCleanup] = {}

    def get(self, key: str) -> DeployCleanup | None:
        with self._lock:
            job = self._jobs.get(key)
            return job.model_copy(deep=True) if job else None

    def start(
        self,
        key: str,
        arm: ArmClient,
        subscription_id: str,
        resource_group: str,
        name: str,
        *,
        on_done: Callable[[], None] | None = None,
    ) -> DeployCleanup:
        """Starts cleaning up after the deployment ``key`` unless that has begun already."""
        with self._lock:
            if key in self._jobs:
                return self._jobs[key].model_copy(deep=True)
            self._jobs[key] = DeployCleanup(state="running", deleted=[], deleting=[])
        logger.warning("Deleting what the failed deployment of gateway %s created", name)
        self._run(lambda: self._clean(key, arm, subscription_id, resource_group, name, on_done))
        return self.get(key) or DeployCleanup(state="running", deleted=[], deleting=[])

    def _update(self, key: str, **values: Any) -> None:
        with self._lock:
            self._jobs[key] = self._jobs[key].model_copy(update=values)

    def _clean(
        self,
        key: str,
        arm: ArmClient,
        subscription_id: str,
        resource_group: str,
        name: str,
        on_done: Callable[[], None] | None,
    ) -> None:
        deadline = self._clock() + CLEANUP_TIMEOUT_SECONDS
        deleted: list[str] = []
        quiet = 0
        while True:
            try:
                items = tagged_resources(arm, subscription_id, resource_group, name)
                # What was asked for before and is still listed, Azure is still deleting.
                waiting = [str(item.get("name")) for item in items if item.get("name") in deleted]
                result = delete_resources(
                    arm, [item for item in items if item.get("name") not in deleted]
                )
            except ArmError as error:
                self._update(key, state="failed", error=f"{error.code}: {error.message}")
                logger.warning("Cleaning up gateway %s failed: %s", name, error.message)
                return
            deleted += [item for item in result.deleted if item not in deleted]
            self._update(key, deleted=list(deleted), deleting=[*waiting, *result.deleting])
            # Nothing left to ask for, twice in a row: a resource Azure was still creating when
            # the deployment stopped can appear a little later.
            quiet = quiet + 1 if not result.deleted and not result.failed else 0
            if quiet >= 2:
                self._update(key, state="done")
                logger.warning(
                    "Deleted what the failed deployment of gateway %s created: %s",
                    name,
                    ", ".join(deleted) or "nothing was left",
                )
                if on_done is not None:
                    on_done()
                return
            if self._clock() > deadline:
                reasons = "; ".join(f"{item}: {error.message}" for item, error in result.failed)
                self._update(key, state="failed", error=reasons or "Azure kept the resources")
                return
            # Refused for now (a dependency still there, a resource still being created after
            # the deployment stopped), or asked: look again shortly.
            self._sleep(CLEANUP_RETRY_SECONDS)


def _on_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="gateway-cleanup", daemon=True).start()
