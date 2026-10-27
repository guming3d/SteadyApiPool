"""The service panel's cost: this month's spend (Cost Management) and the tier's list price."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from pool_manager.apim.cost import list_price
from pool_manager.app import app
from pool_manager.cost import region_name
from tests.apim.fake_apim import RETAIL_PRICES, SERVICE, FakeApim
from tests.apim.test_app import _client, _settings
from tests.apim.test_setup import _jwt

SINCE = datetime.now(UTC).date().replace(day=1).isoformat()
QUERY = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CostManagement/query"


@pytest.fixture
def apim() -> FakeApim:
    return FakeApim()


@pytest.fixture
def client(apim: FakeApim) -> Iterator[TestClient]:
    yield _client(apim, _settings())
    app.dependency_overrides.clear()


def _cost_requests(apim: FakeApim) -> list[str]:
    return [
        request.url.host if request.url.host == "prices.azure.com" else "cost"
        for request in apim.requests
        if request.url.host == "prices.azure.com" or "CostManagement" in request.url.path
    ]


def test_cost_has_spend_and_list_price(client: TestClient, apim: FakeApim) -> None:
    apim.costs[SERVICE.casefold()] = (12.5, "EUR")
    response = client.get("/api/apim/cost")
    assert response.status_code == 200, response.text
    cost = response.json()

    assert (cost["sku"], cost["units"], cost["location"]) == ("BasicV2", 2, "East US 2")
    assert cost["month_to_date"] == {"amount": 12.5, "currency": "EUR", "since": SINCE}
    assert (cost["month_to_date_note"], cost["detail"]) == (None, None)
    # Two units around the clock for an average month; requests beyond the included ones extra.
    assert cost["list_price"] == {
        "tier": "Basic v2",
        "units": 2,
        "hourly": 0.20548,
        "monthly": 300.0,
        "per_10k_calls": 0.03,
        "currency": "USD",
    }
    assert cost["portal_url"].endswith("/subscriptions/sub/resourceGroups/rg/costanalysis")

    query = next(r for r in apim.requests if "CostManagement" in r.url.path)
    assert query.method == "POST"
    assert query.url.path == QUERY
    filter_ = json.loads(query.content)["dataset"]["filter"]["dimensions"]
    assert filter_ == {"name": "ResourceId", "operator": "In", "values": [SERVICE.casefold()]}


def test_cost_is_cached_until_refreshed(client: TestClient, apim: FakeApim) -> None:
    client.get("/api/apim/cost")
    assert _cost_requests(apim) == ["prices.azure.com", "cost"]
    client.get("/api/apim/cost")
    assert len(_cost_requests(apim)) == 2
    client.get("/api/apim/cost?refresh=true")
    assert len(_cost_requests(apim)) == 4


def test_no_charges_yet(client: TestClient, apim: FakeApim) -> None:
    cost = client.get("/api/apim/cost").json()
    assert cost["month_to_date"] == {"amount": 0.0, "currency": None, "since": SINCE}


@pytest.mark.parametrize(
    ("error", "note"),
    [
        ((403, "RBACAccessDenied"), "forbidden"),
        ((400, "SubscriptionTypeNotSupported"), "unsupported"),
        ((400, "BadRequest"), "unavailable"),
    ],
)
def test_missing_spend_is_a_note_not_an_error(
    client: TestClient, apim: FakeApim, error: tuple[int, str], note: str
) -> None:
    apim.cost_error = error
    response = client.get("/api/apim/cost")
    assert response.status_code == 200, response.text
    cost = response.json()
    assert cost["month_to_date"] is None
    assert cost["month_to_date_note"] == note
    assert cost["detail"]
    # The list price needs no Azure rights.
    assert cost["list_price"]["monthly"] == 300.0


def test_unreachable_prices_leave_the_spend(client: TestClient, apim: FakeApim) -> None:
    apim.prices_down = True
    apim.costs[SERVICE.casefold()] = (3.0, "USD")
    cost = client.get("/api/apim/cost").json()
    assert cost["list_price"] is None
    assert cost["month_to_date"]["amount"] == 3.0


def test_deployed_spend_is_read_with_the_users_own_token(apim: FakeApim) -> None:
    client = _client(
        apim, _settings(auth_mode="easy_auth", owners="dev@contoso.com", delegated_access=True)
    )
    try:
        signed_in = {"X-MS-CLIENT-PRINCIPAL-NAME": "dev@contoso.com"}
        # Without Easy Auth's Azure token there is nobody to read the spend as.
        without = client.get("/api/apim/cost", headers=signed_in).json()
        assert without["month_to_date_note"] == "no_token"

        token = _jwt()
        apim.callers[token] = "user"
        # The app identity could read it; the user can't, so the user's answer counts.
        apim.cost_forbidden.add("user")
        headers = {**signed_in, "X-MS-TOKEN-AAD-ACCESS-TOKEN": token}
        cost = client.get("/api/apim/cost?refresh=true", headers=headers).json()
        assert cost["month_to_date_note"] == "forbidden"
        query = next(r for r in reversed(apim.requests) if "CostManagement" in r.url.path)
        assert query.headers["Authorization"] == f"Bearer {token}"
    finally:
        app.dependency_overrides.clear()


def test_list_price_per_tier() -> None:
    items = [{"currencyCode": "USD", **item} for item in RETAIL_PRICES]
    developer = list_price(items, "Developer", 1)
    assert developer is not None
    assert (developer.monthly, developer.per_10k_calls) == (48.03, None)
    # Consumption has no units, only calls.
    consumption = list_price(items, "Consumption", 0)
    assert consumption is not None
    assert (consumption.hourly, consumption.monthly, consumption.per_10k_calls) == (
        None,
        None,
        0.035,
    )
    assert list_price(items, "PremiumV2", 1) is None  # Not in this region's list.
    assert region_name("East US 2") == "eastus2"
