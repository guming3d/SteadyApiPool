"""The load test through an API bound to a pool: where it goes, how it authenticates, and its
routes. The engine's numbers are covered in tests/litellm/test_benchmark.py."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pool_manager.apim.api_test import ApimTester
from pool_manager.app import app, get_apim_service, get_apim_tester, get_benchmarks
from pool_manager.benchmark import BenchmarkRunner
from pool_manager.config import Settings
from pool_manager.errors import ServiceError
from tests.apim.fake_apim import FakeApim
from tests.apim.test_api_test import API, _pool_api
from tests.apim.test_app import WRITE, _client, _settings

KEY = "primary-s-api"
REGIONS = ("East US 2", "Sweden Central")


class _Gateway:
    """The APIM gateway: 401 for a wrong key, else Azure OpenAI's answer from one region or the
    other, streamed when asked."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Ocp-Apim-Subscription-Key") != KEY:
            message = "Access denied due to missing subscription key."
            return httpx.Response(401, json={"statusCode": 401, "message": message})
        region = {"x-ms-region": REGIONS[len(self.requests) % 2]}
        usage = {"prompt_tokens": 60, "completion_tokens": 2}
        if json.loads(request.content).get("stream"):
            chunks = [
                {"choices": [{"delta": {"content": "Hi"}}]},
                {"choices": [], "usage": usage},
            ]
            text = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            headers = {**region, "Content-Type": "text/event-stream"}
            return httpx.Response(200, text=text + "data: [DONE]\n\n", headers=headers)
        return httpx.Response(200, json={"choices": [], "usage": usage}, headers=region)


def _wire(apim: FakeApim, settings: Settings, gateway: _Gateway) -> TestClient:
    client = _client(apim, settings)
    service = app.dependency_overrides[get_apim_service]()
    transport = httpx.MockTransport(gateway.handle)
    tester = ApimTester(settings, service, transport)
    runner = BenchmarkRunner(transport)
    app.dependency_overrides[get_apim_tester] = lambda: tester
    app.dependency_overrides[get_benchmarks] = lambda: runner
    return client


@pytest.fixture
def apim() -> FakeApim:
    return FakeApim()


@pytest.fixture
def gateway() -> _Gateway:
    return _Gateway()


@pytest.fixture
def client(apim: FakeApim, gateway: _Gateway) -> Iterator[TestClient]:
    client = _wire(apim, _settings(), gateway)
    _pool_api(client, apim)
    yield client
    app.dependency_overrides.clear()


def _start(client: TestClient, **body: Any) -> httpx.Response:
    payload = {"spec": {"users": 2, "max_requests": 6}, "subscription_key": KEY, **body}
    return client.post(f"/api/apim/apis/{API}/benchmarks", json=payload, headers=WRITE)


def _wait(client: TestClient, run_id: str) -> dict[str, Any]:
    for _ in range(500):
        run: dict[str, Any] = client.get(f"/api/apim/apis/{API}/benchmarks/{run_id}").json()
        if run["state"] != "running":
            return run
        time.sleep(0.01)
    raise AssertionError("the load test did not finish")


def test_a_load_test_through_the_api(client: TestClient, gateway: _Gateway) -> None:
    started = _start(client, spec={"users": 2, "max_requests": 6, "model": "gpt-4o"})

    assert started.status_code == 200, started.text
    assert KEY not in started.text
    run = _wait(client, started.json()["id"])
    assert (run["state"], run["pool_id"], run["api_id"], run["model"]) == (
        "done",
        "pool-gpt-4o",
        API,
        "gpt-4o",
    )
    summary = run["summary"]
    assert summary["requests"] == 6 and summary["successes"] == 6
    assert summary["ttft_p50_ms"] is not None and summary["completion_tokens"] == 12
    # APIM names no member; the region Azure OpenAI answered from tells them apart.
    assert sorted(member["member"] for member in summary["members"]) == sorted(REGIONS)
    assert summary["failovers"] == 0
    assert KEY not in json.dumps(run)
    request = gateway.requests[-1]
    assert str(request.url).startswith("https://apim.azure-api.net/gpt-4o/chat/completions?")
    assert request.url.params["api-version"] == "2025-04-01-preview"
    body = json.loads(request.content)
    # Newer Azure OpenAI models refuse max_tokens, and APIM forwards the body as it is.
    assert body["max_completion_tokens"] == 128 and "max_tokens" not in body
    assert body["model"] == "gpt-4o" and body["stream"] is True


def test_the_api_version_and_no_streaming(client: TestClient, gateway: _Gateway) -> None:
    started = _start(
        client, spec={"max_requests": 2, "stream": False}, api_version="2024-10-21"
    ).json()

    run = _wait(client, started["id"])

    assert run["summary"]["successes"] == 2 and run["summary"]["ttft_p50_ms"] is None
    assert gateway.requests[-1].url.params["api-version"] == "2024-10-21"
    assert "stream_options" not in json.loads(gateway.requests[-1].content)


def test_a_wrong_key_shows_as_401s(client: TestClient) -> None:
    run = _wait(client, _start(client, subscription_key="wrong").json()["id"])

    assert run["summary"]["errors"] == {"401": 6}


def test_only_apis_bound_to_a_pool(client: TestClient, apim: FakeApim) -> None:
    apim.add_api("plain", path="plain")

    response = client.post(
        "/api/apim/apis/plain/benchmarks", json={"spec": {}, "subscription_key": KEY}, headers=WRITE
    )

    assert response.status_code == 409
    assert "not bound to a pool" in response.json()["detail"]


def test_runs_are_per_api(client: TestClient, apim: FakeApim) -> None:
    run = _start(client, spec={"max_requests": 1}).json()

    assert client.get(f"/api/apim/apis/other/benchmarks/{run['id']}").status_code == 404
    # An API's run is not found as a pool's (LiteLLM's routes), even by the same name.
    runner: BenchmarkRunner = app.dependency_overrides[get_benchmarks]()
    with pytest.raises(ServiceError):
        runner.get(API, run["id"])
    assert runner.get(None, run["id"], api_id=API.upper()).id == run["id"]
    stopped = client.post(f"/api/apim/apis/{API}/benchmarks/{run['id']}/stop", headers=WRITE)
    assert stopped.status_code == 200


def test_only_owners_start_load_tests(apim: FakeApim, gateway: _Gateway) -> None:
    client = _wire(apim, _settings(auth_mode="easy_auth", owners="owner@contoso.com"), gateway)
    try:
        reader = {"X-MS-CLIENT-PRINCIPAL-NAME": "reader@contoso.com"}
        owner = {"X-MS-CLIENT-PRINCIPAL-NAME": "owner@contoso.com"}
        body = {"spec": {}, "subscription_key": KEY}
        path = f"/api/apim/apis/{API}/benchmarks"
        as_reader = client.post(path, json=body, headers={**reader, **WRITE})
        without_csrf = client.post(path, json=body, headers=owner)
        reader_poll = client.get(f"{path}/x", headers=reader)
    finally:
        app.dependency_overrides.clear()

    assert as_reader.status_code == 403
    assert without_csrf.status_code == 403
    assert reader_poll.status_code == 403
    assert not gateway.requests
