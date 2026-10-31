"""What a LiteLLM gateway costs.

Spend is filtered to the resources tagged ``ai-gateway: <name>``, which the deploy templates put
on everything they create: in the gateway's resource group, or for AKS in the subscription (the
cluster's nodes and load balancer are in its node resource group, which AKS tags like the
cluster).

The estimate is for what the gateway runs: from its sizing plan before it exists
(``plan_price``), else from what was deployed. Container Apps replicas (busy at the active rate,
quiet at the idle rate, so a range), or AKS nodes (from the fewest to the most the autoscaler
runs) and the Standard tier; then Postgres Flexible Server and Azure Managed Redis at the sizes
the plan picked (the tags ``ai-gateway-postgres`` and ``ai-gateway-redis``; a gateway deployed
before them by its profile). It leaves out the monthly free grant, log ingestion and discounts.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx

from ..arm import ArmClient, ArmError, segment
from ..config import Settings
from ..cost import HOURS_PER_MONTH, CostReader, meter, region_name
from .aks_deploy import CONTAINER_SERVICE_API_VERSION
from .gateway import GATEWAY_TAG, GatewayInfo, GatewayUnavailable
from .gateway_template import POSTGRES_HA_TAG
from .models import CostItem, GatewayPlan, ListPrice, ServiceCost
from .service import LiteLLMService
from .sizing import SIDECAR, postgres_size

logger = logging.getLogger(__name__)

SECONDS_PER_MONTH = HOURS_PER_MONTH * 3600
POSTGRES_STORAGE_GB = 32
# Azure Managed Redis runs two instances (high availability).
REDIS_INSTANCES = 2
# What a gateway deployed before the size tags runs, by its profile: Postgres and Redis.
_PROFILE_SIZES: dict[str, tuple[str, str | None]] = {
    "dev": ("Standard_B1ms", None),
    "production": ("Standard_B2s", "Balanced_B0"),
}
DEFAULT_NODE_SIZE = "Standard_D4ds_v5"
# One query per service; each lists every meter of the region (VMs: only the node size).
_SERVICES = {
    "aca": "Azure Container Apps",
    "postgres": "Azure Database for PostgreSQL",
    "redis": "Redis Cache",
    "vm": "Virtual Machines",
    "aks": "Azure Kubernetes Service",
}
_ACA_SERVICES = ("aca", "postgres", "redis")
_AKS_SERVICES = ("vm", "aks", "postgres", "redis")

Prices = dict[str, list[dict[str, Any]]]


def _postgres_meter(sku: str) -> tuple[str, str, str]:
    """The Retail Prices API's compute meter for a server size: each Burstable size has a meter
    of its own ("B1MS"), General Purpose sizes are an "<n> vCore" SKU of their series' meter.
    Returns (SKU name, product name contains, what the estimate calls it)."""
    short = sku.removeprefix("Standard_")
    size = postgres_size(sku)
    if size.edition == "Burstable":
        return short.upper(), "Burstable BS Series Compute", short
    return f"{size.vcores} vCore", "General Purpose Ddsv5 Series Compute", short


def sizes_from_tags(tags: Mapping[str, Any]) -> tuple[str, str | None]:
    """The Postgres and Redis sizes a deployed gateway runs, from its tags."""
    profile = str(tags.get("ai-gateway-profile") or "dev")
    postgres, redis = _PROFILE_SIZES.get(profile, _PROFILE_SIZES["dev"])
    if tags.get("ai-gateway-postgres"):
        postgres = str(tags["ai-gateway-postgres"])
        redis = str(tags.get("ai-gateway-redis") or "") or None
    return postgres, redis


def postgres_ha_from_tags(tags: Mapping[str, Any]) -> bool:
    """Whether a deployed gateway's database has a standby (none before the tag existed)."""
    return str(tags.get(POSTGRES_HA_TAG) or "").casefold() == "zoneredundant"


