"""A pool's Foundry quota: each member's rate limits from its deployment, and what the pool's
deployments allow together, beside what the gateway was sized for."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from pool_manager.app import app
from pool_manager.litellm.gateway_template import aca_parameters
from pool_manager.litellm.models import GatewaySizing
from pool_manager.litellm.sizing import plan
from pool_manager.model_check import deployment_quota
from tests.fake_azure import FakeAzure
from tests.litellm.test_app import PRODUCTION, _client, _settings, import_members, seed


def test_rate_limits_come_from_the_deployment() -> None:
    quota = deployment_quota(
        {
            "sku": {"name": "GlobalStandard", "capacity": 50},
            "properties": {
                "rateLimits": [
                    {"key": "request", "renewalPeriod": 10, "count": 5},
                    {"key": "token", "renewalPeriod": 60, "count": 50000},
                ]
            },
        }
    )
    # An o-series ratio: 1 RPM per unit, not 6.
    assert quota == {
        "deployment_type": "GlobalStandard",
        "capacity": 50,
        "tpm": 50000,
        "rpm": 30,
        "quota_source": "rate_limits",
    }


def test_without_rate_limits_capacity_is_read_as_an_older_chat_model() -> None:
    quota = deployment_quota({"sku": {"name": "DataZoneStandard", "capacity": 20}})
    assert (quota["tpm"], quota["rpm"], quota["quota_source"]) == (20000, 120, "capacity")


def test_provisioned_throughput_has_no_token_rate() -> None:
    quota = deployment_quota({"sku": {"name": "ProvisionedManaged", "capacity": 100}})
    assert quota.get("tpm") is None and quota.get("quota_source") is None


@pytest.fixture
def azure() -> FakeAzure:
    return FakeAzure()


@pytest.fixture
def client(azure: FakeAzure) -> Iterator[TestClient]:
    yield _client(azure, _settings())
    app.dependency_overrides.clear()


def test_the_model_check_adds_the_pools_quota(client: TestClient, azure: FakeAzure) -> None:
    members = import_members(client, seed(azure))

    check = client.post("/api/litellm/pools/model-check", json={"members": members}).json()

    # Two deployments of 100 units: 100K TPM and 600 RPM each.
    assert [(m["tpm"], m["rpm"], m["quota_source"]) for m in check["members"]] == [
        (100000, 600, "rate_limits"),
        (100000, 600, "rate_limits"),
    ]
    assert (check["total_tpm"], check["total_rpm"]) == (200000, 1200)


def test_a_deployment_counts_once(client: TestClient, azure: FakeAzure) -> None:
    members = import_members(client, seed(azure))

    check = client.post(
        "/api/litellm/pools/model-check", json={"members": [members[0], members[0]]}
    ).json()

    assert check["total_tpm"] == 100000


def test_the_gateway_says_what_it_was_sized_for(azure: FakeAzure) -> None:
    sizing = GatewaySizing.model_validate(PRODUCTION)
    found = plan(sizing, "aca")
    parameters = {
        key: value["value"] for key, value in aca_parameters("gw", "eastus2", sizing, found).items()
    }
    azure.container_apps.clear()
    azure.add_gateway("gw", parameters=parameters)
    client = _client(azure, _settings())
    try:
        sized = client.get("/api/litellm/capabilities").json()["sized_for"]
    finally:
        app.dependency_overrides.clear()

    assert GatewaySizing.model_validate(sized) == sizing


def test_a_gateway_from_before_the_sizer_has_no_scenario(client: TestClient) -> None:
    assert client.get("/api/litellm/capabilities").json()["sized_for"] is None
