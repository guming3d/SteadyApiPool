"""What the gateway costs: this month's actual spend and a list price estimate.

Actual spend comes from Cost Management (``Microsoft.CostManagement/query`` on the gateway's
resource group, filtered to the resources tagged ``ai-gateway: <name>``, which the deploy
template puts on everything it creates) and is read with the signed-in user's own token, so the
app identity gets no billing access. Azure refreshes it every few hours.

The estimate comes from the public Azure Retail Prices API, which needs no sign-in, for what
the template deploys: Container Apps replicas (busy at the active rate, quiet at the idle rate,
so a range), the environment, Postgres Flexible Server and, for production, Azure Managed Redis.
It leaves out the monthly free grant, log ingestion and discounts.

Neither is ever a blocker: a missing permission or an unreachable API becomes a note in the
gateway panel. Spend is cached for an hour per caller, prices for a day per region.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock
from typing import Any

import httpx

from .arm import ArmClient, ArmError, segment
from .config import Settings
from .gateway import GATEWAY_TAG, GatewayInfo, GatewayUnavailable
from .models import CostItem, CostNote, ListPrice, MonthToDate, ServiceCost
from .service import PoolManagerService

logger = logging.getLogger(__name__)

COST_API_VERSION = "2026-06-01"
RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
HOURS_PER_MONTH = 730
SECONDS_PER_MONTH = HOURS_PER_MONTH * 3600
# What each profile of the template deploys besides the Container App.
POSTGRES_SKU = {"dev": "B1MS", "production": "B2S"}
POSTGRES_STORAGE_GB = 32
# Azure Managed Redis Balanced B0, with high availability (two instances).
REDIS_SKU, REDIS_INSTANCES = "B0", 2
# One query per service; each lists every meter of the region.
_SERVICES = {
    "aca": "Azure Container Apps",
    "postgres": "Azure Database for PostgreSQL",
    "redis": "Redis Cache",
}
_SPEND_SECONDS = 3600.0
_PRICES_SECONDS = 86400.0
_MAX_PRICE_PAGES = 10
# Cost Management's answers for subscriptions it has no data for (free, sponsored, CSP, ...).
_UNSUPPORTED = frozenset(
    {"SubscriptionTypeNotSupported", "BillingAccessDenied", "IndirectCostDisabled"}
)
_COST_COLUMNS = ("totalcost", "cost", "pretaxcost")


def region_name(location: str) -> str:
    """ARM's short region name: ``East US 2`` becomes ``eastus2``."""
    return location.replace(" ", "").casefold()


def _meter(items: list[dict[str, Any]], match: Callable[[dict[str, Any]], bool]) -> float | None:
    found = [float(item.get("retailPrice") or 0) for item in items if match(item)]
    return max(found) if found else None


def list_price(
    prices: dict[str, list[dict[str, Any]]], info: GatewayInfo, profile: str
) -> ListPrice | None:
    """The estimate from a region's meters; None if a meter it needs is missing."""
    aca = prices.get("aca") or []

    def standard(meter: str) -> float | None:
        return _meter(aca, lambda i: i.get("skuName") == "Standard" and i.get("meterName") == meter)

    cpu_active = standard("Standard vCPU Active Usage")
    cpu_idle = standard("Standard vCPU Idle Usage")
    mem_active = standard("Standard Memory Active Usage")
    mem_idle = standard("Standard Memory Idle Usage")
    if None in {cpu_active, cpu_idle, mem_active, mem_idle} or info.cpu is None:
        return None
    assert cpu_active is not None and cpu_idle is not None
    assert mem_active is not None and mem_idle is not None
    cpu, memory = info.cpu, info.memory_gib or 0.0
    low_replicas = max(info.min_replicas or 1, 1)
    high_replicas = max(info.max_replicas or low_replicas, low_replicas)
    items = [
        CostItem(
            kind="container_apps",
            detail=f"{cpu:g} vCPU, {memory:g} GiB x {low_replicas}-{high_replicas} replicas"
            if high_replicas != low_replicas
            else f"{cpu:g} vCPU, {memory:g} GiB x {low_replicas} replica",
            # Quiet: the minimum replicas at the idle rate. Busy: every replica at the active rate.
            monthly_low=round(
                (cpu * cpu_idle + memory * mem_idle) * SECONDS_PER_MONTH * low_replicas, 2
            ),
            monthly_high=round(
                (cpu * cpu_active + memory * mem_active) * SECONDS_PER_MONTH * high_replicas, 2
            ),
        )
    ]
    postgres = prices.get("postgres") or []
    sku = POSTGRES_SKU.get(profile, "B1MS")
    compute = _meter(
        postgres, lambda i: i.get("meterName") == sku and "Compute" in str(i.get("productName"))
    )
    storage = _meter(
        postgres,
        lambda i: (
            i.get("meterName") == "Storage Data Stored" and "Flex" in str(i.get("productName"))
        ),
    )
    if compute is not None:
        monthly = compute * HOURS_PER_MONTH + (storage or 0) * POSTGRES_STORAGE_GB
        items.append(
            CostItem(
                kind="postgres",
                detail=f"{sku} + {POSTGRES_STORAGE_GB} GB",
                monthly_low=round(monthly, 2),
                monthly_high=round(monthly, 2),
            )
        )
    if profile == "production":
        redis = _meter(
            prices.get("redis") or [],
            lambda i: (
                i.get("skuName") == REDIS_SKU
                and "Managed Redis - Balanced" in str(i.get("productName"))
            ),
        )
        if redis is not None:
            monthly = redis * HOURS_PER_MONTH * REDIS_INSTANCES
            items.append(
                CostItem(
                    kind="redis",
                    detail=f"Balanced {REDIS_SKU} x {REDIS_INSTANCES}",
                    monthly_low=round(monthly, 2),
                    monthly_high=round(monthly, 2),
                )
            )
    currency = str(next((item.get("currencyCode") for item in aca), None) or "USD")
    return ListPrice(
        items=items,
        monthly_low=round(sum(item.monthly_low for item in items), 2),
        monthly_high=round(sum(item.monthly_high for item in items), 2),
        currency=currency,
    )


