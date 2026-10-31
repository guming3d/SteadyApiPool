"""The gateway panel's cost: this month's spend (Cost Management) and a list price estimate."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from pool_manager.app import app
from pool_manager.cost import region_name
from pool_manager.litellm.cost import list_price, plan_price
from pool_manager.litellm.gateway import container_app_info
from pool_manager.litellm.gateway_template import aca_parameters
from pool_manager.litellm.models import GatewaySizing
from pool_manager.litellm.sizing import plan
from tests.fake_azure import GATEWAY_APP, RETAIL_PRICES, FakeAzure
from tests.litellm.fake_litellm import MASTER_KEY, URL
from tests.litellm.test_app import PRODUCTION, _client, _settings
from tests.litellm.test_setup import _jwt

SINCE = datetime.now(UTC).date().replace(day=1).isoformat()
QUERY = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CostManagement/query"


@pytest.fixture
def azure() -> FakeAzure:
    return FakeAzure()


@pytest.fixture
def client(azure: FakeAzure) -> Iterator[TestClient]:
    yield _client(azure, _settings())
    app.dependency_overrides.clear()


def _cost_requests(azure: FakeAzure) -> list[str]:
    return [
        request.url.host if request.url.host == "prices.azure.com" else "cost"
        for request in azure.requests
        if request.url.host == "prices.azure.com" or "CostManagement" in request.url.path
    ]


def _prices() -> dict[str, list[dict[str, object]]]:
    keys = {
        "aca": "Azure Container Apps",
        "postgres": "Azure Database for PostgreSQL",
        "redis": "Redis Cache",
    }
    return {key: [dict(item) for item in RETAIL_PRICES[name]] for key, name in keys.items()}


def test_cost_has_spend_and_an_estimate(client: TestClient, azure: FakeAzure) -> None:
    azure.costs["gw"] = (12.345, "USD")

    cost = client.get("/api/litellm/cost").json()

    assert (cost["profile"], cost["location"]) == ("dev", "East US 2")
    assert cost["month_to_date"] == {"amount": 12.35, "currency": "USD", "since": SINCE}
    assert cost["month_to_date_note"] is None
    price = cost["list_price"]
    kinds = [item["kind"] for item in price["items"]]
    assert kinds == ["container_apps", "postgres"]  # No Redis for dev.
    assert price["items"][0]["detail"] == "1.25 vCPU, 2.5 GiB x 1 replica"
    assert price["monthly_low"] == pytest.approx(29.57 + 16.09, abs=0.02)
    assert price["monthly_high"] == pytest.approx(98.55 + 16.09, abs=0.02)
    assert cost["portal_url"].endswith("/resourceGroups/rg/costanalysis")
    # The spend is everything tagged as this gateway's.
    request = next(r for r in azure.requests if r.url.path == QUERY)
    tags = json.loads(request.content)["dataset"]["filter"]["tags"]
    assert tags == {"name": "ai-gateway", "operator": "In", "values": ["gw"]}


def test_a_gateway_from_before_the_sizing_plan_is_priced_by_its_profile() -> None:
    fake = FakeAzure()
    app_id = fake.add_gateway("gw", profile="production")
    info = container_app_info(fake.container_apps[app_id.casefold()], "gw")

    price = list_price(_prices(), info)

    assert price is not None
    items = {item.kind: item for item in price.items}
    assert items["container_apps"].detail == "2.25 vCPU, 4.5 GiB x 2-10 replicas"
    assert items["container_apps"].monthly_low == pytest.approx(106.43, abs=0.01)
    assert items["container_apps"].monthly_high == pytest.approx(1773.9, abs=0.01)
    # What production deployed then: Burstable B2s and Redis B0.
    assert items["postgres"].detail == "B2s + 32 GB"
    assert items["postgres"].monthly_low == pytest.approx(53.32, abs=0.01)
    assert items["redis"].monthly_low == pytest.approx(23.36, abs=0.01)


def test_a_sized_gateway_is_priced_from_its_tags() -> None:
    sizing = GatewaySizing.model_validate(PRODUCTION)
    found = plan(sizing, "aca")
    parameters = {
        key: value["value"] for key, value in aca_parameters("gw", "eastus2", sizing, found).items()
    }
    fake = FakeAzure()
    app_id = fake.add_gateway("gw", profile="production", parameters=parameters)
    info = container_app_info(fake.container_apps[app_id.casefold()], "gw")

    price = list_price(_prices(), info)

    assert price is not None
    items = {item.kind: item for item in price.items}
    assert items["container_apps"].detail == "2.25 vCPU, 4.5 GiB x 2-6 replicas"
    # General Purpose D2ds_v5: its series' "2 vCore" price, not the per-vCore one; the gateway
    # survives losing a zone, so twice, with the standby in another zone.
    assert items["postgres"].detail == "D2ds_v5 + 32 GB x 2 (zone-redundant HA)"
    assert items["postgres"].monthly_low == pytest.approx(2 * 133.62, abs=0.01)
    assert items["redis"].detail == "Balanced B0 x 2"
    # The same as the plan said before it was deployed.
    before = plan_price(_prices(), found)
    assert before is not None
    assert (before.monthly_low, before.monthly_high) == (price.monthly_low, price.monthly_high)


def test_cost_is_cached_until_refreshed(client: TestClient, azure: FakeAzure) -> None:
    client.get("/api/litellm/cost")
    first = len(_cost_requests(azure))
    client.get("/api/litellm/cost")
    assert len(_cost_requests(azure)) == first
    client.get("/api/litellm/cost", params={"refresh": "true"})
    assert len(_cost_requests(azure)) == first * 2


def test_no_charges_yet(client: TestClient) -> None:
    cost = client.get("/api/litellm/cost").json()
    assert cost["month_to_date"] == {"amount": 0.0, "currency": None, "since": SINCE}


@pytest.mark.parametrize(
    ("error", "note"),
    [
        ((400, "SubscriptionTypeNotSupported"), "unsupported"),
        ((500, "InternalServerError"), "unavailable"),
    ],
)
def test_missing_spend_is_a_note_not_an_error(
    client: TestClient, azure: FakeAzure, error: tuple[int, str], note: str
) -> None:
    azure.cost_error = error
    cost = client.get("/api/litellm/cost").json()
    assert (cost["month_to_date"], cost["month_to_date_note"]) == (None, note)
    assert cost["list_price"] is not None


def test_unreachable_prices_leave_the_spend(client: TestClient, azure: FakeAzure) -> None:
    azure.prices_down = True
    azure.costs["gw"] = (3.0, "USD")
    cost = client.get("/api/litellm/cost").json()
    assert cost["list_price"] is None
    assert cost["month_to_date"]["amount"] == 3.0


def test_an_external_gateways_bill_is_not_ours_to_read(azure: FakeAzure) -> None:
    client = _client(
        azure, _settings(resource_group="", gateway_url=URL, gateway_master_key=MASTER_KEY)
    )
    try:
        cost = client.get("/api/litellm/cost").json()
    finally:
        app.dependency_overrides.clear()
    assert (cost["list_price"], cost["month_to_date_note"]) == (None, "external")
    assert not _cost_requests(azure)


def test_an_aks_gateway_is_priced_from_its_cluster(azure: FakeAzure) -> None:
    outputs = azure.add_aks_gateway(
        "gw-aks", group="rg-aks", subscription="sub", location="eastus2", profile="production"
    )
    azure.costs["gw-aks"] = (412.5, "USD")
    client = _client(
        azure,
        _settings(
            resource_group="rg-aks",
            gateway_name="gw-aks",
            gateway_url="https://gw-aks.eastus2.cloudapp.azure.com",
            gateway_master_key=MASTER_KEY,
            gateway_cluster_id=outputs["clusterId"],
        ),
    )
    try:
        cost = client.get("/api/litellm/cost").json()
    finally:
        app.dependency_overrides.clear()

    items = {item["kind"]: item for item in cost["list_price"]["items"]}
    # Three to six Linux nodes, the Standard tier, then the database and Redis.
    assert (items["aks_nodes"]["monthly_low"], items["aks_nodes"]["monthly_high"]) == (
        round(0.24 * 730 * 3, 2),
        round(0.24 * 730 * 6, 2),
    )
    assert items["aks_tier"]["monthly_low"] == 73.0
    assert {"postgres", "redis"} <= set(items)
    assert (cost["profile"], cost["location"]) == ("production", "eastus2")
    assert cost["month_to_date"]["amount"] == 412.5
    # The node resource group's costs count: the query runs on the subscription, by tag.
    query = next(r for r in azure.requests if "CostManagement" in r.url.path)
    assert query.url.path == "/subscriptions/sub/providers/Microsoft.CostManagement/query"


def test_deployed_spend_is_read_with_the_users_own_token(azure: FakeAzure) -> None:
    client = _client(
        azure, _settings(auth_mode="easy_auth", owners="dev@contoso.com", delegated_access=True)
    )
    try:
        signed_in = {"X-MS-CLIENT-PRINCIPAL-NAME": "dev@contoso.com"}
        # Without Easy Auth's Azure token there is nobody to read the spend as.
        without = client.get("/api/litellm/cost", headers=signed_in).json()
        assert without["month_to_date_note"] == "no_token"

        token = _jwt()
        azure.callers[token] = "user"
        azure.allow("user", GATEWAY_APP, "*")
        # The app identity could read it; the user can't, so the user's answer counts.
        azure.cost_forbidden.add("user")
        headers = {**signed_in, "X-MS-TOKEN-AAD-ACCESS-TOKEN": token}
        cost = client.get("/api/litellm/cost?refresh=true", headers=headers).json()
        assert cost["month_to_date_note"] == "forbidden"
        query = next(r for r in reversed(azure.requests) if "CostManagement" in r.url.path)
        assert query.headers["Authorization"] == f"Bearer {token}"
    finally:
        app.dependency_overrides.clear()


def test_an_estimate_needs_the_container_apps_meters() -> None:
    fake = FakeAzure()
    info = container_app_info(fake.container_apps[GATEWAY_APP.casefold()], "gw")
    assert list_price({"aca": []}, info) is None
    assert region_name("East US 2") == "eastus2"
