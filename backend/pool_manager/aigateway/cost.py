"""What an AI Gateway costs: this month's spend on the gateway's resource, and its list price.

AI Gateway is billed per request (the Retail Prices API's ``10k AI Gateway Requests`` meter of
API Management, SKU ``AI Gateway``), so the list price is per 10,000 requests rather than a
monthly amount. The models behind it are billed by their Foundry accounts, not here.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..arm import ArmClient
from ..config import Settings
from ..cost import CostReader, region_name
from .models import ListPrice, ServiceCost
from .service import AiGatewayService

logger = logging.getLogger(__name__)

RETAIL_SKU = "AI Gateway"
REQUESTS_METER = "10k AI Gateway Requests"


def list_price(items: list[dict[str, Any]]) -> ListPrice | None:
    """The request price from a region's API Management meters; None if they don't list it.
    Requests are tiered, so the highest tier's price is the one that applies at scale."""
    prices = [
        float(item.get("retailPrice") or 0)
        for item in items
        if item.get("skuName") == RETAIL_SKU and item.get("meterName") == REQUESTS_METER
    ]
    if not prices:
        return None
    currency = str(next((item.get("currencyCode") for item in items), None) or "USD")
    return ListPrice(per_10k_requests=max(prices), currency=currency)


class AiGatewayCost(CostReader):
    def __init__(
        self,
        settings: Settings,
        service: AiGatewayService,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        super().__init__(settings, transport=transport, clock=clock)
        self._service = service

    def spend_filter(self, user: ArmClient) -> dict[str, Any]:
        # Cost Management keeps resource IDs in lower case.
        return {
            "dimensions": {
                "name": "ResourceId",
                "operator": "In",
                "values": [user.resource_id.casefold()],
            }
        }

    def get(self, user: ArmClient | None, caller: str, *, refresh: bool = False) -> ServiceCost:
        sku, location = self._service.plan()
        price: ListPrice | None = None
        if location:
            try:
                items = self.retail_prices(
                    "API Management",
                    region_name(location),
                    refresh,
                    f" and skuName eq '{RETAIL_SKU}'",
                )
                price = list_price(items)
            except (httpx.HTTPError, ValueError) as error:
                logger.info("Cannot read Azure retail prices: %s", error)
        spend, note, detail = self.spend(user, caller, refresh)
        return ServiceCost(
            sku=sku or None,
            location=location or None,
            list_price=price,
            month_to_date=spend,
            month_to_date_note=note,
            detail=detail,
            portal_url=f"https://portal.azure.com/#resource{self.group_scope}/costanalysis",
        )
