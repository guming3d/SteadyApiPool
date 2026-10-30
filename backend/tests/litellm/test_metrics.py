"""The pool dashboard: the gateway's spans in Application Insights and its Foundry members."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from pool_manager.app import app
from pool_manager.litellm.metrics import requests_query, spans_query
from pool_manager.metrics import expected_shares
from pool_manager.models import PoolMemberView
from tests.fake_azure import APP_INSIGHTS, FakeAzure
from tests.litellm.fake_litellm import MASTER_KEY, URL
from tests.litellm.test_app import WRITE, _client, _settings, create_pool

T1, T2 = "2026-10-24T06:00:00Z", "2026-10-24T06:01:00Z"
ACCOUNTS = "/subscriptions/sub/resourceGroups/rg-ai/providers/Microsoft.CognitiveServices/accounts"
EAST, WEST = f"{ACCOUNTS}/aoai-east", f"{ACCOUNTS}/aoai-west"


@pytest.fixture
def azure() -> FakeAzure:
    return FakeAzure()


@pytest.fixture
def client(azure: FakeAzure) -> Iterator[TestClient]:
    client = _client(azure, _settings())
    create_pool(client, azure)
    client.post("/api/litellm/pools/gpt6/aliases", json={"name": "gpt-4o"}, headers=WRITE)
    yield client
    app.dependency_overrides.clear()


def _spans(azure: FakeAzure) -> None:
    """Two minutes: 10 requests, one of them a 429 on east that west answered, then 2 final 429s."""
    azure.log_rows["by time"] = [
        {
            "time": f"{T1[:-1]}.0000000Z",
            "success": 9,
            "throttled": 0,
            "client_errors": 0,
            "server_errors": 0,
            "failovers": 1,
            "duration_ms": 900.0,
            "backend_ms": 800.0,
        },
        {
            "time": T2,
            "success": 1,
            "throttled": 2,
            "client_errors": 0,
            "server_errors": 0,
            "failovers": 0,
            "duration_ms": 300.0,
            "backend_ms": 700.0,
        },
    ]
    azure.log_rows["by host, target"] = [
        {
            "host": "aoai-east.openai.azure.com",
            "target": "azure/gpt6",
            "attempts": 8,
            "failures": 3,
            "throttled": 3,
        },
        {
            "host": "aoai-west.openai.azure.com",
            "target": "azure/gpt6",
            "attempts": 5,
            "failures": 2,
            "throttled": 2,
        },
    ]


def test_gateway_metrics_come_from_the_spans(client: TestClient, azure: FakeAzure) -> None:
    _spans(azure)

    body = client.get("/api/litellm/pools/gpt6/metrics").json()

    assert body["names"] == ["gpt6", "gpt-4o"]
    gateway = body["gateway"]
    assert (gateway["requests"], gateway["success"], gateway["throttled"]) == (12, 10, 2)
    assert gateway["failovers"] == 1
    assert gateway["availability"] == 1.0
    # Weighted by requests: (900 * 9 + 300 * 3) / 12.
    assert gateway["duration_ms"] == pytest.approx(750.0)
    assert [point["time"] for point in gateway["traffic"]] == [T1, T2]
    east, west = body["members"]
    assert (east["gateway_attempts"], east["gateway_failures"], east["gateway_throttled"]) == (
        8,
        3,
        3,
    )
    assert west["gateway_attempts"] == 5
    # Queried as the app, on the gateway's own Application Insights, for this window.
    query = azure.log_queries[0]
    assert query["resource"] == APP_INSIGHTS
    assert "'gpt6', 'gpt-4o'" in query["query"]
    assert query["timespan"].count("/") == 1
    logs = next(r for r in azure.requests if r.url.host == "api.loganalytics.azure.com")
    assert logs.headers["Authorization"] == "Bearer token"


def test_member_metrics_come_from_each_deployment(client: TestClient, azure: FakeAzure) -> None:
    for code, count in {"200": 50, "429": 5, "500": 1}.items():
        azure.add_metric(
            EAST, "AzureOpenAIRequests", [(T1, count)], ModelDeploymentName="gpt6", StatusCode=code
        )
    # Another deployment on the same account is not a member.
    azure.add_metric(
        EAST, "AzureOpenAIRequests", [(T1, 999)], ModelDeploymentName="other", StatusCode="200"
    )
    azure.add_metric(EAST, "TokenTransaction", [(T1, 12000)], ModelDeploymentName="gpt6")
    azure.add_metric(EAST, "AzureOpenAITTLTInMS", [(T1, 1200)], ModelDeploymentName="gpt6")
    # Azure reports a 0 ms average for an idle deployment; that is no latency, not a fast one.
    azure.add_metric(WEST, "AzureOpenAITTLTInMS", [(T1, 0)], ModelDeploymentName="gpt6")

    members = client.get("/api/litellm/pools/gpt6/metrics").json()["members"]

    east, west = members
    assert east["deployment_id"] == f"{EAST}/deployments/gpt6"
    assert (east["region"], east["requests"], east["throttled"], east["server_errors"]) == (
        "eastus2",
        56,
        5,
        1,
    )
    assert (east["tokens"], east["latency_ms"], east["utilization_pct"]) == (12000, 1200, None)
    # No traffic in the window: zeros, not unknown.
    assert (west["requests"], west["tokens"], west["latency_ms"]) == (0, 0, None)
    # Failover: the primary takes everything while it is up.
    assert [m["expected_share"] for m in members] == [1.0, 0.0]
    assert all(m["error"] is None for m in members)


def test_no_access_to_the_spans_is_a_note(client: TestClient, azure: FakeAzure) -> None:
    azure.logs_forbidden.add("app")

    body = client.get("/api/litellm/pools/gpt6/metrics", params={"refresh": "true"}).json()

    assert body["gateway"] is None
    assert body["gateway_error"] == "forbidden"
    assert len(body["members"]) == 2


def test_missing_metrics_permission_is_reported(client: TestClient, azure: FakeAzure) -> None:
    azure.allow("app", "/subscriptions/sub", "Microsoft.App/*", "Microsoft.CognitiveServices/*")

    body = client.get("/api/litellm/pools/gpt6/metrics", params={"refresh": "true"}).json()

    assert all("metrics/read" in member["error"] for member in body["members"])


def test_results_are_cached_per_pool_and_range(client: TestClient, azure: FakeAzure) -> None:
    client.get("/api/litellm/pools/gpt6/metrics")
    first = len(azure.log_queries)
    client.get("/api/litellm/pools/gpt6/metrics")
    assert len(azure.log_queries) == first
    client.get("/api/litellm/pools/gpt6/metrics", params={"range": "24h"})
    client.get("/api/litellm/pools/gpt6/metrics", params={"refresh": "true"})
    assert len(azure.log_queries) == first * 3
    assert client.get("/api/litellm/pools/gpt6/metrics", params={"range": "1y"}).status_code == 422


def test_a_gateway_without_app_insights(azure: FakeAzure) -> None:
    settings = _settings(resource_group="", gateway_url=URL, gateway_master_key=MASTER_KEY)
    client = _client(azure, settings)
    try:
        create_pool(client, azure)
        body = client.get("/api/litellm/pools/gpt6/metrics").json()
    finally:
        app.dependency_overrides.clear()

    assert (body["gateway"], body["gateway_error"]) == (None, "no_app_insights")
    assert not azure.log_queries


def test_the_queries_only_count_model_calls_of_the_pool() -> None:
    query = spans_query(["GPT6", "it's"], "2026-10-24T06:00:00Z", "2026-10-24T07:00:00Z")
    assert "model in ('gpt6', 'it\\'s')" in query
    assert 'name startswith "chat "' not in query and "name startswith 'chat '" in query
    by_request = requests_query(["gpt6"], "a", "b", "5m")
    assert "by operation_Id" in by_request and "bin(started, 5m)" in by_request


def test_expected_shares_follow_priority_and_weight() -> None:
    members = [
        PoolMemberView(backend_id="a", priority=0, weight=3),
        PoolMemberView(backend_id="b", priority=0, weight=1),
        PoolMemberView(backend_id="c", priority=1, weight=1),
    ]
    assert expected_shares(members) == [0.75, 0.25, 0.0]
    zero = [PoolMemberView(backend_id=x, priority=2, weight=0) for x in "ab"]
    assert expected_shares(zero) == [0.5, 0.5]
    assert expected_shares([]) == []


def test_the_queries_avoid_kql_pitfalls() -> None:
    # The fake can't parse KQL; these broke the real query API once.
    query = requests_query(["gpt"], "2026-10-26T09:00:00Z", "2026-10-26T10:00:00Z", "1m")
    # time is a keyword: as a column name it has to be quoted.
    assert "by ['time'] = bin(" in query and "by time" not in query
    # A name on arg_max goes to the column it maximizes, not the one it returns.
    assert "= arg_max(" not in query and "arg_max(timestamp, last_code)" in query


GROUP = "/subscriptions/sub/resourceGroups/rg/providers"
GATEWAY_APP = f"{GROUP}/Microsoft.App/containerApps/gw"
POSTGRES = f"{GROUP}/Microsoft.DBforPostgreSQL/flexibleServers/psql-gw-x1"


def test_gateway_dashboard_reads_the_container_app_and_database(
    client: TestClient, azure: FakeAzure
) -> None:
    _spans(azure)
    azure.add_metric(GATEWAY_APP, "Replicas", [(T1, 1), (T2, 1)])
    # A dev gateway's replica has 1.25 cores and 2.5 GiB (LiteLLM and the collector).
    azure.add_metric(GATEWAY_APP, "UsageNanoCores", [(T1, 0.25e9), (T2, 0.625e9)])
    azure.add_metric(GATEWAY_APP, "WorkingSetBytes", [(T1, 1.25 * 2**30)])
    azure.add_metric(POSTGRES, "cpu_percent", [(T1, 12)])
    azure.add_metric(POSTGRES, "active_connections", [(T1, 7), (T2, 9)])

    response = client.get("/api/litellm/metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["traffic"]["requests"] == 12
    # Every model's spans: the query names none.
    assert "model in" not in azure.log_queries[-1]["query"]
    series = {(s["resource"], s["metric"]): s for s in body["series"]}
    assert set(series) == {
        ("gw", "replicas"),
        ("gw", "cpu"),
        ("gw", "memory"),
        ("psql-gw-x1", "db_cpu"),
        ("psql-gw-x1", "db_connections"),
    }
    assert series[("gw", "replicas")]["limit"] == 1
    assert series[("gw", "cpu")]["peak"] == pytest.approx(50)
    assert series[("gw", "cpu")]["average"] == pytest.approx(35)
    assert series[("gw", "memory")]["latest"] == pytest.approx(50)
    assert series[("psql-gw-x1", "db_connections")]["unit"] == "count"
    assert body["problems"] == []


def test_gateway_dashboard_reads_an_aks_cluster(azure: FakeAzure) -> None:
    outputs = azure.add_aks_gateway("gw-aks", group="rg-aks", subscription="sub")
    cluster = outputs["clusterId"]
    database = cluster.replace(
        "Microsoft.ContainerService/managedClusters/gw-aks",
        "Microsoft.DBforPostgreSQL/flexibleServers/psql-gw-aks-x1",
    )
    azure.add_metric(cluster, "node_cpu_usage_percentage", [(T1, 70)])
    azure.add_metric(cluster, "node_memory_working_set_percentage", [(T1, 55)])
    azure.add_metric(database, "memory_percent", [(T1, 30)])
    client = _client(
        azure,
        _settings(
            resource_group="rg-aks",
            gateway_name="gw-aks",
            gateway_url="https://gw-aks.swedencentral.cloudapp.azure.com",
            gateway_master_key=MASTER_KEY,
            gateway_cluster_id=cluster,
        ),
    )
    try:
        body = client.get("/api/litellm/metrics").json()
    finally:
        app.dependency_overrides.clear()

    assert {(s["resource"], s["metric"]) for s in body["series"]} == {
        ("gw-aks", "node_cpu"),
        ("gw-aks", "node_memory"),
        ("psql-gw-aks-x1", "db_memory"),
    }


def test_a_connected_gateway_has_only_its_traffic(azure: FakeAzure) -> None:
    settings = _settings(resource_group="", gateway_url=URL, gateway_master_key=MASTER_KEY)
    client = _client(azure, settings)
    try:
        body = client.get("/api/litellm/metrics").json()
    finally:
        app.dependency_overrides.clear()

    assert (body["traffic"], body["traffic_error"]) == (None, "no_app_insights")
    assert (body["series"], body["problems"]) == ([], [])


def test_no_access_to_the_database_is_a_note(client: TestClient, azure: FakeAzure) -> None:
    # As the deployed app's role: the Container App and its metrics, nothing else in the group.
    azure.allow("app", GATEWAY_APP, "Microsoft.App/*", "Microsoft.Insights/metrics/read")
    azure.add_metric(GATEWAY_APP, "Replicas", [(T1, 2)])

    body = client.get("/api/litellm/metrics", params={"refresh": "true"}).json()

    assert [s["metric"] for s in body["series"]] == ["replicas"]
    assert body["problems"] == [{"resource": "rg", "error": "forbidden_resources"}]
