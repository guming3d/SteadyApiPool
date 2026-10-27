"""What an APIM service costs: this month's spend on the service, and its tier's list price
(the hourly unit price times the service's units over an average month, 730 hours)."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..arm import ArmClient
from ..config import Settings
from ..cost import HOURS_PER_MONTH, CostReader, region_name
from .models import ListPrice, ServiceCost
from .service import ApimService

logger = logging.getLogger(__name__)

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


class ApimCost(CostReader):
    def __init__(
        self,
        settings: Settings,
        service: ApimService,
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
        sku, units, location = self._service.service_plan()
        price: ListPrice | None = None
        try:
            items = self.retail_prices("API Management", region_name(location), refresh)
            price = list_price(items, sku, units)
        except (httpx.HTTPError, ValueError) as error:
            logger.info("Cannot read Azure retail prices: %s", error)
        spend, note, detail = self.spend(user, caller, refresh)
        return ServiceCost(
            sku=sku,
            units=units,
            location=location,
            list_price=price,
            month_to_date=spend,
            month_to_date_note=note,
            detail=detail,
            portal_url=f"https://portal.azure.com/#resource{self.group_scope}/costanalysis",
        )
