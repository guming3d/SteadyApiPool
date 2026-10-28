"""A gateway deployment checks the region before it creates anything, and when one of its
resources fails anyway, it stops and deletes what it created."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pool_manager.app import app
from pool_manager.arm import ArmClient
from pool_manager.config import Settings
from pool_manager.litellm.cleanup import GatewayCleanup
from tests.fake_azure import SUBSCRIPTION, TENANT, FakeAzure, FakeCredential
from tests.litellm.test_app import PRODUCTION, TRIAL, WRITE, _settings
from tests.litellm.test_target import DEPLOYMENT, NO_TARGET, _create, _Wizard

GROUP = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-new"
STATUS = {"subscription_id": SUBSCRIPTION, "resource_group": "rg-new", "name": "gw-new"}
IDENTITY = "Microsoft.ManagedIdentity/userAssignedIdentities"
WORKSPACE = "Microsoft.OperationalInsights/workspaces"
ENVIRONMENT = "Microsoft.App/managedEnvironments"


@pytest.fixture
def azure() -> FakeAzure:
    return FakeAzure()


@pytest.fixture
def wizard(azure: FakeAzure, tmp_path: Path) -> Iterator[_Wizard]:
    yield _Wizard(azure, _settings(**NO_TARGET), tmp_path)
    app.dependency_overrides.clear()


def _status(wizard: _Wizard) -> dict[str, Any]:
    response = wizard.client.get("/api/litellm/target/deploy-status", params=STATUS)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def _preflight(wizard: _Wizard, **overrides: Any) -> dict[str, Any]:
    body = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-new",
        "location": "eastus2",
        "name": "gw-new",
        "sizing": TRIAL,
        **overrides,
    }
    response = wizard.client.post(
        "/api/litellm/target/gateways/preflight", json=body, headers=WRITE
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


# -- before anything is created -------------------------------------------------------------


def test_every_problem_in_the_region_is_reported_before_anything_is_created(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    azure.postgres_skus["eastus2"] = ["Standard_B1ms"]  # No D2ds_v5 for production.
    azure.environments_free["eastus2"] = 0
    azure.offered["Microsoft.Cache/redisEnterprise"] = ["Sweden Central"]

    response = _create(wizard, sizing=PRODUCTION)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "can't be deployed in eastus2, so nothing was created" in detail
    assert "no General Purpose Standard_D2ds_v5 in eastus2" in detail
    assert "Container Apps environments has 0 free" in detail
    assert "Azure doesn't offer Azure Managed Redis in eastus2" in detail
    # The regions where all of it fits are offered.
    assert "Regions where it fits: swedencentral" in detail
    assert not azure.arm_deployments
    assert GROUP.casefold() not in azure.resource_groups


def test_postgres_quota_is_checked(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.postgres_vcores_free["eastus2"] = 1

    dev = _preflight(wizard)
    production = _preflight(wizard, sizing=PRODUCTION)

    assert dev["problems"] == []
    assert dev["open_regions"] == []  # Only looked up when something is in the way.
    assert production["problems"] == [
        "Not enough quota in eastus2: PostgreSQL General Purpose vCores has 1 free, the gateway "
        "needs 2. Request more in the portal (Quotas) or choose another region."
    ]
    assert "swedencentral" in production["open_regions"]


def test_the_preflight_changes_nothing(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.postgres_restricted["eastus2"] = "Subscriptions are restricted in this region."

    result = _preflight(wizard)

    assert result["location"] == "eastus2"
    assert result["problems"] == [
        "The gateway's database (PostgreSQL Flexible Server) can't be created in eastus2 with "
        "this subscription: Subscriptions are restricted in this region."
    ]
    assert {request.method for request in azure.requests} == {"GET"}


def test_an_aks_preflight_checks_the_nodes(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.vcpus_free = 8

    dev = _preflight(wizard, platform="aks")
    production = _preflight(wizard, platform="aks", sizing=PRODUCTION)

    assert dev["problems"] == []
    assert production["problems"][0].startswith(
        "Not enough quota in eastus2: DDSv5 vCPUs has 8 free, the gateway needs 12."
    )


def test_azures_validation_refuses_before_anything_is_created(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    azure.validation_errors = [
        ("SkuNotAvailable", "The requested size for resource psql-gw-new is not available"),
        ("RequestDisallowedByPolicy", "Resource redis-gw-new was disallowed by policy"),
    ]

    response = _create(wizard)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail.startswith("Azure's check of the deployment failed, so nothing was created.")
    assert (
        "SkuNotAvailable: The requested size for resource psql-gw-new is not available." in detail
    )
    assert "RequestDisallowedByPolicy" in detail
    assert not azure.arm_deployments
    # The group it made for the deployment went again; it was empty.
    assert azure.deleted_groups == [GROUP.casefold()]


def test_a_group_that_was_there_stays_when_validation_fails(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    azure.add_resource_group("rg-new")
    azure.validation_errors = [("SkuNotAvailable", "Not available")]

    assert _create(wizard).status_code == 409

    assert GROUP.casefold() in azure.resource_groups
    assert azure.deleted_groups == []


def test_the_deployment_is_validated_as_it_is_sent(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.validation_async = True  # Azure may answer 202 and the outcome later.

    assert _create(wizard).status_code == 200

    sent = azure.arm_deployments[DEPLOYMENT.casefold()]
    (validated,) = azure.validations
    assert validated["properties"]["template"] == sent["template"]
    assert (
        validated["tags"]
        == sent["tags"]
        == {
            "managed-by": "ai-gateway-pool",
            "ai-gateway-on-failure": "delete",
        }
    )


# -- when a resource fails anyway -----------------------------------------------------------


def test_a_failing_resource_stops_the_deployment_and_its_resources_are_deleted(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    assert _create(wizard).status_code == 200
    created = azure.fail_resource("gw-new", created=(IDENTITY, WORKSPACE, ENVIRONMENT))

    running = _status(wizard)
    assert running["state"] == "running"
    assert azure.canceled == [DEPLOYMENT.casefold()]  # Stopped at once: nothing more starts.

    stopped = _status(wizard)

    assert stopped["state"] == "failed"
    assert stopped["error"] == "ResourceDeploymentFailure: Insufficient capacity in this region."
    assert stopped["cleanup"] == {
        "state": "done",
        "deleted": ["cae-gw-new", "id-gw-new", "log-gw-new"],
        "deleting": [],
        "error": None,
    }
    assert all(resource.casefold() not in azure.resources for resource in created)
    # The resource group and the deployment's record (with the error) stay.
    assert GROUP.casefold() in azure.resource_groups
    assert DEPLOYMENT.casefold() in azure.arm_deployments
    # Cleaned up once: later reads only report it.
    deletes = sum(request.method == "DELETE" for request in azure.requests)
    assert _status(wizard)["cleanup"]["state"] == "done"
    assert sum(request.method == "DELETE" for request in azure.requests) == deletes
    # Nothing is left in the way of deploying the name again.
    assert _create(wizard).status_code == 200


def test_the_cleanup_leaves_azure_to_finish_slow_deletes(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.slow_deletes = {ENVIRONMENT.casefold()}
    _create(wizard)
    azure.fail_resource("gw-new", created=(IDENTITY, ENVIRONMENT))
    _status(wizard)

    cleanup = _status(wizard)["cleanup"]

    assert cleanup["state"] == "done"
    assert cleanup["deleting"] == ["cae-gw-new"]


def test_the_cleanup_tries_again_while_azure_refuses(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.refused_deletes = {IDENTITY.casefold(): 2}  # Still being created when it stopped.
    _create(wizard)
    (identity,) = azure.fail_resource("gw-new")
    _status(wizard)

    cleanup = _status(wizard)["cleanup"]

    assert (cleanup["state"], cleanup["deleted"]) == ("done", ["id-gw-new"])
    assert identity.casefold() not in azure.resources


def test_a_cleanup_that_azure_keeps_refusing_says_so(azure: FakeAzure) -> None:
    # An hour passes between passes: the second pass is past the cleanup's time.
    clock = iter(range(0, 10_000_000, 60 * 60))
    cleanup = GatewayCleanup(run=lambda job: job(), sleep=lambda _: None, clock=lambda: next(clock))
    azure.refused_deletes = {IDENTITY.casefold(): 1000}
    azure.add_resource_group("rg-new")
    resource = f"{GROUP}/providers/{IDENTITY}/id-gw-new"
    azure.resources[resource.casefold()] = {
        "id": resource,
        "name": "id-gw-new",
        "type": IDENTITY,
        "tags": {"managed-by": "ai-gateway-pool", "ai-gateway": "gw-new"},
    }
    arm = ArmClient(Settings(), FakeCredential(), azure.transport(), sleep=lambda _: None)

    result = cleanup.start("key", arm, SUBSCRIPTION, "rg-new", "gw-new")

    assert result.state == "failed"
    assert result.error == "id-gw-new: Conflict"
    assert resource.casefold() in azure.resources


def test_without_cleanup_on_failure_a_failed_deployment_is_left_alone(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    assert _create(wizard, cleanup_on_failure=False).status_code == 200
    (identity,) = azure.fail_resource("gw-new")

    status = _status(wizard)

    assert status["auto_cleanup"] is False
    assert status["cleanup"] is None
    assert azure.canceled == []
    assert identity.casefold() in azure.resources
    assert "ai-gateway-on-failure" not in azure.arm_deployments[DEPLOYMENT.casefold()]["tags"]
