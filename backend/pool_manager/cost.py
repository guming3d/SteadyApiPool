"""What the gateway costs: this month's actual spend and a list price estimate.

Actual spend comes from Cost Management (``Microsoft.CostManagement/query``) and is read with the
signed-in user's own token, so the app identity gets no billing access. Azure refreshes it every
few hours. The estimate comes from the public Azure Retail Prices API, which needs no sign-in.
Each product says what it prices and which costs are its gateway's (``apim/cost.py``,
``litellm/cost.py``).

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
from .models import CostNote, MonthToDate

logger = logging.getLogger(__name__)

COST_API_VERSION = "2026-06-01"
RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
HOURS_PER_MONTH = 730
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


def meter(items: list[dict[str, Any]], match: Callable[[dict[str, Any]], bool]) -> float | None:
    """The highest retail price among the meters that match; None if none does."""
    found = [float(item.get("retailPrice") or 0) for item in items if match(item)]
    return max(found) if found else None


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
    """Retail prices and this month's spend; each product builds its gateway's cost from them
    (``get``) and says where its spend is (``spend_scope``, ``spend_filter``)."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http = httpx.Client(transport=transport, timeout=httpx.Timeout(15.0))
        self._clock = clock
        self._lock = Lock()
        self._price_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
        self._spend_cache: dict[str, tuple[float, MonthToDate]] = {}

    def close(self) -> None:
        self._http.close()

    @property
    def group_scope(self) -> str:
        settings = self._settings
        return (
            f"/subscriptions/{segment(settings.subscription_id)}"
            f"/resourceGroups/{segment(settings.resource_group)}"
        )

    # -- what each product says about its gateway -------------------------------------

    @property
    def spend_scope(self) -> str:
        """Where Cost Management looks for the gateway's costs."""
        return self.group_scope

    def spend_filter(self, user: ArmClient) -> dict[str, Any]:
        """The Cost Management filter that keeps only the gateway's costs."""
        raise NotImplementedError

    # -- shared reads -----------------------------------------------------------------

    def spend(
        self, user: ArmClient | None, caller: str, refresh: bool
    ) -> tuple[MonthToDate | None, CostNote | None, str | None]:
        """This month's spend, or why it is missing, and Azure's message when it failed."""
        if user is None:
            return None, "no_token", None
        try:
            return self._month_to_date(user, caller, refresh), None, None
        except ArmError as error:
            logger.info("Cannot read the gateway's cost: %s", error.message)
            if error.status in {401, 403}:
                return None, "forbidden", error.message
            if error.code in _UNSUPPORTED:
                return None, "unsupported", error.message
            return None, "unavailable", error.message

    def retail_prices(
        self, service: str, region: str, refresh: bool, extra_filter: str = ""
    ) -> list[dict[str, Any]]:
        """Every Consumption meter of one service in a region (``extra_filter`` narrows it)."""
        key = (f"{service}|{extra_filter}", region)
        with self._lock:
            cached = self._price_cache.get(key)
        if cached and not refresh and self._clock() - cached[0] < _PRICES_SECONDS:
            return cached[1]
        query = (
            f"serviceName eq '{service}' and armRegionName eq '{region}' "
            f"and priceType eq 'Consumption'{extra_filter}"
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
            self._price_cache[key] = (self._clock(), items)
        return items

    def _month_to_date(self, user: ArmClient, caller: str, refresh: bool) -> MonthToDate:
        with self._lock:
            cached = self._spend_cache.get(caller)
        if cached and not refresh and self._clock() - cached[0] < _SPEND_SECONDS:
            return cached[1]
        since = datetime.now(UTC).date().replace(day=1).isoformat()
        response = user.request(
            "POST",
            user.arm_url(f"{self.spend_scope}/providers/Microsoft.CostManagement/query"),
            api_version=COST_API_VERSION,
            json_body={
                "type": "ActualCost",
                "timeframe": "MonthToDate",
                "dataset": {
                    "granularity": "None",
                    "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                    "filter": self.spend_filter(user),
                },
            },
        )
        spend = month_to_date(response.json(), since)
        with self._lock:
            self._spend_cache[caller] = (self._clock(), spend)
        return spend
