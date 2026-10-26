"""Trying a pool API from the UI, and the subscription keys it needs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pool_manager.api_test import ApiTester
from pool_manager.app import app, get_service, get_tester
from pool_manager.config import Settings
from tests.fake_apim import SERVICE, FakeApim
from tests.test_app import WRITE, _client, _import, _pool_body, _settings

API = "pool-gpt-4o"


class _Gateway:
    """The APIM gateway: answers like APIM for a missing or wrong key, else like the model."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.body: bytes = json.dumps({"model": "gpt-4o", "choices": []}).encode()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Ocp-Apim-Subscription-Key") != "primary-s-api":
            message = "Access denied due to missing subscription key."
            return httpx.Response(401, json={"statusCode": 401, "message": message})
        return httpx.Response(
            200,
            content=self.body,
            headers={
                "Content-Type": "application/json",
                "x-ms-region": "East US 2",
                "apim-request-id": "req-1",
                "x-ratelimit-remaining-tokens": "999",
                "Set-Cookie": "affinity=1",
                "Server": "Kestrel",
            },
        )


def _wire(apim: FakeApim, settings: Settings, gateway: _Gateway) -> TestClient:
    client = _client(apim, settings)
    service = app.dependency_overrides[get_service]()
    tester = ApiTester(settings, service, httpx.MockTransport(gateway.handle))
    app.dependency_overrides[get_tester] = lambda: tester
    return client


def _pool_api(client: TestClient, apim: FakeApim) -> None:
    east = apim.add_account("aoai-east", deployments=[("gpt-4o", "gpt-4o", "OpenAI")])
    west = apim.add_account("aoai-west", deployments=[("gpt-4o-2", "gpt-4o", "OpenAI")])
    ids = _import(client, [(east, "gpt-4o"), (west, "gpt-4o-2")])
    body = {**_pool_body(ids), "title": "gpt-4o"}
    assert client.put(f"/api/pools/{API}", json=body, headers=WRITE).status_code == 200
    created = client.post(f"/api/pools/{API}/api", json={"path": "gpt-4o"}, headers=WRITE)
    assert created.status_code == 200, created.text


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


def _test(client: TestClient, **body: Any) -> httpx.Response:
    payload = {"body": {"model": "gpt-4o", "messages": [{"role": "user", "content": "Hi"}]}}
    return client.post(f"/api/apis/{API}/test", json={**payload, **body}, headers=WRITE)


# -- keys -------------------------------------------------------------------------------


def test_lists_only_keys_that_open_the_api(client: TestClient, apim: FakeApim) -> None:
    apim.add_subscription("s-all", "/apis")
    apim.add_subscription("s-prod", "/products/Starter")
    apim.add_subscription("s-api", f"/apis/{API}")
    apim.add_subscription("s-old", f"/apis/{API}", state="suspended")
    apim.add_subscription("s-other", "/apis/other")
    apim.add_subscription("s-elsewhere", "/products/unlimited")
    apim.add_subscription("master", "/")
    apim.api_products[API] = ["Starter"]

    keys = client.get(f"/api/apis/{API}/keys").json()

    assert [(key["id"], key["scope"], key["product"], key["state"]) for key in keys] == [
        ("s-api", "api", None, "active"),
        ("s-prod", "product", "Starter", "active"),
        ("s-all", "all_apis", None, "active"),
        ("s-old", "api", None, "suspended"),
    ]


def test_creates_one_test_key_per_api(client: TestClient, apim: FakeApim) -> None:
    first = client.post(f"/api/apis/{API}/keys", headers=WRITE)
    again = client.post(f"/api/apis/{API}/keys", headers=WRITE)

    assert first.status_code == 200, first.text
    assert first.json() == again.json()
    assert (first.json()["id"], first.json()["scope"], first.json()["managed"]) == (
        f"pool-test-{API}",
        "api",
        True,
    )
    puts = [r for r in apim.requests if r.method == "PUT" and "/apim/subscriptions/" in r.url.path]
    assert [r.headers["If-None-Match"] for r in puts] == ["*", "*"]
    assert json.loads(puts[0].content)["properties"]["scope"] == f"/apis/{API}"
    assert client.get(f"/api/apis/{API}/keys").json()[0]["managed"] is True


