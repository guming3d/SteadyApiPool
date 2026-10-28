"""Resizing a gateway this app deployed: the read-only plan, what it applies with the user's
rights, what it leaves to the user, and the refusals."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from pool_manager.app import app
from pool_manager.litellm.gateway_template import (
    InstallSize,
    aca_parameters,
    aks_parameters,
    parse_sizing_tag,
)
from pool_manager.litellm.models import GatewaySizing
from pool_manager.litellm.sizing import plan
from tests.fake_azure import FakeAzure
from tests.litellm.fake_litellm import MASTER_KEY
from tests.litellm.test_app import PRODUCTION, TRIAL, WRITE, _client, _settings

APP = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/containerApps/gw"


def _aca(azure: FakeAzure, sizing: dict[str, Any] = TRIAL) -> TestClient:
    """A gateway deployed by this app for ``sizing``."""
    scenario = GatewaySizing.model_validate(sizing)
    parameters = {
        key: value["value"]
        for key, value in aca_parameters("gw", "eastus2", scenario, plan(scenario, "aca")).items()
    }
    azure.container_apps.clear()
    azure.add_gateway("gw", parameters=parameters)
    return _client(azure, _settings())


def _aks(azure: FakeAzure, sizing: dict[str, Any] = TRIAL) -> TestClient:
    scenario = GatewaySizing.model_validate(sizing)
    parameters = {
        key: value["value"]
        for key, value in aks_parameters(
            name="gw-aks",
            location="eastus2",
            sizing=scenario,
            plan=plan(scenario, "aks"),
            deployer="me",
            zones=[],
            postgres_password="x",
        ).items()
    }
    outputs = azure.add_aks_gateway(
        "gw-aks", group="rg-aks", subscription="sub", location="eastus2", parameters=parameters
    )
    return _client(
        azure,
        _settings(
            resource_group="rg-aks",
            gateway_name="gw-aks",
            gateway_url="https://gw-aks.eastus2.cloudapp.azure.com",
            gateway_master_key=MASTER_KEY,
            gateway_cluster_id=outputs["clusterId"],
        ),
    )


def _plan(client: TestClient, sizing: dict[str, Any] = PRODUCTION) -> dict[str, Any]:
    response = client.post("/api/litellm/gateway/resize/plan", json=sizing, headers=WRITE)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def _apply(client: TestClient, etag: str, **extra: Any) -> Any:
    body = {"sizing": PRODUCTION, "etag": etag, **extra}
    return client.post("/api/litellm/gateway/resize", json=body, headers=WRITE)


def test_the_plan_lists_each_change_and_changes_nothing() -> None:
    azure = FakeAzure()
    client = _aca(azure)
    try:
        resize = _plan(client)
    finally:
        app.dependency_overrides.clear()

    changes = {change["item"]: change for change in resize["changes"]}
    assert (changes["replicas"]["current"], changes["replicas"]["proposed"]) == ("1", "2-6")
    assert changes["replica_size"]["proposed"] == "2 vCPU, 4 GiB"
    assert {item: change["action"] for item, change in changes.items()} == {
        "replicas": "apply",
        "scale_rule": "apply",
        "replica_size": "apply",
        "database": "optional",
        "redis": "manual",
    }
    assert resize["sized_for"]["peak_rpm"] == TRIAL["peak_rpm"]
    assert not azure.writes


def test_container_apps_get_the_new_replicas_and_size() -> None:
    azure = FakeAzure()
    client = _aca(azure)
    try:
        etag = _plan(client)["etag"]
        response = _apply(client, etag)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["applied"] == ["replicas", "scale_rule", "replica_size"]
    method, path, body = azure.writes[0]
    assert (method, path) == ("PATCH", APP)
    template = body["properties"]["template"]
    assert (template["scale"]["minReplicas"], template["scale"]["maxReplicas"]) == (2, 6)
    assert template["scale"]["rules"][0]["http"]["metadata"]["concurrentRequests"] == "25"
    litellm, collector = template["containers"]
    assert litellm["resources"] == {"cpu": 2.0, "memory": "4Gi"}
    assert collector["resources"] == {"cpu": 0.25, "memory": "0.5Gi"}
    # The new scenario is recorded; the database wasn't asked for, so it stays.
    assert parse_sizing_tag(body["tags"]["ai-gateway-sizing"]).peak_rpm == PRODUCTION["peak_rpm"]
    assert body["tags"]["ai-gateway-postgres"] == "Standard_B1ms"
    assert len(azure.writes) == 1


def test_the_database_is_resized_only_when_asked() -> None:
    azure = FakeAzure()
    client = _aca(azure)
    try:
        response = _apply(client, _plan(client)["etag"], database=True)
    finally:
        app.dependency_overrides.clear()

    assert "database" in response.json()["applied"]
    method, path, body = azure.writes[0]
    assert (method, path.rsplit("/", 2)[-2]) == ("PATCH", "flexibleServers")
    assert body == {"sku": {"name": "Standard_D2ds_v5", "tier": "GeneralPurpose"}}
    assert azure.writes[1][2]["tags"]["ai-gateway-postgres"] == "Standard_D2ds_v5"


def test_a_gateway_that_changed_since_the_plan_is_refused() -> None:
    azure = FakeAzure()
    client = _aca(azure)
    try:
        etag = _plan(client)["etag"]
        azure.container_apps[APP.casefold()]["properties"]["template"]["scale"]["maxReplicas"] = 3
        response = _apply(client, etag)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert "review it again" in response.json()["detail"]
    assert not azure.writes


def test_writing_needs_the_csrf_header() -> None:
    azure = FakeAzure()
    client = _aca(azure)
    try:
        etag = _plan(client)["etag"]
        response = client.post(
            "/api/litellm/gateway/resize", json={"sizing": PRODUCTION, "etag": etag}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert not azure.writes


def test_a_gateway_this_app_didnt_deploy_isnt_resized() -> None:
    azure = FakeAzure()
    azure.container_apps.clear()
    azure.add_gateway("gw", managed=False)
    client = _client(azure, _settings())
    try:
        response = client.post("/api/litellm/gateway/resize/plan", json=PRODUCTION, headers=WRITE)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert "wasn't deployed by this app" in response.json()["detail"]


def test_aks_gets_new_nodes_and_pods_and_the_rest_as_commands() -> None:
    azure = FakeAzure()
    client = _aks(azure)
    try:
        resize = _plan(client)
        response = _apply(client, resize["etag"])
        command = response.json()["command_id"]
        for _ in range(5):
            status = client.get(f"/api/litellm/gateway/resize/{command}").json()
            if status["state"] != "running":
                break
    finally:
        app.dependency_overrides.clear()

    changes = {change["item"]: change for change in resize["changes"]}
    assert (changes["nodes"]["current"], changes["nodes"]["proposed"]) == ("1", "3")
    assert (changes["pods"]["current"], changes["pods"]["proposed"]) == ("1", "2-6")
    assert changes["tier"]["action"] == "manual"
    assert changes["tier"]["command"] == "az aks update -g rg-aks -n gw-aks --tier standard"
    assert "--load-balancer-outbound-ports" in changes["outbound"]["command"]

    assert response.json()["applied"] == ["nodes", "pods"]
    pool = next(body for method, path, body in azure.writes if path.endswith("/agentPools/system"))
    assert pool["properties"]["count"] == 3 and pool["properties"]["enableAutoScaling"] is False
    tags = next(body for method, path, body in azure.writes if method == "PATCH")["tags"]
    assert InstallSize.parse(tags["ai-gateway-install"]) == InstallSize(2, 6, 120, 10, 600)
    script = azure.run_commands[-1]["command"]
    assert script.startswith("helm upgrade litellm oci://ghcr.io/berriai/litellm-helm")
    assert "--reuse-values" in script and "--set autoscaling.maxReplicas=6" in script
    assert "context" not in azure.run_commands[-1]
    assert status["state"] == "succeeded"
