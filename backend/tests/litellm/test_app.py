"""The real app through TestClient, with ARM and the gateway's LiteLLM proxy in memory."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pool_manager.access import SignInCredential
from pool_manager.app import (
    app,
    get_access,
    get_benchmarks,
    get_discovery,
    get_litellm_cost,
    get_litellm_monitor,
    get_litellm_service,
    get_litellm_targets,
    get_litellm_tester,
    get_targets,
)
from pool_manager.arm import ArmClient
from pool_manager.benchmark import BenchmarkRunner
from pool_manager.config import Settings, get_settings
from pool_manager.discovery import DeploymentDiscovery
from pool_manager.litellm.access import LiteLLMAccessChecker
from pool_manager.litellm.api_test import LiteLLMTester
from pool_manager.litellm.cleanup import GatewayCleanup
from pool_manager.litellm.cost import LiteLLMCost, PlanPrices
from pool_manager.litellm.gateway import GatewayConnection
from pool_manager.litellm.metrics import LiteLLMMonitor
from pool_manager.litellm.service import LiteLLMService
from pool_manager.litellm.target import LiteLLMTargets
from pool_manager.secret_store import SecretStore
from pool_manager.target import TargetChooser
from tests.fake_azure import FakeAzure, FakeCredential
from tests.litellm.fake_litellm import URL

WRITE = {"X-Pool-Manager": "1"}
# Traffic the sizer turns into the smallest gateway (one replica, Burstable, no Redis) and into
# a production one (2-6 replicas, General Purpose, Redis; on AKS three nodes across zones). The
# production calls aren't streamed: streaming costs the gateway far more CPU (sizing.py).
TRIAL: dict[str, Any] = {
    "peak_rpm": 60,
    "input_tokens": 1000,
    "output_tokens": 300,
    "timeout_seconds": 120,
    "zone_redundant": False,
}
PRODUCTION: dict[str, Any] = {
    "peak_rpm": 6000,
    "input_tokens": 2000,
    "output_tokens": 500,
    "timeout_seconds": 120,
    "streaming": False,
}


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "subscription_id": "sub",
        "resource_group": "rg",
        "gateway_name": "gw",
        "auth_mode": "disabled",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def azure() -> FakeAzure:
    return FakeAzure()


def _client(
    azure: FakeAzure,
    settings: Settings,
    sign_in: SignInCredential | None = None,
    credential: Any = None,
    state_file: Path | None = None,
    secrets: SecretStore | None = None,
    benchmarks: BenchmarkRunner | None = None,
) -> TestClient:
    """The app wired to the fakes, the way deps.py wires it to Azure."""
    secrets = secrets or SecretStore(Path(tempfile.mkdtemp()) / "keys.bin", persistent=False)
    arm = ArmClient(
        settings, credential or FakeCredential(), azure.transport(), sleep=lambda _: None
    )
    gateway = GatewayConnection(
        settings,
        arm,
        stored_key=secrets.get,
        transport=azure.litellm.transport(),
        sleep=lambda _: None,
    )
    service = LiteLLMService(settings, arm, gateway)
    discovery = DeploymentDiscovery(settings, arm, service)
    access = LiteLLMAccessChecker(
        settings, arm, service, lambda: [view.account_id for view in discovery.list()], sign_in
    )
    cost = LiteLLMCost(settings, service, transport=azure.transport())
    monitor = LiteLLMMonitor(settings, arm, service, transport=azure.transport())
    tester = LiteLLMTester(settings, service, transport=azure.litellm.transport())
    # Sign-in routes reach the target chooser too; never the developer's real state file.
    chooser = TargetChooser(
        settings,
        sign_in or credential or FakeCredential(),
        transport=azure.transport(),
        sleep=lambda _: None,
        state_file=state_file or Path(tempfile.mkdtemp()) / "state.json",
    )
    gateways = LiteLLMTargets(
        chooser,
        gateway_transport=azure.litellm.transport(),
        secrets=secrets,
        # Cleanups after failed deployments run inline, at once.
        cleanup=GatewayCleanup(run=lambda job: job(), sleep=lambda _: None),
        prices=PlanPrices(settings, transport=azure.transport()),
    )
    overrides = {
        get_settings: settings,
        get_litellm_service: service,
        get_discovery: discovery,
        get_access: access,
        get_litellm_cost: cost,
        get_litellm_monitor: monitor,
        get_litellm_tester: tester,
        get_benchmarks: benchmarks or BenchmarkRunner(azure.litellm.transport()),
        get_targets: chooser,
        get_litellm_targets: gateways,
    }
    for factory, value in overrides.items():
        app.dependency_overrides[factory] = _constant(value)
    return TestClient(app)


def _constant(value: Any) -> Any:
    # No parameters: FastAPI would read a defaulted one as a query parameter.
    return lambda: value


@pytest.fixture
def client(azure: FakeAzure) -> Iterator[TestClient]:
    yield _client(azure, _settings())
    app.dependency_overrides.clear()


def seed(azure: FakeAzure, model: str = "gpt-6") -> list[str]:
    """Two Foundry accounts with a gpt6 deployment each, and the gateway allowed on both."""
    accounts = [
        azure.add_account("aoai-east", deployments=[("gpt6", model, "OpenAI")]),
        azure.add_account("aoai-west", location="westus3", deployments=[("gpt6", model, "OpenAI")]),
    ]
    for account in accounts:
        azure.grant_openai_user(account)
    return accounts


def import_members(client: TestClient, accounts: list[str]) -> list[str]:
    response = client.post(
        "/api/deployments/import",
        json={"deployments": [{"account_id": a, "deployment": "gpt6"} for a in accounts]},
        headers=WRITE,
    )
    assert response.status_code == 200, response.text
    ids: list[str] = response.json()["backend_ids"]
    return ids


def pool_body(members: list[str], etag: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "title": "GPT-6",
        "members": [
            {"backend_id": member, "priority": index, "weight": 1}
            for index, member in enumerate(members)
        ],
        "etag": etag,
        **extra,
    }


def create_pool(client: TestClient, azure: FakeAzure, pool_id: str = "gpt6") -> list[str]:
    members = import_members(client, seed(azure))
    response = client.put(f"/api/litellm/pools/{pool_id}", json=pool_body(members), headers=WRITE)
    assert response.status_code == 200, response.text
    return members


# -- the whole flow over HTTP ---------------------------------------------------------------


def test_deployments_to_a_pool_clients_can_call(client: TestClient, azure: FakeAzure) -> None:
    accounts = seed(azure)
    deployments = client.get("/api/deployments").json()
    assert {d["account_name"] for d in deployments} == {"aoai-east", "aoai-west"}
    assert all(d["backend_id"] is None for d in deployments)

    members = import_members(client, accounts)
    check = client.post("/api/litellm/pools/model-check", json={"members": members}).json()
    assert check["ok"] and check["model_names"] == ["gpt-6"]
    preview = client.post("/api/litellm/pools/gpt6/preview", json=pool_body(members)).json()
    assert preview["current"] is None and "model: azure/gpt6" in preview["proposed"]

    pool = client.put("/api/litellm/pools/gpt6", json=pool_body(members), headers=WRITE).json()
    assert [m["backend_id"] for m in pool["members"]] == members
    assert len(azure.litellm.pool("gpt6")) == 2
    assert {d["backend_id"] for d in client.get("/api/deployments").json()} == set(members)

    endpoint = client.post(
        "/api/litellm/pools/gpt6/aliases", json={"name": "gpt-4o"}, headers=WRITE
    ).json()
    assert endpoint == {
        "pool_id": "gpt6",
        "base_url": f"{URL}/v1",
        "model_names": ["gpt6", "gpt-4o"],
        "aliases": [{"name": "gpt-4o", "pool_id": "gpt6", "managed": True}],
    }
    key = client.post("/api/litellm/pools/gpt6/keys", headers=WRITE).json()
    assert key["view"]["alias"] == "pool-test-gpt6"
    keys = client.get("/api/litellm/pools/gpt6/keys").json()
    assert [k["id"] for k in keys] == [key["view"]["id"]]
    assert "key" not in keys[0]

    result = client.post(
        "/api/litellm/pools/gpt6/test",
        json={"key": key["key"], "body": {"model": "gpt-4o", "messages": []}},
        headers=WRITE,
    ).json()
    assert result["status"] == 200
    assert result["headers"]["x-litellm-model-id"] == f"gpt6--{members[0]}"

    config = client.get("/api/litellm/pools/gpt6/config").json()["yaml"]
    assert "model_group_alias:" in config and "gpt-4o: gpt6" in config

    plan = client.get("/api/litellm/pools/gpt6/removal").json()
    confirm = {
        "pools": plan["pools"],
        "delete_aliases": [a["name"] for a in plan["delete_aliases"]],
        "unbind_aliases": [a["name"] for a in plan["unbind_aliases"]],
        "delete_keys": [k["alias"] for k in plan["delete_keys"]],
        "etag": pool["etag"],
    }
    removed = client.post("/api/litellm/pools/gpt6/remove", json=confirm, headers=WRITE)
    assert removed.status_code == 200, removed.text
    assert azure.litellm.rows == [] and azure.litellm.keys == {}


def test_pool_routes_need_a_gateway(azure: FakeAzure) -> None:
    client = _client(azure, _settings(subscription_id="", resource_group="", gateway_name=""))
    try:
        response = client.get("/api/litellm/pools")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert "gateway" in response.json()["detail"]


def test_gateway_errors_become_json(client: TestClient, azure: FakeAzure) -> None:
    azure.litellm.master_key = "rotated"

    response = client.get("/api/litellm/pools")

    assert response.status_code == 502
    assert "master key" in response.json()["detail"]


def test_capabilities(client: TestClient) -> None:
    caps = client.get("/api/litellm/capabilities").json()

    assert caps["gateway_name"] == "gw"
    assert caps["gateway_kind"] == "aca"
    assert caps["gateway_url"] == URL
    assert caps["litellm_version"] == "1.106.4"
    assert caps["app_insights_id"].endswith("/components/appi-gw")


def test_cleanup_needs_the_gateway_name(client: TestClient, azure: FakeAzure) -> None:
    create_pool(client, azure)
    plan = client.get("/api/litellm/cleanup").json()
    confirm = {
        "pools": plan["pools"],
        "delete_aliases": [],
        "unbind_aliases": [],
        "delete_keys": [],
    }

    wrong = client.post(
        "/api/litellm/cleanup", json={**confirm, "confirm_name": "nope"}, headers=WRITE
    )
    right = client.post(
        "/api/litellm/cleanup", json={**confirm, "confirm_name": "gw"}, headers=WRITE
    )

    assert wrong.status_code == 400
    assert right.status_code == 200
    assert azure.litellm.rows == []


# -- who may change things -----------------------------------------------------------------


def test_writes_require_the_csrf_header(client: TestClient) -> None:
    response = client.put("/api/litellm/backends/pool-a", json={"url": "https://a"})
    assert response.status_code == 403


def test_easy_auth_owners_and_readers(azure: FakeAzure) -> None:
    client = _client(azure, _settings(auth_mode="easy_auth", owners="Owner@Contoso.com"))
    try:
        assert client.get("/api/litellm/backends").status_code == 401
        reader = {"X-MS-CLIENT-PRINCIPAL-NAME": "reader@contoso.com"}
        owner = {"X-MS-CLIENT-PRINCIPAL-NAME": "owner@contoso.com"}
        assert client.get("/api/me", headers=reader).json()["can_write"] is False
        assert client.get("/api/litellm/backends", headers=reader).status_code == 200
        body = {
            "url": "https://aoai-east.openai.azure.com/openai/deployments/gpt6",
            "auth": {"type": "managed_identity"},
        }
        refused = client.put("/api/litellm/backends/pool-a", json=body, headers={**reader, **WRITE})
        assert refused.status_code == 403
        # Keys come from the master key, so only owners may list them, even without a write.
        assert client.get("/api/litellm/pools/x/keys", headers=reader).status_code == 403
        assert client.get("/api/litellm/pools/x/keys", headers=owner).status_code != 403
        allowed = client.put("/api/litellm/backends/pool-a", json=body, headers={**owner, **WRITE})
        assert allowed.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_disabled_auth_is_refused_on_app_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_SITE_NAME", "pool-manager")
    with pytest.raises(RuntimeError):
        _settings().validate_runtime()


def test_the_public_app_takes_no_litellm_gateway() -> None:
    # Each user chooses their own APIM service there; nothing is configured for everyone.
    settings = Settings(_env_file=None, auth_mode="entra", entra_client_id="x", gateway_name="gw")  # type: ignore[call-arg]
    with pytest.raises(RuntimeError, match="POOL_MANAGER_GATEWAY_NAME"):
        settings.validate_runtime()


def test_a_deployed_external_gateway_needs_its_master_key() -> None:
    settings = _settings(
        auth_mode="easy_auth",
        resource_group="",
        gateway_url=URL,
    )
    with pytest.raises(RuntimeError, match="MASTER_KEY"):
        settings.validate_runtime()


# -- the ARM client ------------------------------------------------------------------------


def test_arm_retries_throttling_with_retry_after() -> None:
    calls: list[int] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"value": []})

    arm = ArmClient(_settings(), FakeCredential(), httpx.MockTransport(handler), sleep=slept.append)
    assert list(arm.list("/revisions", api_version="2025-01-01")) == []
    assert slept == [3.0]


def test_arm_refuses_foreign_next_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [], "nextLink": "https://evil.example/x"})

    arm = ArmClient(_settings(), FakeCredential(), httpx.MockTransport(handler))
    with pytest.raises(Exception, match="another host"):
        list(arm.list("/revisions", api_version="2025-01-01"))


def test_the_arm_client_is_scoped_to_the_container_app() -> None:
    arm = ArmClient(
        _settings(), FakeCredential(), httpx.MockTransport(lambda _: httpx.Response(200))
    )

    assert arm.resource_id == (
        "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/containerApps/gw"
    )
