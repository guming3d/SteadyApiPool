"""The pool dashboard: Azure Monitor metrics for a pool's APIs and its Foundry members."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from pool_manager.apim.metrics import ApimMonitor
from pool_manager.app import app, get_apim_monitor, get_apim_service
from pool_manager.arm import ArmClient
from pool_manager.metrics import expected_shares
from pool_manager.models import PoolMemberView
from tests.apim.fake_apim import SERVICE, FakeApim, FakeCredential
from tests.apim.test_api_test import API, _pool_api
from tests.apim.test_app import WRITE, _client, _settings

T1, T2 = "2026-10-24T06:00:00Z", "2026-10-24T06:01:00Z"
ACCOUNTS = "/subscriptions/sub/resourceGroups/rg-ai/providers/Microsoft.CognitiveServices/accounts"
EAST, WEST = f"{ACCOUNTS}/aoai-east", f"{ACCOUNTS}/aoai-west"


@pytest.fixture
def apim() -> FakeApim:
    return FakeApim()


@pytest.fixture
def client(apim: FakeApim) -> Iterator[TestClient]:
    settings = _settings()
    client = _client(apim, settings)
    arm = ArmClient(settings, FakeCredential(), apim.transport(), sleep=lambda _: None)
    monitor = ApimMonitor(settings, arm, app.dependency_overrides[get_apim_service]())
    app.dependency_overrides[get_apim_monitor] = lambda: monitor
    _pool_api(client, apim)
    yield client
    app.dependency_overrides.clear()


def _metrics_requests(apim: FakeApim) -> list[dict[str, str]]:
    return [
        dict(r.url.params)
        for r in apim.requests
        if r.url.path.endswith("/Microsoft.Insights/metrics")
    ]


def test_gateway_metrics_cover_only_the_pools_apis(client: TestClient, apim: FakeApim) -> None:
    for code, points in {
        "200": [(T1, 10), (T2, 20)],
        "429": [(T2, 2)],
        "500": [(T1, 1)],
        "404": [(T2, 3)],
    }.items():
        apim.add_metric(SERVICE, "Requests", points, ApiId=API, GatewayResponseCode=code)
    apim.add_metric(SERVICE, "Requests", [(T1, 100)], ApiId="other", GatewayResponseCode="200")
    apim.add_metric(SERVICE, "Duration", [(T1, 800), (T2, 1000)], ApiId=API)
    apim.add_metric(SERVICE, "BackendDuration", [(T1, 700), (T2, 900)], ApiId=API)

    response = client.get(f"/api/apim/pools/{API}/metrics", params={"range": "1h"})

    assert response.status_code == 200, response.text
    body = response.json()
    gateway = body["gateway"]
    assert body["names"] == [API]
    assert body["interval"] == "PT1M"
    assert (gateway["requests"], gateway["success"], gateway["throttled"]) == (36, 30, 2)
    assert (gateway["client_errors"], gateway["server_errors"]) == (3, 1)
    assert gateway["availability"] == pytest.approx(35 / 36)
    # Weighted by each minute's requests: 11 at 800 ms, 25 at 1000 ms.
    assert gateway["duration_ms"] == pytest.approx((800 * 11 + 1000 * 25) / 36)
    assert gateway["backend_duration_ms"] == pytest.approx((700 * 11 + 900 * 25) / 36)
    assert gateway["traffic"] == [
        {"time": T1, "success": 10, "throttled": 0, "client_errors": 0, "server_errors": 1},
        {"time": T2, "success": 20, "throttled": 2, "client_errors": 3, "server_errors": 0},
    ]
    assert gateway["latency"][0] == {"time": T1, "overall_ms": 800, "backend_ms": 700}
    filters = {params["metricnames"]: params["$filter"] for params in _metrics_requests(apim)}
    assert filters["Requests"] == f"ApiId eq '{API}' and GatewayResponseCode eq '*'"
    assert filters["Duration,BackendDuration"] == f"ApiId eq '{API}'"


def test_member_metrics_come_from_each_deployment(client: TestClient, apim: FakeApim) -> None:
    for code, count in {"200": 50, "429": 5, "500": 1}.items():
        apim.add_metric(
            EAST,
            "AzureOpenAIRequests",
            [(T1, count)],
            ModelDeploymentName="gpt-4o",
            StatusCode=code,
        )
    # Another deployment on the same account is not a member.
    apim.add_metric(
        EAST, "AzureOpenAIRequests", [(T1, 999)], ModelDeploymentName="other", StatusCode="200"
    )
    apim.add_metric(EAST, "TokenTransaction", [(T1, 12000)], ModelDeploymentName="gpt-4o")
    apim.add_metric(EAST, "AzureOpenAITTLTInMS", [(T1, 1200)], ModelDeploymentName="gpt-4o")
    # Azure reports a 0 ms average for an idle deployment; that is no latency, not a fast one.
    apim.add_metric(WEST, "AzureOpenAITTLTInMS", [(T1, 0)], ModelDeploymentName="gpt-4o-2")

    members = client.get(f"/api/apim/pools/{API}/metrics").json()["members"]

    east, west = members
    assert east["deployment_id"] == f"{EAST}/deployments/gpt-4o"
    assert (east["region"], east["requests"], east["throttled"], east["server_errors"]) == (
        "eastus2",
        56,
        5,
        1,
    )
    assert (east["tokens"], east["latency_ms"], east["utilization_pct"]) == (12000, 1200, None)
    # No traffic in the window: zeros, not unknown.
    assert (west["requests"], west["tokens"], west["latency_ms"]) == (0, 0, None)
    assert [m["expected_share"] for m in members] == [0.5, 0.5]
    assert all(m["error"] is None for m in members)
    requests_filter = next(
        p["$filter"] for p in _metrics_requests(apim) if p["metricnames"] == "AzureOpenAIRequests"
    )
    assert requests_filter == "ModelDeploymentName eq '*' and StatusCode eq '*'"
    assert all(
        p["interval"] == "FULL" for p in _metrics_requests(apim) if "Azure" in p["metricnames"]
    )


def test_missing_metrics_permission_is_reported(client: TestClient, apim: FakeApim) -> None:
    apim.callers["token"] = "app"
    # Everything the app needs except Microsoft.Insights/metrics/read.
    apim.allow(
        "app", "/subscriptions/sub", "Microsoft.ApiManagement/*", "Microsoft.CognitiveServices/*"
    )

    body = client.get(f"/api/apim/pools/{API}/metrics", params={"refresh": "true"}).json()

    assert body["gateway"] is None
    assert "Microsoft.Insights/metrics/read" in body["gateway_error"]
    assert all("metrics/read" in member["error"] for member in body["members"])


def test_results_are_cached_per_pool_and_range(client: TestClient, apim: FakeApim) -> None:
    client.get(f"/api/apim/pools/{API}/metrics")
    first = len(_metrics_requests(apim))
    client.get(f"/api/apim/pools/{API}/metrics")
    assert len(_metrics_requests(apim)) == first
    client.get(f"/api/apim/pools/{API}/metrics", params={"range": "24h"})
    client.get(f"/api/apim/pools/{API}/metrics", params={"refresh": "true"})
    assert len(_metrics_requests(apim)) == first * 3
    assert client.get(f"/api/apim/pools/{API}/metrics", params={"range": "1y"}).status_code == 422


def test_pool_without_an_api_has_no_gateway_metrics(client: TestClient) -> None:
    members = client.get(f"/api/apim/pools/{API}").json()["members"]
    body = {"members": [{**m, "priority": 0} for m in members], "title": "spare"}
    assert client.put("/api/apim/pools/pool-spare", json=body, headers=WRITE).status_code == 200

    metrics = client.get("/api/apim/pools/pool-spare/metrics").json()

    assert (metrics["names"], metrics["gateway"], metrics["gateway_error"]) == ([], None, None)
    assert len(metrics["members"]) == 2


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


def test_gateway_dashboard_covers_the_whole_service(client: TestClient, apim: FakeApim) -> None:
    apim.add_metric(SERVICE, "Requests", [(T1, 10), (T2, 20)], ApiId=API, GatewayResponseCode="200")
    apim.add_metric(SERVICE, "Requests", [(T1, 5)], ApiId="other", GatewayResponseCode="429")
    apim.add_metric(SERVICE, "Duration", [(T1, 800), (T2, 1000)])
    apim.add_metric(SERVICE, "BackendDuration", [(T1, 700), (T2, 900)])
    apim.add_metric(SERVICE, "Capacity", [(T1, 20), (T2, 60)])

    response = client.get("/api/apim/metrics", params={"range": "6h"})

    assert response.status_code == 200, response.text
    body = response.json()
    traffic = body["traffic"]
    assert (body["range"], body["interval"]) == ("6h", "PT5M")
    # Every API's requests, not only those of a pool.
    assert (traffic["requests"], traffic["success"], traffic["throttled"]) == (35, 30, 5)
    assert traffic["duration_ms"] == pytest.approx((800 * 15 + 1000 * 20) / 35)
    requests = [p for p in _metrics_requests(apim) if p["metricnames"] == "Requests"]
    assert [p["$filter"] for p in requests] == ["GatewayResponseCode eq '*'"]
    # The classic tiers' Capacity; a v2 tier's CPU and memory have no points here.
    [capacity] = body["series"]
    assert (capacity["metric"], capacity["unit"], capacity["resource"]) == (
        "capacity",
        "percent",
        SERVICE.rpartition("/")[2],
    )
    # Capacity's Maximum is 0 below Premium: its peak is the busiest bin's average.
    assert (capacity["average"], capacity["peak"], capacity["latest"]) == (40, 60, 60)
    assert [p["peak"] for p in capacity["points"]] == [None, None]
    assert not any(
        p["metricnames"] == "Capacity" and p["aggregation"] == "Maximum"
        for p in _metrics_requests(apim)
    )
    assert body["problems"] == []


def test_gateway_dashboard_reads_v2_cpu_and_memory(client: TestClient, apim: FakeApim) -> None:
    apim.add_metric(SERVICE, "CpuPercent_Gateway", [(T1, 30), (T2, 50)])
    apim.add_metric(SERVICE, "MemoryPercent_Gateway", [(T1, 40)])

    body = client.get("/api/apim/metrics").json()

    series = {s["metric"]: s for s in body["series"]}
    assert set(series) == {"cpu", "memory"}
    assert (series["cpu"]["average"], series["cpu"]["peak"]) == (40, 50)
    assert series["memory"]["latest"] == 40


def test_gateway_dashboard_without_metrics_access(client: TestClient, apim: FakeApim) -> None:
    apim.callers["token"] = "app"
    apim.allow(
        "app", "/subscriptions/sub", "Microsoft.ApiManagement/*", "Microsoft.CognitiveServices/*"
    )

    body = client.get("/api/apim/metrics", params={"refresh": "true"}).json()

    assert body["traffic"] is None
    assert "Microsoft.Insights/metrics/read" in body["traffic_error"]
    assert body["series"] == []
    assert body["problems"] == [{"resource": SERVICE.rpartition("/")[2], "error": "forbidden"}]


def test_gateway_dashboard_is_cached_per_range(client: TestClient, apim: FakeApim) -> None:
    client.get("/api/apim/metrics")
    first = len(_metrics_requests(apim))
    client.get("/api/apim/metrics")
    assert len(_metrics_requests(apim)) == first
    client.get("/api/apim/metrics", params={"refresh": "true"})
    assert len(_metrics_requests(apim)) == first * 2
    assert client.get("/api/apim/metrics", params={"range": "1y"}).status_code == 422


def test_a_metric_the_tier_lacks_is_left_out(client: TestClient, apim: FakeApim) -> None:
    apim.metrics_unsupported.add("Capacity")
    apim.add_metric(SERVICE, "CpuPercent_Gateway", [(T1, 30)])

    body = client.get("/api/apim/metrics").json()

    assert [s["metric"] for s in body["series"]] == ["cpu"]
    assert body["problems"] == []
