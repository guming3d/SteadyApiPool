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
from pool_manager.litellm.sizing import node_in_family, plan
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


def _aks(azure: FakeAzure, sizing: dict[str, Any] = TRIAL, family: int = 0) -> TestClient:
    """A cluster this app deployed; ``family`` picks its nodes' family (sizing.NODE_FAMILIES),
    as the region check does where the first isn't open."""
    scenario = GatewaySizing.model_validate(sizing)
    found = plan(scenario, "aks")
    assert found.aks is not None
    node_size = node_in_family(found.aks.node_size, family)
    found = found.model_copy(update={"aks": found.aks.model_copy(update={"node_size": node_size})})
    parameters = {
        key: value["value"]
        for key, value in aks_parameters(
            name="gw-aks",
            location="eastus2",
            sizing=scenario,
            plan=found,
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


def _apply(client: TestClient, etag: str, sizing: dict[str, Any] = PRODUCTION, **extra: Any) -> Any:
    body = {"sizing": sizing, "etag": etag, **extra}
    return client.post("/api/litellm/gateway/resize", json=body, headers=WRITE)


def _cluster_tags(azure: FakeAzure) -> dict[str, str]:
    cluster = next(item for key, item in azure.resources.items() if key.endswith("/gw-aks"))
    return dict(cluster["tags"])


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
        # Six replicas' connections don't fit the trial's B1ms: resizing it isn't optional.
        "database": "apply",
        "redis": "manual",
        # Azure makes an environment zone-redundant only when it's created.
        "zones": "manual",
        "database_ha": "manual",
    }
    assert changes["zones"]["command"] is None
    assert changes["database_ha"]["command"] == (
        "az postgres flexible-server update -g rg -n psql-gw-x1 --high-availability ZoneRedundant"
    )
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
    assert response.json()["applied"] == ["database", "replicas", "scale_rule", "replica_size"]
    # The database first, so the new replicas find their connections.
    method, path, body = azure.writes[0]
    assert (method, path.rsplit("/", 2)[-2]) == ("PATCH", "flexibleServers")
    assert body == {"sku": {"name": "Standard_D2ds_v5", "tier": "GeneralPurpose"}}
    method, path, body = azure.writes[1]
    assert (method, path) == ("PATCH", APP)
    template = body["properties"]["template"]
    assert (template["scale"]["minReplicas"], template["scale"]["maxReplicas"]) == (2, 6)
    http, cpu = template["scale"]["rules"]
    assert http["http"]["metadata"]["concurrentRequests"] == "239"
    assert cpu["custom"] == {"type": "cpu", "metadata": {"type": "Utilization", "value": "27"}}
    litellm, collector = template["containers"]
    assert litellm["resources"] == {"cpu": 2.0, "memory": "4Gi"}
    assert collector["resources"] == {"cpu": 0.25, "memory": "0.5Gi"}
    # The new scenario is recorded, with the database it has now.
    assert parse_sizing_tag(body["tags"]["ai-gateway-sizing"]).peak_rpm == PRODUCTION["peak_rpm"]
    assert body["tags"]["ai-gateway-postgres"] == "Standard_D2ds_v5"
    assert len(azure.writes) == 2


def test_a_database_that_holds_the_replicas_is_resized_only_when_asked() -> None:
    azure = FakeAzure()
    client = _aca(azure, PRODUCTION)
    try:
        resize = _plan(client, TRIAL)
        kept = _apply(client, resize["etag"], TRIAL)
        resize = _plan(client, TRIAL)
        smaller = _apply(client, resize["etag"], TRIAL, database=True)
    finally:
        app.dependency_overrides.clear()

    changes = {change["item"]: change["action"] for change in resize["changes"]}
    assert changes["database"] == "optional"
    assert "database" not in kept.json()["applied"]
    assert "database" in smaller.json()["applied"]
    database = [body for _, path, body in azure.writes if "flexibleServers" in path]
    # Burstable has no standby: it goes first, then the size.
    assert database == [
        {"properties": {"highAvailability": {"mode": "Disabled"}}},
        {"sku": {"name": "Standard_B1ms", "tier": "Burstable"}},
    ]
    tags = azure.writes[-1][2]["tags"]
    assert (tags["ai-gateway-postgres"], tags["ai-gateway-postgres-ha"]) == (
        "Standard_B1ms",
        "Disabled",
    )


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


def test_a_cluster_on_another_node_family_keeps_its_family() -> None:
    azure = FakeAzure()
    client = _aks(azure, family=1)
    try:
        resize = _plan(client)
    finally:
        app.dependency_overrides.clear()

    assert "node_size" not in {change["item"] for change in resize["changes"]}


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

    assert changes["zones"]["action"] == "manual"
    assert "--zones 1 2 3" in changes["zones"]["command"]

    assert response.json()["applied"] == ["database", "nodes", "pods"]
    pool = next(body for method, path, body in azure.writes if path.endswith("/agentPools/system"))
    assert pool["properties"]["count"] == 3 and pool["properties"]["enableAutoScaling"] is False
    cluster = [body["tags"] for _, path, body in azure.writes if path.endswith("/gw-aks")]
    # While the command runs, the new size waits next to the old one...
    assert InstallSize.parse(cluster[0]["ai-gateway-install"]) == InstallSize()
    assert InstallSize.parse(cluster[0]["ai-gateway-resize-install"]) == InstallSize(
        2, 6, 120, 10, 600
    )
    assert cluster[0]["ai-gateway-resize-command"] == command
    # ...and becomes the cluster's once it has succeeded.
    tags = _cluster_tags(azure)
    assert InstallSize.parse(tags["ai-gateway-install"]) == InstallSize(2, 6, 120, 10, 600)
    assert parse_sizing_tag(tags["ai-gateway-sizing"]).peak_rpm == PRODUCTION["peak_rpm"]
    assert not any(key.startswith("ai-gateway-resize-") for key in tags)
    script = azure.run_commands[-1]["command"]
    assert script.startswith("helm upgrade litellm oci://ghcr.io/berriai/litellm-helm")
    assert "--reuse-values" in script and "--set autoscaling.maxReplicas=6" in script
    assert "context" not in azure.run_commands[-1]
    assert status["state"] == "succeeded"


def test_a_failed_change_to_the_pods_is_offered_again() -> None:
    azure = FakeAzure()
    azure.run_command_exit = 1
    client = _aks(azure)
    try:
        response = _apply(client, _plan(client)["etag"])
        command = response.json()["command_id"]
        for _ in range(5):
            status = client.get(f"/api/litellm/gateway/resize/{command}").json()
            if status["state"] != "running":
                break
        again = _plan(client)
    finally:
        app.dependency_overrides.clear()

    assert status["state"] == "failed"
    # The cluster still says what it runs: the trial's pods and scenario.
    tags = _cluster_tags(azure)
    assert InstallSize.parse(tags["ai-gateway-install"]) == InstallSize()
    assert parse_sizing_tag(tags["ai-gateway-sizing"]).peak_rpm == TRIAL["peak_rpm"]
    assert not any(key.startswith("ai-gateway-resize-") for key in tags)
    changes = {change["item"]: change for change in again["changes"]}
    assert (changes["pods"]["current"], changes["pods"]["proposed"]) == ("1", "2-6")
    assert again["sized_for"]["peak_rpm"] == TRIAL["peak_rpm"]


def test_a_failed_change_nobody_followed_is_offered_again() -> None:
    azure = FakeAzure()
    azure.run_command_exit = 1
    azure.run_command_polls = 0
    client = _aks(azure)
    try:
        _apply(client, _plan(client)["etag"])
        # The dialog was closed: the plan finds the command failed, without writing anything.
        writes = len(azure.writes)
        again = _plan(client)
    finally:
        app.dependency_overrides.clear()

    changes = {change["item"]: change for change in again["changes"]}
    assert (changes["pods"]["current"], changes["pods"]["proposed"]) == ("1", "2-6")
    assert again["sized_for"]["peak_rpm"] == TRIAL["peak_rpm"]
    assert len(azure.writes) == writes


def test_a_resize_waits_for_the_last_change_to_the_pods() -> None:
    azure = FakeAzure()
    azure.run_command_polls = 100
    client = _aks(azure)
    try:
        _apply(client, _plan(client)["etag"])
        resize = _plan(client)
        response = _apply(client, resize["etag"])
    finally:
        app.dependency_overrides.clear()

    # The running change isn't offered again, and nothing else starts until it ends.
    assert "pods" not in {change["item"] for change in resize["changes"]}
    assert response.status_code == 409
    assert "still running" in response.json()["detail"]
    assert len(azure.run_commands) == 1
