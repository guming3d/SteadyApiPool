"""What a gateway deployment needs from a region, checked before anything is created.

A deployment creates several resources at once; one that Azure refuses halfway (no PostgreSQL
for this subscription in the region, no quota left, a size the region doesn't offer) used to
leave the others behind, billed. ``CapacityCheck.problems`` reads, without changing anything:

- that the region offers every resource type the template creates (the providers' locations);
- PostgreSQL Flexible Server: the region's restrictions, the server version, the Burstable size
  the profile uses, and the subscription's vCore quota for that family;
- Container Apps: room for one more managed environment in the region;
- AKS: the node size (and its zones) and the vCPU quota for the nodes.

Every problem is reported at once, so the user can pick another region in one go. A check that
can't be read (no permission, a provider that doesn't answer) is skipped: the deployment's own
validation and the cleanup after a failure still stand behind it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..arm import ArmClient, ArmError, segment
from .gateway_template import AKS_NODE_SIZE
from .models import GatewayPlatform, GatewayProfile

logger = logging.getLogger(__name__)

RESOURCES_API_VERSION = "2021-04-01"
POSTGRES_API_VERSION = "2024-08-01"
# The server version in the gateway templates.
POSTGRES_VERSION = "16"
CONTAINER_APPS_API_VERSION = "2025-01-01"
COMPUTE_API_VERSION = "2024-07-01"
SKUS_API_VERSION = "2021-07-01"
# The Burstable sizes the templates use, their vCores, and the family their quota counts in.
POSTGRES_SKUS: dict[GatewayProfile, tuple[str, int]] = {
    "dev": ("Standard_B1ms", 1),
    "production": ("Standard_B2s", 2),
}
POSTGRES_EDITION = "Burstable"
POSTGRES_FAMILY = "standardBSFamily"
# The AKS node size's quota family, and vCPUs per node (Standard_D4ds_v5).
VM_FAMILY = "standardDDSv5Family"
VCPUS_PER_NODE = 4
# Offered instead when the chosen region can't take the gateway.
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

# What each resource type is called in a message.
_LABELS = {
    "Microsoft.OperationalInsights/workspaces": "Log Analytics",
    "Microsoft.Insights/components": "Application Insights",
    "Microsoft.ManagedIdentity/userAssignedIdentities": "managed identities",
    "Microsoft.DBforPostgreSQL/flexibleServers": "PostgreSQL Flexible Server",
    "Microsoft.Cache/redisEnterprise": "Azure Managed Redis",
    "Microsoft.App/managedEnvironments": "Container Apps environments",
    "Microsoft.App/containerApps": "Container Apps",
    "Microsoft.ContainerService/managedClusters": "AKS clusters",
}


def resource_types(platform: GatewayPlatform, profile: GatewayProfile) -> list[str]:
    """The resource types the template creates for this platform and profile."""
    types = [
        "Microsoft.OperationalInsights/workspaces",
        "Microsoft.Insights/components",
        "Microsoft.ManagedIdentity/userAssignedIdentities",
        "Microsoft.DBforPostgreSQL/flexibleServers",
    ]
    if profile == "production":
        types.append("Microsoft.Cache/redisEnterprise")
    if platform == "aks":
        types.append("Microsoft.ContainerService/managedClusters")
    else:
        types += ["Microsoft.App/managedEnvironments", "Microsoft.App/containerApps"]
    return types


def _region(name: object) -> str:
    """ARM's display names ("East US 2") and location names ("eastus2") alike."""
    return str(name).casefold().replace(" ", "")


@dataclass
class CapacityReport:
    problems: list[str] = field(default_factory=list)
    # The AKS node size's usable availability zones in the region.
    zones: list[str] = field(default_factory=list)