def _database_items(
    prices: Prices, postgres_sku: str, redis_sku: str | None, postgres_ha: bool = False
) -> list[CostItem]:
    """PostgreSQL and Redis: the same on Container Apps and AKS. A high availability standby
    is billed like the server it stands by for (compute and storage)."""
    items: list[CostItem] = []
    sku_name, product, label = _postgres_meter(postgres_sku)
    postgres = prices.get("postgres") or []
    compute = meter(
        postgres, lambda i: i.get("skuName") == sku_name and product in str(i.get("productName"))
    )
    storage = meter(
        postgres,
        lambda i: (
            i.get("meterName") == "Storage Data Stored" and "Flex" in str(i.get("productName"))
        ),
    )
    if compute is not None:
        servers = 2 if postgres_ha else 1
        monthly = (compute * HOURS_PER_MONTH + (storage or 0) * POSTGRES_STORAGE_GB) * servers
        items.append(
            CostItem(
                kind="postgres",
                detail=f"{label} + {POSTGRES_STORAGE_GB} GB"
                + (f" x {servers} (zone-redundant HA)" if postgres_ha else ""),
                monthly_low=round(monthly, 2),
                monthly_high=round(monthly, 2),
            )
        )
    if redis_sku:
        short = redis_sku.rsplit("_", 1)[-1]
        redis = meter(
            prices.get("redis") or [],
            lambda i: (
                i.get("skuName") == short
                and "Managed Redis - Balanced" in str(i.get("productName"))
            ),
        )
        if redis is not None:
            monthly = redis * HOURS_PER_MONTH * REDIS_INSTANCES
            items.append(
                CostItem(
                    kind="redis",
                    detail=f"Balanced {short} x {REDIS_INSTANCES}",
                    monthly_low=round(monthly, 2),
                    monthly_high=round(monthly, 2),
                )
            )
    return items


def _total(items: list[CostItem], currency: str) -> ListPrice:
    return ListPrice(
        items=items,
        monthly_low=round(sum(item.monthly_low for item in items), 2),
        monthly_high=round(sum(item.monthly_high for item in items), 2),
        currency=currency,
    )


def aks_list_price(
    prices: Prices,
    *,
    node_size: str,
    nodes: tuple[int, int],
    standard_tier: bool,
    postgres_sku: str,
    redis_sku: str | None,
    postgres_ha: bool = False,
) -> ListPrice | None:
    """An AKS gateway: its Linux nodes (from the fewest to the most the autoscaler runs), the
    Standard tier's uptime SLA, PostgreSQL and Redis; None without a node price."""
    vms = prices.get("vm") or []
    node = meter(
        vms,
        # By ARM size: the meter is "D4ds v5" for Ddsv5 but "Standard_D4ds_v7" for Ddsv7.
        lambda i: (
            str(i.get("armSkuName")).casefold() == node_size.casefold()
            and not any(word in str(i.get("meterName")) for word in ("Spot", "Low Priority"))
            and "Windows" not in str(i.get("productName"))
            and i.get("unitOfMeasure") == "1 Hour"
        ),
    )
    if node is None:
        return None
    low, high = nodes
    items = [
        CostItem(
            kind="aks_nodes",
            detail=f"{node_size} x {low}-{high}" if high != low else f"{node_size} x {low}",
            monthly_low=round(node * HOURS_PER_MONTH * low, 2),
            monthly_high=round(node * HOURS_PER_MONTH * high, 2),
        )
    ]
    if standard_tier:
        tier = meter(
            prices.get("aks") or [],
            lambda i: (
                i.get("skuName") == "Standard" and i.get("meterName") == "Standard Uptime SLA"
            ),
        )
        if tier is not None:
            items.append(
                CostItem(
                    kind="aks_tier",
                    detail="Standard",
                    monthly_low=round(tier * HOURS_PER_MONTH, 2),
                    monthly_high=round(tier * HOURS_PER_MONTH, 2),
                )
            )
    items += _database_items(prices, postgres_sku, redis_sku, postgres_ha)
    currency = str(next((item.get("currencyCode") for item in vms), None) or "USD")
    return _total(items, currency)


