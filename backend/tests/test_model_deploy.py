"""Creating OpenAI deployments in Foundry accounts from the Deployments tab (model_deploy.py)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pool_manager.app import app
from tests.fake_azure import FakeAzure
from tests.test_app import WRITE, _client, _settings
from tests.test_setup import _jwt


@pytest.fixture
def apim() -> FakeAzure:
    fake = FakeAzure()
    fake.add_account("aoai-east", deployments=[("gpt-4o", "gpt-4o", "OpenAI")])
    fake.add_account("speech", kind="SpeechServices")
    return fake


@pytest.fixture
def client(apim: FakeAzure) -> Iterator[TestClient]:
    yield _client(apim, _settings())
    app.dependency_overrides.clear()


def _account(client: TestClient, **headers: str) -> str:
    accounts = client.get("/api/foundry/accounts", headers=headers).json()
    return next(account["id"] for account in accounts if account["name"] == "aoai-east")


def _create(client: TestClient, account_id: str, **overrides: Any) -> Any:
    body = {
        "account_id": account_id,
        "deployment": "gpt-4o-new",
        "model_name": "gpt-4o",
        "model_version": "2024-11-20",
        "sku": "GlobalStandard",
        "capacity": 50,
        **overrides,
    }
    return client.post("/api/foundry/deployments", json=body, headers=WRITE)


def test_lists_the_foundry_accounts_discovery_scans(client: TestClient) -> None:
    accounts = client.get("/api/foundry/accounts").json()
    assert [account["name"] for account in accounts] == ["aoai-east"]
    assert (
        accounts[0]
        | {
            "kind": "AIServices",
            "location": "eastus2",
            "resource_group": "rg-ai",
            "subscription_id": "sub",
        }
        == accounts[0]
    )


def test_offers_only_openai_models_with_pay_per_token_types(client: TestClient) -> None:
    models = client.get("/api/foundry/models", params={"account_id": _account(client)}).json()
    # Deprecated, provisioned-only and partner (Anthropic) models are left out.
    assert [(model["name"], model["version"]) for model in models] == [
        ("gpt-4o", "2024-11-20"),
        ("gpt-4o", "2024-08-06"),
    ]
    latest = models[0]
    assert latest["is_default_version"] is True
    assert latest["retires_on"] == "2027-03-01T00:00:00Z"
    assert latest["capabilities"] == ["chatCompletion", "responses"]
    # Global, Data Zone, Standard; never PTU.
    assert [sku["name"] for sku in latest["skus"]] == [
        "GlobalStandard",
        "DataZoneStandard",
        "Standard",
    ]
    assert latest["skus"][0] == {
        "name": "GlobalStandard",
        "default_capacity": 50,
        "min_capacity": 1,
        "max_capacity": 1000,
        "step": 1,
    }
    assert models[1]["lifecycle_status"] == "Legacy"


def test_reports_free_capacity_in_the_accounts_region(client: TestClient, apim: FakeAzure) -> None:
    apim.capacities[("eastus2", "gpt-4o", "2024-11-20")] = {"GlobalStandard": 450, "Standard": 30}
    params = {"account_id": _account(client), "model": "gpt-4o", "version": "2024-11-20"}
    assert client.get("/api/foundry/capacity", params=params).json() == {
        "available": {"GlobalStandard": 450, "Standard": 30},
        "error": None,
    }


def test_creates_a_deployment_and_follows_it_to_succeeded(
    client: TestClient, apim: FakeAzure
) -> None:
    account_id = _account(client)
    response = _create(client, account_id)
    assert response.status_code == 200, response.text
    assert response.json() | {"deployment": "gpt-4o-new", "state": "Creating"} == response.json()

    put = next(r for r in reversed(apim.requests) if r.method == "PUT")
    assert put.url.path.endswith("/accounts/aoai-east/deployments/gpt-4o-new")
    assert put.read() and __import__("json").loads(put.content) == {
        "sku": {"name": "GlobalStandard", "capacity": 50},
        "properties": {
            "model": {"format": "OpenAI", "name": "gpt-4o", "version": "2024-11-20"},
            "versionUpgradeOption": "OnceNewDefaultVersionAvailable",
        },
    }

    params = {"account_id": account_id, "deployment": "gpt-4o-new"}
    status = client.get("/api/foundry/deployments/status", params=params).json()
    assert status["state"] == "Succeeded"
    # The discovery cache was dropped, so the new deployment is listed right away.
    listed = [view["deployment"] for view in client.get("/api/deployments").json()]
    assert "gpt-4o-new" in listed


@pytest.mark.parametrize(
    ("overrides", "status", "detail"),
    [
        ({"model_version": "1999-01-01"}, 400, "doesn't offer gpt-4o 1999-01-01"),
        ({"model_name": "claude-sonnet-5", "model_version": "2"}, 400, "doesn't offer"),
        ({"model_name": "gpt-35-turbo", "model_version": "0125"}, 400, "doesn't offer"),
        ({"sku": "DataZoneStandard", "model_version": "2024-08-06"}, 400, "no DataZoneStandard"),
        ({"capacity": 5000}, 400, "between 1 and 1000"),
        ({"deployment": "gpt-4o"}, 409, "already has a deployment gpt-4o"),
    ],
)
def test_refuses_what_the_account_does_not_offer(
    client: TestClient,
    apim: FakeAzure,
    overrides: dict[str, Any],
    status: int,
    detail: str,
) -> None:
    response = _create(client, _account(client), **overrides)
    assert response.status_code == status, response.text
    assert detail in response.json()["detail"]
    assert not [r for r in apim.requests if r.method == "PUT"]


def test_refuses_accounts_outside_the_scan_and_bad_requests(client: TestClient) -> None:
    other = (
        "/subscriptions/other/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/x"
    )
    assert _create(client, other).status_code == 404
    speech = _account(client).replace("aoai-east", "speech")
    assert _create(client, speech).status_code == 404
    # PTU isn't a deployment type this app creates, and names follow Azure's rules.
    assert _create(client, _account(client), sku="GlobalProvisionedManaged").status_code == 422
    assert _create(client, _account(client), deployment="-bad name").status_code == 422
    body = {"account_id": _account(client), "deployment": "x1", "model_name": "gpt-4o"}
    assert client.post("/api/foundry/deployments", json=body).status_code in {403, 422}


def test_writes_need_the_csrf_header(client: TestClient) -> None:
    body = {
        "account_id": _account(client),
        "deployment": "gpt-4o-new",
        "model_name": "gpt-4o",
        "model_version": "2024-11-20",
        "sku": "GlobalStandard",
        "capacity": 50,
    }
    assert client.post("/api/foundry/deployments", json=body).status_code == 403


def test_azure_errors_come_back_readable(client: TestClient, apim: FakeAzure) -> None:
    apim.deploy_error = (400, "InsufficientQuota")
    response = _create(client, _account(client))
    assert response.status_code == 400
    assert "InsufficientQuota" in response.json()["detail"]


def test_deployed_app_deploys_with_the_users_own_token(apim: FakeAzure) -> None:
    client = _client(
        apim, _settings(auth_mode="easy_auth", owners="owner@contoso.com", delegated_access=True)
    )
    try:
        signed_in = {"X-MS-CLIENT-PRINCIPAL-NAME": "dev@contoso.com"}
        account_id = _account(client, **signed_in)
        # Without the user's Azure token there is nothing to deploy with.
        body = {
            "account_id": account_id,
            "deployment": "gpt-4o-new",
            "model_name": "gpt-4o",
            "model_version": "2024-11-20",
            "sku": "Standard",
            "capacity": 10,
        }
        no_token = client.post(
            "/api/foundry/deployments", json=body, headers={**WRITE, **signed_in}
        )
        assert no_token.status_code == 401

        token = _jwt()
        apim.callers[token] = "user"
        headers = {**WRITE, "X-MS-CLIENT-PRINCIPAL-NAME": "dev@contoso.com"}
        headers["X-MS-TOKEN-AAD-ACCESS-TOKEN"] = token
        assert (
            client.post("/api/foundry/deployments", json=body, headers=headers).status_code == 200
        )
        put = next(r for r in reversed(apim.requests) if r.method == "PUT")
        assert put.headers["Authorization"] == f"Bearer {token}"

        # Azure refusing the user becomes a message naming the permission.
        apim.deploy_forbidden.add("user")
        body["deployment"] = "gpt-4o-other"
        refused = client.post("/api/foundry/deployments", json=body, headers=headers)
        assert refused.status_code == 403
        assert "deployments/write" in refused.json()["detail"]
    finally:
        app.dependency_overrides.clear()
