"""What a gateway deployment needs from a region, checked before anything is created.

A deployment creates several resources at once; one that Azure refuses halfway (no PostgreSQL
for this subscription in the region, no quota left, a size the region doesn't offer) used to
leave the others behind, billed. ``CapacityCheck.problems`` reads, without changing anything:

- that the region offers every resource type the template creates (the providers' locations);
- PostgreSQL Flexible Server: the region's restrictions, the server version, the size the
  sizing plan picked, and the subscription's vCore quota for that size's family;
- Container Apps: room for one more managed environment in the region;
- AKS: the plan's node size, or the same size in the first node family the subscription may
  use there (Ddsv5, then Ddsv7, then Ddsv6), its zones and that family's vCPU quota for the
  most nodes it runs.

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
from .models import GatewayPlan
from .sizing import NODE_FAMILIES, node_family, node_in_family, postgres_size

logger = logging.getLogger(__name__)

RESOURCES_API_VERSION = "2021-04-01"
POSTGRES_API_VERSION = "2024-08-01"
# The server version in the gateway templates.
POSTGRES_VERSION = "16"
CONTAINER_APPS_API_VERSION = "2025-01-01"
COMPUTE_API_VERSION = "2024-07-01"
SKUS_API_VERSION = "2021-07-01"
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


def resource_types(plan: GatewayPlan) -> list[str]:
    """The resource types the template creates for this plan."""
    types = [
        "Microsoft.OperationalInsights/workspaces",
        "Microsoft.Insights/components",
        "Microsoft.ManagedIdentity/userAssignedIdentities",
        "Microsoft.DBforPostgreSQL/flexibleServers",
    ]
    if plan.redis_sku:
        types.append("Microsoft.Cache/redisEnterprise")
    if plan.platform == "aks":
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
    # The node size to deploy: the plan's, or the same size in the first of the node families
    # the subscription may use in the region (sizing.NODE_FAMILIES). None without AKS.
    node_size: str | None = None


def regional(plan: GatewayPlan, report: CapacityReport) -> GatewayPlan:
    """The plan with the node size the region check chose (the same, unless the plan's family
    isn't open to the subscription there)."""
    if plan.aks is None or not report.node_size or report.node_size == plan.aks.node_size:
        return plan
    return plan.model_copy(
        update={"aks": plan.aks.model_copy(update={"node_size": report.node_size})}
    )


class CapacityCheck:
    def problems(
        self,
        arm: ArmClient,
        subscription_id: str,
        location: str,
        plan: GatewayPlan,
        *,
        providers: Mapping[str, Mapping[str, Any] | None] | None = None,
    ) -> CapacityReport:
        """Everything in ``location`` that would make the deployment fail; empty if none."""
        subscription = f"/subscriptions/{segment(subscription_id)}"
        types = resource_types(plan)
        if providers is None:
            providers = self.providers(arm, subscription_id, types)
        report = CapacityReport()
        report.problems += self._offered(types, providers, location)
        checks: list[Callable[[], list[str]]] = [
            lambda: self._postgres(arm, subscription, location, plan.postgres_sku),
        ]
        if plan.aks is not None:
            aks = plan.aks

            def nodes_fit() -> list[str]:
                node_size, zones, problems = self._nodes(
                    arm, subscription, location, aks.node_size, aks.node_max * aks.node_vcpus
                )
                report.node_size = node_size
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
        plan: GatewayPlan,
        exclude: str,
    ) -> list[str]:
        """Regions, of a few common ones, where the same checks find nothing in the way."""
        providers = self.providers(arm, subscription_id, resource_types(plan))
        candidates = [r for r in ALTERNATIVE_REGIONS if r != _region(exclude)]
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            reports = list(
                pool.map(
                    lambda region: self.problems(
                        arm, subscription_id, region, plan, providers=providers
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

    def _postgres(self, arm: ArmClient, subscription: str, location: str, sku: str) -> list[str]:
        base = f"{subscription}/providers/Microsoft.DBforPostgreSQL/locations/{segment(location)}"
        size = postgres_size(sku)
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
            edition = next(
                (e for e in editions if str(e.get("name")).casefold() == size.edition.casefold()),
                None,
            )
            offered = {str(s.get("name")) for s in (edition or {}).get("supportedServerSkus") or []}
            if size.sku not in offered:
                problems.append(
                    f"PostgreSQL Flexible Server has no {size.edition_label} {size.sku} in "
                    f"{location}."
                )
        problems += _quota(
            (usages or {}).get("value") or [],
            {
                size.family: f"PostgreSQL {size.edition_label} vCores",
                "cores": "PostgreSQL vCores",
            },
            size.vcores,
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
        self, arm: ArmClient, subscription: str, location: str, node_size: str, vcpus: int
    ) -> tuple[str, list[str], list[str]]:
        """The node size to use (the plan's, or the same size in the first node family this
        subscription may use there and has quota for), its usable zones, and what keeps
        ``vcpus`` of it from fitting."""
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
            return node_size, [], []
        by_name = {
            str(item.get("name")).casefold(): item
            for item in skus
            if item.get("resourceType") == "virtualMachines"
        }
        family = node_family(node_size)
        candidates = (
            [node_in_family(node_size, i) for i in range(len(NODE_FAMILIES))]
            if family is not None
            else [node_size]
        )
        offered = [size for size in candidates if size.casefold() in by_name]
        if not offered:
            return node_size, [], [f"The AKS node size {node_size} isn't offered in {location}."]
        usable = [size for size in offered if _open(by_name[size.casefold()])]
        if not usable:
            others = [size for size in offered if size != node_size]
            alternative = f", nor {' or '.join(others)}" if others else ""
            return (
                node_size,
                [],
                [
                    f"The AKS node size {node_size} isn't available to this subscription in "
                    f"{location}{alternative}."
                ],
            )
        # The first open family with quota for the nodes; when none has, the first open one and
        # its shortfall.
        found = [self._fit(by_name[size.casefold()], usages, vcpus, location) for size in usable]
        index = next((i for i, (_, problems) in enumerate(found) if not problems), 0)
        zones, problems = found[index]
        return usable[index], zones, problems

    @staticmethod
    def _fit(
        sku: Mapping[str, Any], usages: Mapping[str, Any] | None, vcpus: int, location: str
    ) -> tuple[list[str], list[str]]:
        """A VM size's usable zones, and the quota that keeps ``vcpus`` of it from fitting."""
        restrictions = sku.get("restrictions") or []
        info = (sku.get("locationInfo") or [{}])[0]
        blocked = {
            zone
            for item in restrictions
            if item.get("type") == "Zone"
            for zone in (item.get("restrictionInfo") or {}).get("zones") or []
        }
        zones = sorted(str(zone) for zone in info.get("zones") or [] if zone not in blocked)
        quota_names = {"cores": "Total regional vCPUs"}
        family = node_family(str(sku.get("name") or ""))
        if family is not None:
            _, quota, label = NODE_FAMILIES[family]
            quota_names = {quota: label, **quota_names}
        problems = _quota((usages or {}).get("value") or [], quota_names, vcpus, location)
        return zones, problems


def _open(sku: Mapping[str, Any]) -> bool:
    """Whether the subscription may create this VM size in the region: not refused there, nor
    in every one of its zones."""
    restrictions = sku.get("restrictions") or []
    if any(
        item.get("type") == "Location" and item.get("reasonCode") == "NotAvailableForSubscription"
        for item in restrictions
    ):
        return False
    zones = set((sku.get("locationInfo") or [{}])[0].get("zones") or [])
    blocked = {
        zone
        for item in restrictions
        if item.get("type") == "Zone"
        for zone in (item.get("restrictionInfo") or {}).get("zones") or []
    }
    return not zones or not zones <= blocked


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