def aca_list_price(
    prices: Prices,
    *,
    cpu: float,
    memory: float,
    replicas: tuple[int, int],
    postgres_sku: str,
    redis_sku: str | None,
    postgres_ha: bool = False,
) -> ListPrice | None:
    """A Container Apps gateway: its replicas (``cpu`` and ``memory`` for all containers of
    one), PostgreSQL and Redis; None if a meter it needs is missing."""
    aca = prices.get("aca") or []

    def standard(name: str) -> float | None:
        return meter(aca, lambda i: i.get("skuName") == "Standard" and i.get("meterName") == name)

    cpu_active = standard("Standard vCPU Active Usage")
    cpu_idle = standard("Standard vCPU Idle Usage")
    mem_active = standard("Standard Memory Active Usage")
    mem_idle = standard("Standard Memory Idle Usage")
    if cpu_active is None or cpu_idle is None or mem_active is None or mem_idle is None:
        return None
    low, high = replicas
    items = [
        CostItem(
            kind="container_apps",
            detail=f"{cpu:g} vCPU, {memory:g} GiB x {low}-{high} replicas"
            if high != low
            else f"{cpu:g} vCPU, {memory:g} GiB x {low} replica",
            # Quiet: the minimum replicas at the idle rate. Busy: every replica at the active rate.
            monthly_low=round((cpu * cpu_idle + memory * mem_idle) * SECONDS_PER_MONTH * low, 2),
            monthly_high=round(
                (cpu * cpu_active + memory * mem_active) * SECONDS_PER_MONTH * high, 2
            ),
        )
    ]
    items += _database_items(prices, postgres_sku, redis_sku, postgres_ha)
    currency = str(next((item.get("currencyCode") for item in aca), None) or "USD")
    return _total(items, currency)


def list_price(prices: Prices, info: GatewayInfo) -> ListPrice | None:
    """A deployed Container Apps gateway's estimate, from the app and its tags."""
    if info.cpu is None:
        return None
    low = max(info.min_replicas or 1, 1)
    high = max(info.max_replicas or low, low)
    postgres, redis = sizes_from_tags(info.tags)
    return aca_list_price(
        prices,
        cpu=info.cpu,
        memory=info.memory_gib or 0.0,
        replicas=(low, high),
        postgres_sku=postgres,
        redis_sku=redis,
        postgres_ha=postgres_ha_from_tags(info.tags),
    )


def plan_price(prices: Prices, plan: GatewayPlan) -> ListPrice | None:
    """What a plan would cost, before it is deployed."""
    if plan.aks is not None:
        return aks_list_price(
            prices,
            node_size=plan.aks.node_size,
            nodes=(plan.aks.node_min, plan.aks.node_max),
            standard_tier=plan.aks.tier == "Standard",
            postgres_sku=plan.postgres_sku,
            redis_sku=plan.redis_sku,
            postgres_ha=plan.postgres_ha,
        )
    return aca_list_price(
        prices,
        cpu=plan.replica_cpu + SIDECAR[0],
        memory=plan.replica_memory_gib + SIDECAR[1],
        replicas=(plan.min_replicas, plan.max_replicas),
        postgres_sku=plan.postgres_sku,
        redis_sku=plan.redis_sku,
        postgres_ha=plan.postgres_ha,
    )


def _fetch(
    reader: CostReader, keys: tuple[str, ...], region: str, refresh: bool, node_size: str
) -> Prices:
    vm_filter = f" and armSkuName eq '{node_size}'"
    return {
        key: reader.retail_prices(_SERVICES[key], region, refresh, vm_filter if key == "vm" else "")
        for key in keys
    }


class PlanPrices(CostReader):
    """Retail prices for a sizing plan in a region, before the gateway exists."""

    def price(self, plan: GatewayPlan, location: str) -> ListPrice | None:
        keys = _AKS_SERVICES if plan.aks is not None else _ACA_SERVICES
        node_size = plan.aks.node_size if plan.aks is not None else DEFAULT_NODE_SIZE
        try:
            return plan_price(_fetch(self, keys, region_name(location), False, node_size), plan)
        except (httpx.HTTPError, ValueError) as error:
            logger.info("Cannot read Azure retail prices: %s", error)
            return None

    def spend_filter(self, user: ArmClient) -> dict[str, Any]:
        raise NotImplementedError("a plan has no spend yet")