def test_reads_a_key_only_for_subscriptions_that_open_the_api(
    client: TestClient, apim: FakeApim
) -> None:
    apim.add_subscription("s-api", f"/apis/{API}")
    apim.add_subscription("s-other", "/apis/other")

    assert client.post(f"/api/apis/{API}/keys/s-api/secret").status_code == 403  # No CSRF header.
    secret = client.post(f"/api/apis/{API}/keys/s-api/secret", headers=WRITE)
    other = client.post(f"/api/apis/{API}/keys/s-other/secret", headers=WRITE)
    missing = client.post(f"/api/apis/{API}/keys/nope/secret", headers=WRITE)

    assert secret.json() == {"primary_key": "primary-s-api"}
    assert other.status_code == 403
    assert missing.status_code == 404


def test_keys_need_the_users_own_rights(client: TestClient, apim: FakeApim) -> None:
    apim.callers["token"] = "dev"
    apim.allow("dev", SERVICE, "Microsoft.ApiManagement/service/*/read")

    listed = client.get(f"/api/apis/{API}/keys")
    created = client.post(f"/api/apis/{API}/keys", headers=WRITE)

    assert listed.status_code == 200
    assert created.status_code == 403
    assert "listSecrets" in created.json()["detail"]


def test_deployed_keys_need_a_delegated_token(apim: FakeApim, gateway: _Gateway) -> None:
    client = _wire(apim, _settings(auth_mode="easy_auth", owners="dev@contoso.com"), gateway)
    client.headers.update(
        {"X-MS-CLIENT-PRINCIPAL-NAME": "dev@contoso.com", "X-MS-CLIENT-PRINCIPAL-ID": "oid"}
    )
    try:
        _pool_api(client, apim)
        response = client.get(f"/api/apis/{API}/keys")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


# -- test call ---------------------------------------------------------------------------


def test_calls_the_api_through_the_gateway(client: TestClient, gateway: _Gateway) -> None:
    response = _test(client, subscription_key="primary-s-api")

    assert response.status_code == 200, response.text
    result = response.json()
    sent = gateway.requests[-1]
    assert str(sent.url) == (
        "https://apim.azure-api.net/gpt-4o/chat/completions?api-version=2025-04-01-preview"
    )
    assert sent.method == "POST"
    assert json.loads(sent.content)["model"] == "gpt-4o"
    assert result["status"] == 200
    assert result["url"] == str(sent.url)
    assert json.loads(result["body"])["model"] == "gpt-4o"
    # Only headers that explain the result; never cookies.
    assert result["headers"] == {
        "content-type": "application/json",
        "x-ms-region": "East US 2",
        "apim-request-id": "req-1",
        "x-ratelimit-remaining-tokens": "999",
    }
    assert result["truncated"] is False


def test_gateway_errors_are_results_not_failures(client: TestClient) -> None:
    result = _test(client).json()

    assert result["status"] == 401
    assert "missing subscription key" in result["body"]


def test_long_bodies_are_truncated(client: TestClient, gateway: _Gateway) -> None:
    gateway.body = b"x" * (70 * 1024)

    result = _test(client, subscription_key="primary-s-api").json()

    assert result["truncated"] is True
    assert len(result["body"]) == 64 * 1024


@pytest.mark.parametrize("path", ["../admin", "chat/../../x", "https://evil.example/x", "a?b=1"])
def test_paths_stay_below_the_api(client: TestClient, path: str) -> None:
    assert _test(client, path=path).status_code == 422


def test_only_pool_apis_are_tested(client: TestClient, apim: FakeApim) -> None:
    apim.add_api("plain", "plain")

    response = client.post("/api/apis/plain/test", json={"body": {}}, headers=WRITE)

    assert response.status_code == 409
    assert client.post(f"/api/apis/{API}/test", json={"body": {}}).status_code == 403
