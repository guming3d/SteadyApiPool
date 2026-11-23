"""AI Gateway: pools (aliases) and members (models), through the app's HTTP API against the
in-memory gateway (fake_aigateway.py)."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pool_manager.access import SignInCredential
from pool_manager.aigateway.access import AiGatewayAccessChecker
from pool_manager.aigateway.api_test import AiGatewayTester
from pool_manager.aigateway.cost import AiGatewayCost
from pool_manager.aigateway.metrics import AiGatewayMonitor
from pool_manager.aigateway.models import CircuitBreaker, FailureStatusCodeRange
from pool_manager.aigateway.resources import (
    MANAGED_MARKER,
    failure_condition,
    parse_condition,
    parse_failures,
    parse_iso_seconds,
)
from pool_manager.aigateway.service import AiGatewayService
from pool_manager.aigateway.target import AiGatewayTargets
from pool_manager.app import (
    app,
    get_access,
    get_aigateway_cost,
    get_aigateway_monitor,
    get_aigateway_service,
    get_aigateway_targets,
    get_aigateway_tester,
    get_benchmarks,
    get_discovery,
    get_targets,
)
from pool_manager.arm import ArmClient
from pool_manager.benchmark import BenchmarkRunner
from pool_manager.config import Settings, get_settings
from pool_manager.discovery import DeploymentDiscovery
from pool_manager.target import TargetChooser
from tests.aigateway.fake_aigateway import GATEWAY, FakeAiGateway, FakeCredential

WRITE = {"X-Pool-Manager": "1"}
WS = "/workspaces/default"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "subscription_id": "sub",
        "resource_group": "rg",
        "aigateway_name": "aigw",
        "auth_mode": "disabled",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _constant(value: Any) -> Any:
    return lambda: value


def _client(
    azure: FakeAiGateway,
    settings: Settings,
    sign_in: SignInCredential | None = None,
    credential: Any = None,
    state_file: Path | None = None,
) -> TestClient:
    """The app wired to the fakes, the way deps.py wires it to Azure."""
    arm = ArmClient(
        settings, credential or FakeCredential(), azure.transport(), sleep=lambda _: None
    )
    service = AiGatewayService(settings, arm)
    discovery = DeploymentDiscovery(settings, arm, service)
    access = AiGatewayAccessChecker(
        settings, arm, service, lambda: [view.account_id for view in discovery.list()], sign_in
    )
    chooser = TargetChooser(
        settings,
        sign_in or credential or FakeCredential(),
        transport=azure.transport(),
        sleep=lambda _: None,
        state_file=state_file or Path(tempfile.mkdtemp()) / "state.json",
    )
    overrides = {
        get_settings: settings,
        get_aigateway_service: service,
        get_discovery: discovery,
        get_access: access,
        get_aigateway_cost: AiGatewayCost(settings, service, transport=azure.transport()),
        get_aigateway_monitor: AiGatewayMonitor(
            settings, arm, service, transport=azure.transport()
        ),
        get_aigateway_tester: AiGatewayTester(settings, service, transport=azure.transport()),
        get_benchmarks: BenchmarkRunner(),
        get_targets: chooser,
        get_aigateway_targets: AiGatewayTargets(chooser),
    }
    for factory, value in overrides.items():
        app.dependency_overrides[factory] = _constant(value)
    return TestClient(app)


@pytest.fixture
def azure() -> FakeAiGateway:
    return FakeAiGateway()


@pytest.fixture
def client(azure: FakeAiGateway) -> Iterator[TestClient]:
    yield _client(azure, _settings())
    app.dependency_overrides.clear()


def seed(azure: FakeAiGateway, model: str = "gpt-6") -> list[str]:
    """Two Foundry accounts with a gpt6 deployment each."""
    return [
        azure.add_account("aoai-east", deployments=[("gpt6", model, "OpenAI")]),
        azure.add_account("aoai-west", location="westus3", deployments=[("gpt6", model, "OpenAI")]),
    ]


def import_members(client: TestClient, accounts: list[str]) -> list[str]:
    response = client.post(
        "/api/deployments/import",
        json={"deployments": [{"account_id": a, "deployment": "gpt6"} for a in accounts]},
        headers=WRITE,
    )
    assert response.status_code == 200, response.text
    ids: list[str] = response.json()["backend_ids"]
    return ids


def pool_body(members: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "title": "GPT-6",
        "members": [
            {"backend_id": member, "priority": index, "weight": 1}
            for index, member in enumerate(members)
        ],
        **extra,
    }


def create_pool(
    client: TestClient, pool_id: str, members: list[str], **extra: Any
) -> dict[str, Any]:
    response = client.put(
        f"/api/aigateway/pools/{pool_id}", json=pool_body(members, **extra), headers=WRITE
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _last(azure: FakeAiGateway, method: str, suffix: str) -> tuple[httpx.Request, Any]:
    request = next(
        r for r in reversed(azure.requests) if r.method == method and r.url.path.endswith(suffix)
    )
    return request, json.loads(request.content) if request.content else None


# -- the failure rule -----------------------------------------------------------------------


def test_the_default_failure_rule_reads_back() -> None:
    breaker = CircuitBreaker()
    when = failure_condition(breaker)
    assert when == (
        "@cel(response == null || response.status_code == 429 || response.status_code == 408 "
        "|| (response.status_code >= 500 && response.status_code <= 599))"
    )
    assert parse_condition(when) == (breaker.status_code_ranges, True)
    only = CircuitBreaker(
        status_code_ranges=[FailureStatusCodeRange(minimum=429, maximum=429)], no_response=False
    )
    assert failure_condition(only) == "@cel(response != null && (response.status_code == 429))"
    assert parse_failures([{"when": failure_condition(only), "excludeFor": "PT30S"}]) == (
        only.model_copy(update={"exclude_seconds": 30}),
        False,
    )


def test_failure_rules_someone_else_wrote_are_not_parsed() -> None:
    assert parse_condition("@cel(response.headers['x'] == 'y')") is None
    assert parse_failures([{"when": "@cel(response == null)"}, {"when": "@cel(true)"}]) == (
        None,
        True,
    )
    assert parse_failures(None) == (None, False)
    assert parse_iso_seconds("PT1M30S") == 90
    assert parse_iso_seconds("P1D") == 86_400
    assert parse_iso_seconds("soon") is None


# -- members ---------------------------------------------------------------------------------


def test_importing_deployments_registers_models_under_one_provider_per_account(
    client: TestClient, azure: FakeAiGateway
) -> None:
    east, west = seed(azure)
    members = import_members(client, [east, west])
    assert members == ["pool-aoai-east-gpt6", "pool-aoai-west-gpt6"]

    providers = azure.children_of(f"{WS}/modelProviders")
    assert [item["name"] for item in providers] == ["pool-aoai-east", "pool-aoai-west"]
    provider = providers[0]["properties"]
    assert provider["modelProviderType"] == "Foundry"
    assert provider["foundry"] == {
        "endpoint": {"url": "https://aoai-east.cognitiveservices.azure.com/"},
        "resourceIds": [east],
    }
    assert provider["authentication"] == {
        "managedIdentity": {"resource": "https://cognitiveservices.azure.com/"}
    }
    assert provider["description"].startswith(MANAGED_MARKER)
    model = azure.body(f"{WS}/modelProviders/pool-aoai-east/models/pool-aoai-east-gpt6")
    assert model["properties"]["deployment"] == {
        "resourceId": f"{east}/deployments/gpt6",
        "modelName": "gpt6",
        "modelVersion": "2026-09-03",
    }
    assert model["properties"]["supportedEndpoints"] == [
        "/openai/v1/chat/completions",
        "/openai/v1/responses",
    ]
    request, _ = _last(azure, "PUT", "/models/pool-aoai-east-gpt6")
    assert request.headers["If-None-Match"] == "*"

    views = client.get("/api/aigateway/backends").json()
    assert {view["id"]: view["url"] for view in views} == {
        "pool-aoai-east-gpt6": "https://aoai-east.openai.azure.com/openai/deployments/gpt6",
        "pool-aoai-west-gpt6": "https://aoai-west.openai.azure.com/openai/deployments/gpt6",
    }
    assert all(view["managed"] and view["auth_type"] == "managed_identity" for view in views)
    # Importing again finds the same members.
    assert import_members(client, [east]) == ["pool-aoai-east-gpt6"]
    assert len(azure.children_of(f"{WS}/modelProviders")) == 2


def test_a_custom_member_keeps_its_secret_in_the_gateway(
    client: TestClient, azure: FakeAiGateway
) -> None:
    body = {
        "title": "Contoso",
        "url": "https://api.contoso.com/v1",
        "model": "contoso-chat",
        "auth": {"type": "bearer", "secret": "s3cret"},
    }
    response = client.put("/api/aigateway/backends/contoso", json=body, headers=WRITE)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["kind"] == "custom" and view["auth_type"] == "bearer"
    provider_id = f"{GATEWAY}{WS}/modelProviders/contoso".casefold()
    assert azure.header_secrets[provider_id] == [
        {"name": "Authorization", "value": "Bearer s3cret"}
    ]
    assert "value" not in json.dumps(azure.children[provider_id])
    # Updating without the secret keeps it.
    body = {**body, "url": "https://api2.contoso.com/v1", "auth": {"type": "bearer"}}
    response = client.put(
        "/api/aigateway/backends/contoso", json={**body, "etag": view["etag"]}, headers=WRITE
    )
    assert response.status_code == 200, response.text
    assert response.json()["url"] == "https://api2.contoso.com/v1"
    assert azure.header_secrets[provider_id][0]["value"] == "Bearer s3cret"
    # Creating without a secret is refused; so is creating it twice.
    assert (
        client.put(
            "/api/aigateway/backends/other",
            json={**body, "auth": {"type": "api_key"}},
            headers=WRITE,
        ).status_code
        == 400
    )
    assert (
        client.put("/api/aigateway/backends/contoso", json=body, headers=WRITE).status_code == 409
    )


def test_models_this_app_did_not_create_are_read_only(
    client: TestClient, azure: FakeAiGateway
) -> None:
    east, _ = seed(azure)
    azure.add_provider(
        "theirs",
        {
            "modelProviderType": "Foundry",
            "foundry": {
                "resourceIds": [east],
                "endpoint": {"url": "https://aoai-east.cognitiveservices.azure.com/"},
            },
        },
    )
    azure.add_model(
        "theirs",
        "gpt-6.1-sol",
        {
            "displayName": "Sol",
            "deployment": {"resourceId": f"{east}/deployments/gpt6", "modelName": "gpt6"},
        },
    )
    views = client.get("/api/aigateway/backends").json()
    assert [(view["id"], view["managed"]) for view in views] == [("gpt-6-1-sol", False)]
    assert client.delete("/api/aigateway/backends/gpt-6-1-sol", headers=WRITE).status_code == 409


# -- pools ----------------------------------------------------------------------------------


def test_a_pool_is_an_alias_with_the_apps_failure_rule(
    client: TestClient, azure: FakeAiGateway
) -> None:
    members = import_members(client, seed(azure))
    pool = create_pool(client, "gpt6", members, description="Chat")

    request, body = _last(azure, "PUT", "/aliases/gpt6")
    assert request.headers["If-None-Match"] == "*"
    properties = body["properties"]
    east, west = (
        f"{GATEWAY}{WS}/modelProviders/pool-aoai-{region}/models/pool-aoai-{region}-gpt6"
        for region in ("east", "west")
    )
    assert properties == {
        "aliasName": "gpt6",
        "description": f"{MANAGED_MARKER}\nTitle: GPT-6\nChat",
        "backendModels": [
            {"resourceId": east, "priority": 0, "weight": 1},
            {"resourceId": west, "priority": 1, "weight": 1},
        ],
        "selectionOrder": ["priority"],
        "failures": [
            {
                "when": failure_condition(CircuitBreaker()),
                "retryAttempts": 0,
                "excludeFor": "PT60S",
            }
        ],
        "maxRetryAttempts": 2,
    }
    assert pool["title"] == "GPT-6" and pool["description"] == "Chat"
    assert pool["circuit_breaker"] == CircuitBreaker().model_dump()
    assert pool["retries"] == 2 and pool["managed"] and not pool["custom_failures"]
    assert [m["backend_id"] for m in pool["members"]] == members
    endpoint = client.get("/api/aigateway/pools/gpt6/endpoint").json()
    assert endpoint == {
        "pool_id": "gpt6",
        "base_url": "https://aigw.eastus2-01.ai.gateway.azure.com/default/models/openai/v1",
        "model_name": "gpt6",
    }


def test_saving_a_pool_is_etag_conditional_and_keeps_the_aliases_policies(
    client: TestClient, azure: FakeAiGateway
) -> None:
    members = import_members(client, seed(azure))
    create_pool(client, "gpt6", members)
    alias = azure.body(f"{WS}/aliases/gpt6")
    alias["properties"]["policies"] = [{"type": "tokenLimit", "count": 1000, "period": "minute"}]
    current = client.get("/api/aigateway/pools/gpt6").json()
    assert current["policies"] == ["tokenLimit"]

    stale = client.put(
        "/api/aigateway/pools/gpt6", json=pool_body(members, etag='"0"'), headers=WRITE
    )
    assert stale.status_code == 409
    breaker = {"exclude_seconds": 30, "same_member_retries": 1, "no_response": False}
    saved = client.put(
        "/api/aigateway/pools/gpt6",
        json=pool_body(
            list(reversed(members)), etag=current["etag"], circuit_breaker=breaker, retries=3
        ),
        headers=WRITE,
    )
    assert saved.status_code == 200, saved.text
    request, body = _last(azure, "PUT", "/aliases/gpt6")
    assert request.headers["If-Match"] == current["etag"]
    assert body["properties"]["policies"] == [
        {"type": "tokenLimit", "count": 1000, "period": "minute"}
    ]
    assert body["properties"]["maxRetryAttempts"] == 3
    assert body["properties"]["failures"][0]["excludeFor"] == "PT30S"
    assert saved.json()["circuit_breaker"]["same_member_retries"] == 1
    # A new pool can't take an existing pool's ID.
    assert (
        client.put("/api/aigateway/pools/gpt6", json=pool_body(members), headers=WRITE).status_code
        == 409
    )


def test_members_must_serve_the_same_model(client: TestClient, azure: FakeAiGateway) -> None:
    east = azure.add_account("aoai-east", deployments=[("gpt6", "gpt-6", "OpenAI")])
    other = azure.add_account("aoai-west", deployments=[("gpt6", "gpt-5", "OpenAI")])
    members = import_members(client, [east, other])
    response = client.put("/api/aigateway/pools/mixed", json=pool_body(members), headers=WRITE)
    assert response.status_code == 409
    assert "same Foundry model" in response.json()["detail"]
    check = client.post("/api/aigateway/pools/model-check", json={"members": members}).json()
    assert check["ok"] is False and check["model_names"] == ["gpt-6", "gpt-5"]


def test_weights_below_one_are_refused(client: TestClient, azure: FakeAiGateway) -> None:
    members = import_members(client, seed(azure))
    body = pool_body(members)
    body["members"][1]["weight"] = 0
    assert client.put("/api/aigateway/pools/gpt6", json=body, headers=WRITE).status_code == 422


def test_aliases_this_app_did_not_create_are_read_only(
    client: TestClient, azure: FakeAiGateway
) -> None:
    members = import_members(client, seed(azure))
    model = azure.children_of(f"{WS}/modelProviders/pool-aoai-east/models")[0]["id"]
    azure.add_alias(
        "chat",
        {
            "aliasName": "chat",
            "description": "Theirs",
            "backendModels": [{"resourceId": model}],
            "failures": [{"when": "@cel(response.status_code == 503)"}],
        },
    )
    pool = client.get("/api/aigateway/pools/chat").json()
    assert pool["managed"] is False and pool["custom_failures"] is True
    assert pool["description"] == "Theirs" and pool["members"][0]["priority"] == 0
    response = client.put(
        "/api/aigateway/pools/chat", json=pool_body(members, etag=pool["etag"]), headers=WRITE
    )
    assert response.status_code == 409
    # Its routing name is taken: a new pool with that ID would answer to it too.
    assert (
        client.put("/api/aigateway/pools/chat2", json=pool_body(members), headers=WRITE).status_code
        == 200
    )
    azure.body(f"{WS}/aliases/chat")["properties"]["aliasName"] = "gpt7"
    taken = client.put("/api/aigateway/pools/gpt7", json=pool_body(members), headers=WRITE)
    assert taken.status_code == 409 and "already answers" in taken.json()["detail"]


def test_dropping_a_member_deletes_its_model_and_then_its_provider(
    client: TestClient, azure: FakeAiGateway
) -> None:
    east, west = seed(azure)
    azure.add_account("aoai-third", deployments=[("gpt6", "gpt-6", "OpenAI")])
    third = azure.accounts[-1]["id"]
    members = import_members(client, [east, west, third])
    pool = create_pool(client, "gpt6", members)
    saved = client.put(
        "/api/aigateway/pools/gpt6", json=pool_body(members[:2], etag=pool["etag"]), headers=WRITE
    )
    assert saved.status_code == 200, saved.text
    names = [item["name"] for item in azure.children_of(f"{WS}/modelProviders")]
    assert names == ["pool-aoai-east", "pool-aoai-west"]


# -- removal ---------------------------------------------------------------------------------


def test_removing_a_pool_takes_its_unused_members(client: TestClient, azure: FakeAiGateway) -> None:
    members = import_members(client, seed(azure))
    create_pool(client, "gpt6", members)
    create_pool(client, "gpt6b", members[:1] + members[1:])
    plan = client.get("/api/aigateway/pools/gpt6/removal").json()
    assert plan == {
        "pools": ["gpt6"],
        "delete_members": [],
        "delete_keys": [],
        "kept_pools": [],
        "blocked": [],
    }
    pool = client.get("/api/aigateway/pools/gpt6").json()
    result = client.post(
        "/api/aigateway/pools/gpt6/remove",
        json={"pools": ["gpt6"], "etag": pool["etag"]},
        headers=WRITE,
    ).json()
    assert result == {"pools": ["gpt6"], "deleted_members": [], "deleted_keys": []}
    plan = client.get("/api/aigateway/pools/gpt6b/removal").json()
    assert plan["delete_members"] == members
    # Refused when the plan reviewed is not the plan now.
    assert (
        client.post(
            "/api/aigateway/pools/gpt6b/remove", json={"pools": []}, headers=WRITE
        ).status_code
        == 409
    )
    result = client.post(
        "/api/aigateway/pools/gpt6b/remove", json={"pools": ["gpt6b"]}, headers=WRITE
    ).json()
    assert result["deleted_members"] == members
    assert azure.children_of(f"{WS}/modelProviders") == []


def test_cleanup_takes_the_apps_pools_and_test_key_and_needs_the_name(
    client: TestClient, azure: FakeAiGateway
) -> None:
    members = import_members(client, seed(azure))
    create_pool(client, "gpt6", members)
    azure.add_alias("theirs", {"aliasName": "theirs", "backendModels": []})
    assert client.post("/api/aigateway/keys", headers=WRITE).status_code == 200
    plan = client.get("/api/aigateway/cleanup").json()
    assert plan["pools"] == ["gpt6"] and plan["kept_pools"] == ["theirs"]
    assert plan["delete_keys"] == ["pool-test"] and plan["delete_members"] == members
    confirm = {"pools": ["gpt6"], "delete_keys": ["pool-test"]}
    wrong = client.post(
        "/api/aigateway/cleanup", json={**confirm, "confirm_name": "x"}, headers=WRITE
    )
    assert wrong.status_code == 400
    result = client.post(
        "/api/aigateway/cleanup", json={**confirm, "confirm_name": "aigw"}, headers=WRITE
    ).json()
    assert result == {"pools": ["gpt6"], "deleted_members": members, "deleted_keys": ["pool-test"]}
    assert [item["name"] for item in azure.children_of(f"{WS}/aliases")] == ["theirs"]


# -- trying a pool ---------------------------------------------------------------------------


def test_the_test_key_and_the_test_call(client: TestClient, azure: FakeAiGateway) -> None:
    members = import_members(client, seed(azure))
    create_pool(client, "gpt6", members)
    azure._put(f"{GATEWAY}/apiKeys/master", {"properties": {"displayName": "Built-in"}})
    created = client.post("/api/aigateway/keys", headers=WRITE)
    assert created.status_code == 200, created.text
    assert created.json()["managed"] is True
    assert [key["id"] for key in client.get("/api/aigateway/keys").json()] == ["pool-test"]
    assert client.post("/api/aigateway/keys/master/secret", headers=WRITE).status_code == 403
    secret = client.post("/api/aigateway/keys/pool-test/secret", headers=WRITE).json()
    assert secret == {"primary_key": "primary-pool-test"}

    body = {"body": {"model": "something-else", "messages": []}, "key": "primary-pool-test"}
    assert (
        client.post("/api/aigateway/pools/gpt6/test", json=body, headers=WRITE).status_code == 400
    )
    body["body"].pop("model")
    response = client.post("/api/aigateway/pools/gpt6/test", json=body, headers=WRITE)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["url"] == (
        "https://aigw.eastus2-01.ai.gateway.azure.com/default/models/openai/v1/chat/completions"
    )
    assert result["headers"]["x-ms-served-model"] == "gpt-6-luna"
    assert "set-cookie" not in result["headers"]
    sent = azure.runtime_requests[-1]
    assert sent.headers["Authorization"] == "Bearer primary-pool-test"
    assert json.loads(sent.content)["model"] == "gpt6"
    other = {**body, "path": "images/generations"}
    assert (
        client.post("/api/aigateway/pools/gpt6/test", json=other, headers=WRITE).status_code == 400
    )


def test_a_load_test_targets_the_pools_alias(azure: FakeAiGateway) -> None:
    settings = _settings()
    arm = ArmClient(settings, FakeCredential(), azure.transport(), sleep=lambda _: None)
    service = AiGatewayService(settings, arm)
    client = _client(azure, settings)
    try:
        create_pool(client, "gpt6", import_members(client, seed(azure)))
    finally:
        app.dependency_overrides.clear()
    target = AiGatewayTester(settings, service).benchmark_target("gpt6", None, "k")
    assert target.url.endswith("/default/models/openai/v1/chat/completions")
    assert target.model == "gpt6" and target.member_header == "x-ms-served-model"
    assert target.headers["Authorization"] == "Bearer k"
    assert target.tokens_field == "max_completion_tokens"


# -- the dashboard and cost --------------------------------------------------------------------


def test_the_monitor_needs_a_telemetry_exporter(client: TestClient, azure: FakeAiGateway) -> None:
    members = import_members(client, seed(azure))
    create_pool(client, "gpt6", members)
    metrics = client.get("/api/aigateway/pools/gpt6/metrics").json()
    assert metrics["gateway"] is None and metrics["gateway_error"] == "no_app_insights"
    assert metrics["names"] == ["gpt6"]

    azure.add_exporter()
    east_model = f"{GATEWAY}{WS}/modelProviders/pool-aoai-east/models/pool-aoai-east-gpt6"
    azure.log_rows = {
        "order by time": [
            {
                "time": "2026-11-23T10:00:00Z",
                "success": 9,
                "throttled": 1,
                "client_errors": 0,
                "server_errors": 0,
                "failovers": 2,
                "duration_ms": 800.0,
                "backend_ms": 700.0,
            }
        ],
        "by backend": [{"backend": east_model, "attempts": 12, "failures": 3, "throttled": 3}],
    }
    metrics = client.get("/api/aigateway/pools/gpt6/metrics?refresh=true").json()
    gateway = metrics["gateway"]
    assert gateway["requests"] == 10 and gateway["failovers"] == 2
    assert gateway["availability"] == 1.0 and gateway["duration_ms"] == 800.0
    east = next(m for m in metrics["members"] if m["backend_id"] == members[0])
    assert (east["gateway_attempts"], east["gateway_failures"], east["gateway_throttled"]) == (
        12,
        3,
        3,
    )
    query = azure.log_queries[-1]
    assert query["resource"].endswith("/workspaces/aigw-logs")
    assert "OTelLogs" in query["query"] and "'gpt6'" in query["query"]
    load = client.get("/api/aigateway/metrics").json()
    assert load["traffic"]["requests"] == 10 and load["series"] == []


def test_cost_is_the_request_price_and_the_gateways_spend(
    client: TestClient, azure: FakeAiGateway
) -> None:
    # A table of its own: the default is shared with other tests.
    azure.retail_prices = {
        "eastus2": {
            "API Management": [
                {
                    "skuName": "AI Gateway",
                    "meterName": "10k AI Gateway Requests",
                    "retailPrice": 0.5,
                },
                {"skuName": "Basic v2", "meterName": "Basic v2 Unit", "retailPrice": 0.2},
            ]
        }
    }
    azure.costs[GATEWAY.casefold()] = (12.34, "USD")
    cost = client.get("/api/aigateway/cost").json()
    assert cost["list_price"] == {"per_10k_requests": 0.5, "currency": "USD"}
    assert cost["month_to_date"]["amount"] == 12.34
    assert cost["sku"] == "Standard" and cost["location"] == "eastus2"


# -- the product guard -------------------------------------------------------------------------


def test_other_products_routes_answer_409(client: TestClient) -> None:
    assert client.get("/api/apim/pools").status_code == 409
    assert client.get("/api/litellm/pools").status_code == 409
    assert client.get("/api/aigateway/capabilities").status_code == 200