class LiteLLMCost(CostReader):
    def __init__(
        self,
        settings: Settings,
        service: LiteLLMService,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        super().__init__(settings, transport=transport, clock=clock)
        self._service = service

    @property
    def spend_scope(self) -> str:
        """The gateway's resource group, or for AKS the subscription, since the cluster's nodes
        and load balancer are in its node resource group (AKS tags them like the cluster)."""
        if self._settings.gateway_cluster_id:
            return f"/subscriptions/{segment(self._settings.subscription_id)}"
        return self.group_scope

    def spend_filter(self, user: ArmClient) -> dict[str, Any]:
        # Everything the template deployed for this gateway carries the tag.
        return {
            "tags": {"name": GATEWAY_TAG, "operator": "In", "values": [self._settings.gateway_name]}
        }

    def _aks_price(self, refresh: bool) -> tuple[ListPrice | None, str | None, str | None]:
        """An AKS gateway's list price, profile and location, from its cluster."""
        try:
            cluster = self._service.arm.get_arm_json(
                self._settings.gateway_cluster_id, api_version=CONTAINER_SERVICE_API_VERSION
            )
        except ArmError as error:
            logger.info("Cannot read the gateway's cluster for its cost: %s", error.message)
            return None, None, None
        if not cluster:
            return None, None, None
        location = str(cluster.get("location") or "")
        tags = cluster.get("tags") or {}
        profile = str(tags.get("ai-gateway-profile") or "dev")
        pools = (cluster.get("properties") or {}).get("agentPoolProfiles") or [{}]
        count = int(pools[0].get("count") or 1)
        nodes = (int(pools[0].get("minCount") or count), int(pools[0].get("maxCount") or count))
        node_size = str(pools[0].get("vmSize") or DEFAULT_NODE_SIZE)
        standard = str((cluster.get("sku") or {}).get("tier") or "").casefold() == "standard"
        postgres, redis = sizes_from_tags(tags)
        try:
            prices = _fetch(self, _AKS_SERVICES, region_name(location), refresh, node_size)
            price = aks_list_price(
                prices,
                node_size=node_size,
                nodes=nodes,
                standard_tier=standard,
                postgres_sku=postgres,
                redis_sku=redis,
                postgres_ha=postgres_ha_from_tags(tags),
            )
            return price, profile, location
        except (httpx.HTTPError, ValueError) as error:
            logger.info("Cannot read Azure retail prices: %s", error)
            return None, profile, location

    def get(self, user: ArmClient | None, caller: str, *, refresh: bool = False) -> ServiceCost:
        if self._settings.gateway_cluster_id:
            aks_price, aks_profile, location = self._aks_price(refresh)
            return self._cost(user, caller, refresh, aks_profile, location, aks_price)
        if self._settings.external:
            return ServiceCost(
                profile=None,
                location=None,
                list_price=None,
                month_to_date=None,
                month_to_date_note="external",
                detail=None,
                portal_url=None,
            )
        info: GatewayInfo | None = None
        try:
            info = self._service.gateway.info()
        except GatewayUnavailable as error:
            logger.info("Cannot read the gateway for its cost: %s", error.message)
        profile = (info.tags.get("ai-gateway-profile") if info else None) or "dev"
        price: ListPrice | None = None
        if info is not None and info.location:
            try:
                prices = _fetch(
                    self, _ACA_SERVICES, region_name(info.location), refresh, DEFAULT_NODE_SIZE
                )
                price = list_price(prices, info)
            except (httpx.HTTPError, ValueError) as error:
                logger.info("Cannot read Azure retail prices: %s", error)
        return self._cost(user, caller, refresh, profile, info.location if info else None, price)

    def _cost(
        self,
        user: ArmClient | None,
        caller: str,
        refresh: bool,
        profile: str | None,
        location: str | None,
        price: ListPrice | None,
    ) -> ServiceCost:
        spend, note, detail = self.spend(user, caller, refresh)
        return ServiceCost(
            profile=profile if profile in {"dev", "production"} else None,
            location=location,
            list_price=price,
            month_to_date=spend,
            month_to_date_note=note,
            detail=detail,
            portal_url=f"https://portal.azure.com/#resource{self.spend_scope}/costanalysis",
        )