def month_to_date(result: dict[str, Any], since: str) -> MonthToDate:
    """Sums the cost column of a Cost Management query result."""
    properties = result.get("properties") or {}
    columns = [str(column.get("name") or "") for column in properties.get("columns") or []]
    lowered = [name.casefold() for name in columns]
    amount_at = next((lowered.index(name) for name in _COST_COLUMNS if name in lowered), 0)
    currency_at = lowered.index("currency") if "currency" in lowered else None
    rows = properties.get("rows") or []
    amount = sum(float(row[amount_at] or 0) for row in rows if len(row) > amount_at)
    currency = next(
        (str(row[currency_at]) for row in rows if currency_at is not None and row[currency_at]),
        None,
    )
    return MonthToDate(amount=round(amount, 2), currency=currency, since=since)


class CostReader:
    def __init__(
        self,
        settings: Settings,
        service: PoolManagerService,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._settings = settings
        self._service = service
        self._http = httpx.Client(transport=transport, timeout=httpx.Timeout(15.0))
        self._clock = clock
        self._lock = Lock()
        self._prices: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
        self._spend: dict[str, tuple[float, MonthToDate]] = {}

    def close(self) -> None:
        self._http.close()

    @property
    def _group_scope(self) -> str:
        settings = self._settings
        return (
            f"/subscriptions/{segment(settings.subscription_id)}"
            f"/resourceGroups/{segment(settings.resource_group)}"
        )

    def get(self, user: ArmClient | None, caller: str, *, refresh: bool = False) -> ServiceCost:
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
                region = region_name(info.location)
                prices = {key: self._region_prices(key, region, refresh) for key in _SERVICES}
                price = list_price(prices, info, profile)
            except (httpx.HTTPError, ValueError) as error:
                logger.info("Cannot read Azure retail prices: %s", error)
        spend: MonthToDate | None = None
        note: CostNote | None = None
        detail: str | None = None
        if user is None:
            note = "no_token"
        else:
            try:
                spend = self._month_to_date(user, caller, refresh)
            except ArmError as error:
                detail = error.message
                if error.status in {401, 403}:
                    note = "forbidden"
                elif error.code in _UNSUPPORTED:
                    note = "unsupported"
                else:
                    note = "unavailable"
                logger.info("Cannot read the gateway's cost: %s", error.message)
        return ServiceCost(
            profile=profile,
            location=info.location if info else None,
            list_price=price,
            month_to_date=spend,
            month_to_date_note=note,
            detail=detail,
            portal_url=f"https://portal.azure.com/#resource{self._group_scope}/costanalysis",
        )

    def _region_prices(self, key: str, region: str, refresh: bool) -> list[dict[str, Any]]:
        with self._lock:
            cached = self._prices.get((key, region))
        if cached and not refresh and self._clock() - cached[0] < _PRICES_SECONDS:
            return cached[1]
        query = (
            f"serviceName eq '{_SERVICES[key]}' and armRegionName eq '{region}' "
            "and priceType eq 'Consumption'"
        )
        items: list[dict[str, Any]] = []
        url: str | None = RETAIL_PRICES_URL
        params: dict[str, str] | None = {"$filter": query}
        for _ in range(_MAX_PRICE_PAGES):
            if url is None:
                break
            response = self._http.get(url, params=params)
            response.raise_for_status()
            body = response.json()
            items.extend(body.get("Items") or [])
            url = body.get("NextPageLink") or None
            params = None  # The next link carries the query.
            if url and not url.startswith("https://prices.azure.com/"):
                break
        with self._lock:
            self._prices[(key, region)] = (self._clock(), items)
        return items

    def _month_to_date(self, user: ArmClient, caller: str, refresh: bool) -> MonthToDate:
        with self._lock:
            cached = self._spend.get(caller)
        if cached and not refresh and self._clock() - cached[0] < _SPEND_SECONDS:
            return cached[1]
        since = datetime.now(UTC).date().replace(day=1).isoformat()
        response = user.request(
            "POST",
            user.arm_url(f"{self._group_scope}/providers/Microsoft.CostManagement/query"),
            api_version=COST_API_VERSION,
            json_body={
                "type": "ActualCost",
                "timeframe": "MonthToDate",
                "dataset": {
                    "granularity": "None",
                    "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                    # Everything the template deployed for this gateway carries the tag.
                    "filter": {
                        "tags": {
                            "name": GATEWAY_TAG,
                            "operator": "In",
                            "values": [self._settings.gateway_name],
                        }
                    },
                },
            },
        )
        spend = month_to_date(response.json(), since)
        with self._lock:
            self._spend[caller] = (self._clock(), spend)
        return spend
