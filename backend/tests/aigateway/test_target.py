"""Choosing, creating and deleting an AI Gateway in the local setup wizard."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pool_manager.app import app
from pool_manager.config import read_saved_target
from tests.aigateway.fake_aigateway import SUBSCRIPTION, TENANT, FakeAiGateway
from tests.aigateway.test_app import WRITE, _client, _settings

NO_TARGET = {"subscription_id": "", "resource_group": "", "aigateway_name": ""}
NEW = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-new"
    "/providers/Microsoft.ApiManagement/aigateways/aigw-new"
)


@pytest.fixture
def azure() -> FakeAiGateway:
    return FakeAiGateway()


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
def client(azure: FakeAiGateway, state: Path) -> Iterator[TestClient]:
    yield _client(azure, _settings(**NO_TARGET), state_file=state)
    app.dependency_overrides.clear()


def _saved(state: Path) -> dict[str, Any]:
    return dict(json.loads(state.read_text(encoding="utf-8"))["target"])


def test_lists_the_gateways_of_a_subscription(client: TestClient, azure: FakeAiGateway) -> None:
    azure.add_aigateway("ours")
    azure.add_aigateway("theirs", managed=False)
    views = client.get(f"/api/aigateway/target/gateways?subscription_id={SUBSCRIPTION}").json()
    assert [(view["name"], view["managed"], view["can_manage"]) for view in views] == [
        ("ours", True, True),
        ("theirs", False, True),
    ]
    assert views[0]["url"] == "https://ours.eastus2-01.ai.gateway.azure.com"
    options = client.get(
        f"/api/aigateway/target/create-options?subscription_id={SUBSCRIPTION}"
    ).json()
    assert options["locations"] == ["eastus2", "swedencentral"]
    assert options["provider_registered"] is True


def test_select_saves_the_gateway(client: TestClient, azure: FakeAiGateway, state: Path) -> None:
    azure.add_aigateway("ours")
    body = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-aigw",
        "aigateway_name": "ours",
    }
    assert client.put("/api/aigateway/target", json=body).status_code == 403  # CSRF
    response = client.put("/api/aigateway/target", json=body, headers=WRITE)
    assert response.status_code == 200, response.text
    assert response.json()["product"] == "aigateway"
    assert _saved(state) == {
        "product": "aigateway",
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-aigw",
        "aigateway_name": "ours",
        "tenant_id": TENANT,
    }
    assert read_saved_target(state) == {k: v for k, v in _saved(state).items() if k != "product"}


def test_select_refuses_a_gateway_still_being_created(
    client: TestClient, azure: FakeAiGateway
) -> None:
    gateway = azure.add_aigateway("ours")
    azure.gateways[gateway.casefold()]["properties"]["provisioningState"] = "Creating"
    body = {"subscription_id": SUBSCRIPTION, "resource_group": "rg-aigw", "aigateway_name": "ours"}
    response = client.put("/api/aigateway/target", json=body, headers=WRITE)
    assert response.status_code == 424 and "still being created" in response.json()["detail"]
    forced = client.put("/api/aigateway/target", json={**body, "force": True}, headers=WRITE)
    assert forced.status_code == 200


def test_create_makes_the_group_and_a_tagged_gateway(
    client: TestClient, azure: FakeAiGateway, state: Path
) -> None:
    body = {
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-new",
        "location": "eastus2",
        "name": "aigw-new",
    }
    response = client.post("/api/aigateway/target/gateways", json=body, headers=WRITE)
    assert response.status_code == 200, response.text
    assert response.json()["provisioning_state"] == "Creating"
    created = azure.gateways[NEW.casefold()]
    assert created["tags"] == {"managed-by": "steadyapipool"}
    assert created["identity"]["type"] == "SystemAssigned" and created["sku"] == {
        "name": "Standard"
    }
    put = next(r for r in azure.requests if r.method == "PUT" and r.url.path == NEW)
    assert put.headers["If-None-Match"] == "*"
    group = azure.resource_groups[f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-new".casefold()]
    assert group["tags"] == {"managed-by": "steadyapipool"}
    assert _saved(state)["aigateway_name"] == "aigw-new"
    # Never over an existing gateway, or a name Azure says is taken.
    assert (
        client.post("/api/aigateway/target/gateways", json=body, headers=WRITE).status_code == 409
    )
    azure.taken_names.add("aigw-taken")
    taken = client.post(
        "/api/aigateway/target/gateways", json={**body, "name": "aigw-taken"}, headers=WRITE
    )
    assert taken.status_code == 409
    bad = client.post(
        "/api/aigateway/target/gateways", json={**body, "name": "Bad_Name"}, headers=WRITE
    )
    assert bad.status_code == 422


def test_only_gateways_this_app_created_can_be_deleted(
    client: TestClient, azure: FakeAiGateway
) -> None:
    azure.add_aigateway("theirs", managed=False)
    ours = azure.add_aigateway("ours")
    select = {"subscription_id": SUBSCRIPTION, "resource_group": "rg-aigw"}
    theirs = client.post(
        "/api/aigateway/target/gateways/delete-check",
        json={**select, "aigateway_name": "theirs"},
        headers=WRITE,
    )
    assert theirs.status_code == 403
    check = client.post(
        "/api/aigateway/target/gateways/delete-check",
        json={**select, "aigateway_name": "ours"},
        headers=WRITE,
    ).json()
    assert check["name"] == "ours" and check["current"] is False
    wrong = client.post(
        "/api/aigateway/target/gateways/delete",
        json={**select, "aigateway_name": "ours", "confirm_name": "nope"},
        headers=WRITE,
    )
    assert wrong.status_code == 400 and ours.casefold() in azure.gateways
    deleted = client.post(
        "/api/aigateway/target/gateways/delete",
        json={**select, "aigateway_name": "ours", "confirm_name": "ours"},
        headers=WRITE,
    )
    assert deleted.status_code == 204 and ours.casefold() not in azure.gateways