class CapacityCheck:
    def problems(
        self,
        arm: ArmClient,
        subscription_id: str,
        location: str,
        platform: GatewayPlatform,
        profile: GatewayProfile,
        *,
        providers: Mapping[str, Mapping[str, Any] | None] | None = None,
    ) -> CapacityReport:
        """Everything in ``location`` that would make the deployment fail; empty if none."""
        subscription = f"/subscriptions/{segment(subscription_id)}"
        types = resource_types(platform, profile)
        if providers is None:
            providers = self.providers(arm, subscription_id, types)
        report = CapacityReport()
        report.problems += self._offered(types, providers, location)
        checks: list[Callable[[], list[str]]] = [
            lambda: self._postgres(arm, subscription, location, profile),
        ]
        if platform == "aks":
            nodes = 3 if profile == "production" else 1

            def nodes_fit() -> list[str]:
                zones, problems = self._nodes(arm, subscription, location, nodes)
                report.zones = zones
                return problems

            checks.append(nodes_fit)
        else:
            checks.append(lambda: self._environments(arm, subscription, location))
        with ThreadPoolExecutor(max_workers=len(checks)) as pool:
            for found in pool.map(lambda check: check(), checks):
                report.problems += found
        return report

    def providers(
        self, arm: ArmClient, subscription_id: str, types: Iterable[str]
    ) -> dict[str, dict[str, Any] | None]:
        """Each namespace's provider, with its resource types and their locations."""
        namespaces = sorted({kind.split("/", 1)[0] for kind in types})
        subscription = f"/subscriptions/{segment(subscription_id)}"

        def read(namespace: str) -> dict[str, Any] | None:
            try:
                return arm.get_arm_json(
                    f"{subscription}/providers/{namespace}", api_version=RESOURCES_API_VERSION
                )
            except ArmError as error:
                logger.info("Cannot read provider %s: %s", namespace, error.message)
                return None

        with ThreadPoolExecutor(max_workers=len(namespaces)) as pool:
            return dict(zip(namespaces, pool.map(read, namespaces), strict=True))

    def open_regions(
        self,
        arm: ArmClient,
        subscription_id: str,
        platform: GatewayPlatform,
        profile: GatewayProfile,
        exclude: str,
    ) -> list[str]:
        """Regions, of a few common ones, where the same checks find nothing in the way."""
        providers = self.providers(arm, subscription_id, resource_types(platform, profile))
        candidates = [r for r in ALTERNATIVE_REGIONS if r != _region(exclude)]
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            reports = list(
                pool.map(
                    lambda region: self.problems(
                        arm, subscription_id, region, platform, profile, providers=providers
                    ),
                    candidates,
                )
            )
        return [
            region
            for region, report in zip(candidates, reports, strict=True)
            if not report.problems
        ]

    # -- the checks ---------------------------------------------------------------------

    def _offered(
        self,
        types: list[str],
        providers: Mapping[str, Mapping[str, Any] | None],
        location: str,
    ) -> list[str]:
        problems = []
        for kind in types:
            namespace, _, resource_type = kind.partition("/")
            provider = providers.get(namespace) or {}
            listed = next(
                (
                    item
                    for item in provider.get("resourceTypes") or []
                    if str(item.get("resourceType")).casefold() == resource_type.casefold()
                ),
                None,
            )
            # Not listed (or the provider couldn't be read): nothing to go by.
            if listed is None or not listed.get("locations"):
                continue
            if _region(location) not in {_region(item) for item in listed["locations"]}:
                problems.append(f"Azure doesn't offer {_LABELS.get(kind, kind)} in {location}.")
        return problems

    def _postgres(
        self, arm: ArmClient, subscription: str, location: str, profile: GatewayProfile
    ) -> list[str]:
        base = f"{subscription}/providers/Microsoft.DBforPostgreSQL/locations/{segment(location)}"
        sku, vcores = POSTGRES_SKUS[profile]
        try:
            found = arm.get_arm_json(f"{base}/capabilities", api_version=POSTGRES_API_VERSION)
            usages = arm.get_arm_json(
                f"{base}/resourceType/flexibleServers/usages", api_version=POSTGRES_API_VERSION
            )
        except ArmError as error:
            logger.info("Cannot read PostgreSQL capacity in %s: %s", location, error.message)
            return []
        problems = []
        capabilities = (found or {}).get("value") or []
        capability = (capabilities[0] or {}) if capabilities else {}
        versions = {
            str(item.get("name")) for item in capability.get("supportedServerVersions") or []
        }
        if capability and (
            str(capability.get("restricted") or "").casefold() == "enabled"
            or POSTGRES_VERSION not in versions
        ):
            reason = str(capability.get("reason") or "").strip()
            problems.append(
                "The gateway's database (PostgreSQL Flexible Server) can't be created in "
                f"{location} with this subscription: "
                f"{reason or f'PostgreSQL {POSTGRES_VERSION} is not offered there'}"
            )
            return problems
        editions = capability.get("supportedServerEditions") or []
        if editions:
            burstable = next(
                (e for e in editions if str(e.get("name")).casefold() == POSTGRES_EDITION.lower()),
                None,
            )
            offered = {
                str(s.get("name")) for s in (burstable or {}).get("supportedServerSkus") or []
            }
            if sku not in offered:
                problems.append(
                    f"PostgreSQL Flexible Server has no {POSTGRES_EDITION} {sku} in {location}."
                )
        problems += _quota(
            (usages or {}).get("value") or [],
            {POSTGRES_FAMILY: "PostgreSQL Burstable vCores", "cores": "PostgreSQL vCores"},
            vcores,
            location,
        )
        return problems

    def _environments(self, arm: ArmClient, subscription: str, location: str) -> list[str]:
        try:
            found = arm.get_arm_json(
                f"{subscription}/providers/Microsoft.App/locations/{segment(location)}/usages",
                api_version=CONTAINER_APPS_API_VERSION,
            )
        except ArmError as error:
            logger.info("Cannot read Container Apps quota in %s: %s", location, error.message)
            return []
        return _quota(
            (found or {}).get("value") or [],
            {"ManagedEnvironmentCount": "Container Apps environments"},
            1,
            location,
        )

    def _nodes(
        self, arm: ArmClient, subscription: str, location: str, nodes: int
    ) -> tuple[list[str], list[str]]:
        """The node size's usable zones, and what keeps the nodes from fitting."""
        try:
            skus = list(
                arm.list_arm(
                    f"{subscription}/providers/Microsoft.Compute/skus",
                    api_version=SKUS_API_VERSION,
                    params={"$filter": f"location eq '{location}'"},
                )
            )
            usages = arm.get_arm_json(
                f"{subscription}/providers/Microsoft.Compute/locations/{segment(location)}/usages",
                api_version=COMPUTE_API_VERSION,
            )
        except ArmError as error:
            logger.info("Cannot read the node size's capacity in %s: %s", location, error.message)
            return [], []
        sku = next(
            (
                item
                for item in skus
                if item.get("resourceType") == "virtualMachines"
                and item.get("name") == AKS_NODE_SIZE
            ),
            None,
        )
        if sku is None:
            return [], [f"The AKS node size {AKS_NODE_SIZE} isn't offered in {location}."]
        restrictions = sku.get("restrictions") or []
        if any(
            item.get("type") == "Location"
            and item.get("reasonCode") == "NotAvailableForSubscription"
            for item in restrictions
        ):
            return [], [
                f"The AKS node size {AKS_NODE_SIZE} isn't available to this subscription in "
                f"{location}."
            ]
        info = (sku.get("locationInfo") or [{}])[0]
        blocked = {
            zone
            for item in restrictions
            if item.get("type") == "Zone"
            for zone in (item.get("restrictionInfo") or {}).get("zones") or []
        }
        zones = sorted(str(zone) for zone in info.get("zones") or [] if zone not in blocked)
        problems = _quota(
            (usages or {}).get("value") or [],
            {VM_FAMILY: "DDSv5 vCPUs", "cores": "Total regional vCPUs"},
            nodes * VCPUS_PER_NODE,
            location,
        )
        return zones, problems


def _quota(
    usages: list[dict[str, Any]], names: Mapping[str, str], needed: int, location: str
) -> list[str]:
    """Quotas among ``usages`` (by name, case-insensitive) with less than ``needed`` free."""
    wanted = {name.casefold(): label for name, label in names.items()}
    problems = []
    for usage in usages:
        name = str((usage.get("name") or {}).get("value") or "").casefold()
        if name not in wanted:
            continue
        free = int(float(usage.get("limit") or 0) - float(usage.get("currentValue") or 0))
        if free < needed:
            problems.append(
                f"Not enough quota in {location}: {wanted[name]} has {max(free, 0)} free, the "
                f"gateway needs {needed}. Request more in the portal (Quotas) or choose another "
                "region."
            )
    return problems
