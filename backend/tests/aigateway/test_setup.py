"""AI Gateway's setup checks and fixes: its access, its identity, and Foundry User for that
identity on the member accounts."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pool_manager.app import app
from tests.aigateway.fake_aigateway import FOUNDRY_USER, GATEWAY, FakeAiGateway
from tests.aigateway.test_app import WRITE, _client, _settings, import_members, seed


@pytest.fixture
def azure() -> FakeAiGateway:
    return FakeAiGateway()


@pytest.fixture
def client(azure: FakeAiGateway) -> Iterator[TestClient]:
    yield _client(azure, _settings())
    app.dependency_overrides.clear()


def _checks(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/api/setup")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["product"] == "aigateway"
    return {check["id"]: check for check in report["checks"]}


def test_report_asks_for_foundry_user_on_member_accounts(
    client: TestClient, azure: FakeAiGateway
) -> None:
    east, west = seed(azure)
    import_members(client, [east, west])
    azure.role_assignments.append(
        {
            "id": f"{east}/providers/Microsoft.Authorization/roleAssignments/seeded",
            "properties": {
                "scope": east,
                "principalId": "aigw-mi",
                "roleDefinitionId": FOUNDRY_USER,
            },
        }
    )
    checks = _checks(client)
    assert checks["gateway_target"]["status"] == "ok"
    assert checks["gateway_access"]["status"] == "ok"
    assert checks["gateway_identity"] == {
        **checks["gateway_identity"],
        "status": "ok",
        "subject": "aigw-mi",
    }
    assert checks[f"foundry_access:{east.casefold()}"]["status"] == "ok"
    missing = checks[f"foundry_access:{west.casefold()}"]
    assert missing["status"] == "action" and missing["fix"] == "grant_foundry_access"
    assert missing["request"]["role"] == "Foundry User"
    assert '--role "Foundry User"' in missing["request"]["command"]

    response = client.post(
        "/api/setup/foundry-access/grant", json={"account_ids": [west]}, headers=WRITE
    )
    assert response.status_code == 200, response.text
    assert [check["status"] for check in response.json()] == ["ok"]
    granted = azure.role_assignments[-1]["properties"]
    assert granted["roleDefinitionId"].endswith("/53ca6127-db72-4b80-b1b0-d745d6d5456d")
    assert granted["principalId"] == "aigw-mi" and granted["scope"] == west


def test_missing_rights_on_the_gateway_are_named(client: TestClient, azure: FakeAiGateway) -> None:
    azure.allow("app", "/subscriptions/sub", "*/read")
    check = _checks(client)["gateway_access"]
    assert check["status"] == "action"
    assert "Microsoft.ApiManagement/aigateways/workspaces/aliases/write" in check["missing"]
    assert check["request"]["scope"] == GATEWAY


def test_enable_the_gateways_identity(client: TestClient, azure: FakeAiGateway) -> None:
    azure.principal = None
    check = _checks(client)["gateway_identity"]
    assert check["status"] == "action" and check["fix"] == "enable_aigateway_identity"
    assert "az aigateway identity assign" in check["request"]["command"]
    response = client.post("/api/aigateway/setup/identity", headers=WRITE)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    assert azure.principal == "aigw-mi-new"


def test_a_gateway_still_being_created_is_pending(client: TestClient, azure: FakeAiGateway) -> None:
    azure.provisioning = "Creating"
    report = client.get("/api/setup").json()
    target = next(check for check in report["checks"] if check["id"] == "gateway_target")
    assert target["status"] == "pending" and target["started_at"] == "2026-11-20T10:00:00Z"
    assert report["can_operate"] is False
