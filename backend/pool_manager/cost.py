"""What the APIM service costs: this month's actual spend and its tier's list price.

Actual spend comes from Cost Management (``Microsoft.CostManagement/query`` on the service's
resource group, filtered to the service) and is read with the signed-in user's own token, like
subscription keys, so the app identity gets no billing access. Azure refreshes it every few hours.
The list price comes from the public Azure Retail Prices API, which needs no sign-in: the tier's
hourly unit price times the service's units over an average month (730 hours), in USD.

Neither is ever a blocker: a missing permission or an unreachable API becomes a note in the
service panel. Spend is cached for an hour per caller, prices for a day per region.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from threading import Lock
from typing import Any

import httpx

from .arm import ArmClient, ArmError, segment
from .config import Settings
from .models import CostNote, ListPrice, MonthToDate, ServiceCost
from .service import PoolManagerService

logger = logging.getLogger(__name__)

COST_API_VERSION = "2026-06-01"
RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
HOURS_PER_MONTH = 730
# ARM SKU names and how the Retail Prices API names their tiers ("<tier> Unit", "<tier> Calls").
RETAIL_TIERS = {
    "Consumption": "Consumption",
    "Developer": "Developer",
    "Basic": "Basic",
    "Standard": "Standard",
    "Premium": "Premium",
    "Isolated": "Isolated",
    "BasicV2": "Basic v2",
    "StandardV2": "Standard v2",
    "PremiumV2": "Premium v2",
}
_SPEND_SECONDS = 3600.0
_PRICES_SECONDS = 86400.0
_MAX_PRICE_PAGES = 5
# Cost Management's answers for subscriptions it has no data for (free, sponsored, CSP, ...).
_UNSUPPORTED = frozenset(
    {"SubscriptionTypeNotSupported", "BillingAccessDenied", "IndirectCostDisabled"}
)
_COST_COLUMNS = ("totalcost", "cost", "pretaxcost")


def region_name(location: str) -> str:
    """ARM's short region name: APIM reports ``East US 2``, prices use ``eastus2``."""
    return location.replace(" ", "").casefold()


def list_price(items: list[dict[str, Any]], sku: str, units: int) -> ListPrice | None:
    """The tier's price from a region's API Management meters; None if they don't list it."""
    tier = RETAIL_TIERS.get(sku, sku)
    hourly = next(
        (
            float(item.get("retailPrice") or 0)
            for item in items
            if item.get("meterName") == f"{tier} Unit"
            and "hour" in str(item.get("unitOfMeasure") or "").casefold()
        ),
        None,
    )
    # Calls are tiered: the included requests cost 0, the rest the highest tier's price.
    calls = [
        float(item.get("retailPrice") or 0)
        for item in items
        if item.get("meterName") == f"{tier} Calls"
    ]
    per_10k = max(calls) if calls and max(calls) > 0 else None
    if hourly is None and per_10k is None:
        return None
    currency = str(next((item.get("currencyCode") for item in items), None) or "USD")
    return ListPrice(
        tier=tier,
        units=units,
        hourly=hourly,
        monthly=round(hourly * HOURS_PER_MONTH * units, 2) if hourly is not None else None,
        per_10k_calls=per_10k,
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
        self._prices: dict[str, tuple[float, list[dict[str, Any]]]] = {}
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
        sku, units, location = self._service.service_plan()
        price: ListPrice | None = None
        try:
            price = list_price(self._region_prices(region_name(location), refresh), sku, units)
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
                logger.info("Cannot read the APIM service's cost: %s", error.message)
        return ServiceCost(
            sku=sku,
            units=units,
            location=location,
            list_price=price,
            month_to_date=spend,
            month_to_date_note=note,
            detail=detail,
            portal_url=f"https://portal.azure.com/#resource{self._group_scope}/costanalysis",
        )

    def _region_prices(self, region: str, refresh: bool) -> list[dict[str, Any]]:
        with self._lock:
            cached = self._prices.get(region)
        if cached and not refresh and self._clock() - cached[0] < _PRICES_SECONDS:
            return cached[1]
        query = (
            f"serviceName eq 'API Management' and armRegionName eq '{region}' "
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
            self._prices[region] = (self._clock(), items)
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
                    # Cost Management keeps resource IDs in lower case.
                    "filter": {
                        "dimensions": {
                            "name": "ResourceId",
                            "operator": "In",
                            "values": [user.resource_id.casefold()],
                        }
                    },
                },
            },
        )
        spend = month_to_date(response.json(), since)
        with self._lock:
            self._spend[caller] = (self._clock(), spend)
        return spend
