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
from pool_manager.apim.access import ApimAccessChecker
from pool_manager.apim.cost import ApimCost
from pool_manager.apim.policy import DEFAULT_POLICY
from pool_manager.apim.resources import MANAGED_MARKER, session_cookie_name
from pool_manager.apim.service import ApimService, policy_response_value
from pool_manager.apim.target import ApimTargets
from pool_manager.app import (
    app,
    get_access,
    get_apim_cost,
    get_apim_service,
    get_apim_targets,
    get_discovery,
    get_targets,
)
from pool_manager.arm import ArmClient
from pool_manager.config import GA_API_VERSION, Settings, get_settings
from pool_manager.discovery import DeploymentDiscovery, suggested_backend_id
from pool_manager.target import TargetChooser
from tests.apim.fake_apim import SERVICE, FakeApim, FakeCredential

WRITE = {"X-Pool-Manager": "1"}


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "subscription_id": "sub",
        "resource_group": "rg",
        "apim_name": "apim",
        "auth_mode": "disabled",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def apim() -> FakeApim:
    return FakeApim()


def _client(
    apim: FakeApim,
    settings: Settings,
    sign_in: SignInCredential | None = None,
    credential: Any = None,
) -> TestClient:
    arm = ArmClient(
        settings, credential or FakeCredential(), apim.transport(), sleep=lambda _: None
    )
    service = ApimService(settings, arm)
    app.dependency_overrides[get_settings] = lambda: settings
    discovery = DeploymentDiscovery(settings, arm, service)
    access = ApimAccessChecker(
        settings, arm, service, lambda: [view.account_id for view in discovery.list()], sign_in
    )
    app.dependency_overrides[get_apim_service] = lambda: service
    app.dependency_overrides[get_discovery] = lambda: discovery
    app.dependency_overrides[get_access] = lambda: access
    cost = ApimCost(settings, service, transport=apim.transport())
    app.dependency_overrides[get_apim_cost] = lambda: cost
    # Sign-in routes reach the target chooser too; never the developer's real state file.
    chooser = TargetChooser(
        settings,
        sign_in or credential or FakeCredential(),
        transport=apim.transport(),
        sleep=lambda _: None,
        state_file=Path(tempfile.mkdtemp()) / "state.json",
    )
    services = ApimTargets(chooser)
    app.dependency_overrides[get_targets] = lambda: chooser
    app.dependency_overrides[get_apim_targets] = lambda: services
    return TestClient(app)


@pytest.fixture
def client(apim: FakeApim) -> Iterator[TestClient]:
    yield _client(apim, _settings())
    app.dependency_overrides.clear()


def _member(client: TestClient, name: str, **auth: Any) -> dict[str, Any]:
    response = client.put(
        f"/api/apim/backends/{name}",
        json={"url": f"https://{name}.openai.azure.com/openai", "auth": auth or {"type": "none"}},
        headers=WRITE,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _last_body(apim: FakeApim, method: str, suffix: str) -> tuple[httpx.Request, Any]:
    request = next(
        r for r in reversed(apim.requests) if r.method == method and r.url.path.endswith(suffix)
    )
    return request, json.loads(request.content)


def test_member_backend_has_the_expected_shape(client: TestClient, apim: FakeApim) -> None:
    view = _member(
        client, "pool-east", type="api_key", header_name="api-key", secret={"value": "s3cret"}
    )

    _, named_value = _last_body(apim, "PUT", "/namedValues/pool-east-credential")
    assert named_value["properties"]["secret"] is True
    assert named_value["properties"]["value"] == "s3cret"
    _, backend = _last_body(apim, "PUT", "/backends/pool-east")
    properties = backend["properties"]
    assert properties["type"] == "Single"
    assert properties["credentials"] == {"header": {"api-key": ["{{pool-east-credential}}"]}}
    assert properties["tls"] == {"validateCertificateChain": True, "validateCertificateName": True}
    assert properties["circuitBreaker"] == {
        "rules": [
            {
                "name": "deployment-fault-isolation",
                "failureCondition": {
                    "count": 1,
                    "interval": "PT60S",
                    "statusCodeRanges": [
                        {"min": 429, "max": 429},
                        {"min": 408, "max": 408},
                        {"min": 500, "max": 599},
                    ],
                    "errorReasons": ["BackendConnectionFailure", "Timeout"],
                },
                "tripDuration": "PT60S",
                "acceptRetryAfter": True,
            }
        ]
    }
    assert view["managed"] is True
    assert view["auth_type"] == "api_key"
    assert view["named_value"] == "pool-east-credential"
    assert view["header_name"] == "api-key"
    assert view["circuit_breaker"]["failure_count"] == 1
    assert "s3cret" not in json.dumps(view)


def test_failover_pool_uses_one_based_priority_and_preview_version(
    client: TestClient, apim: FakeApim
) -> None:
    _member(client, "pool-east")
    _member(client, "pool-west", type="managed_identity")
    response = client.put(
        "/api/apim/pools/gpt-pool",
        json={
            "members": [
                {"backend_id": "pool-east", "priority": 0, "weight": 1},
                {"backend_id": "POOL-WEST", "priority": 1, "weight": 1},
            ],
            "session_affinity": True,
        },
        headers=WRITE,
    )
    assert response.status_code == 200, response.text

    request, body = _last_body(apim, "PUT", "/backends/gpt-pool")
    assert request.url.params["api-version"] == "2025-09-01-preview"
    assert request.headers["If-None-Match"] == "*"
    assert body["properties"]["type"] == "Pool"
    assert body["properties"]["description"] == MANAGED_MARKER
    assert body["properties"]["pool"] == {
        "services": [
            {"id": f"{SERVICE}/backends/pool-east", "priority": 1, "weight": 1},
            {"id": f"{SERVICE}/backends/pool-west", "priority": 2, "weight": 1},
        ],
        "sessionAffinity": {
            "sessionId": {"source": "cookie", "name": session_cookie_name("gpt-pool")}
        },
    }
    pool = response.json()
    assert pool["members"][1] == {"backend_id": "pool-west", "priority": 1, "weight": 1}
    assert pool["session_affinity"] is True
    assert pool["etag"]


def test_pool_updates_require_the_current_etag(client: TestClient) -> None:
    _member(client, "pool-a")
    _member(client, "pool-b")
    payload = {
        "members": [
            {"backend_id": "pool-a", "priority": 0, "weight": 1},
            {"backend_id": "pool-b", "priority": 0, "weight": 1},
        ]
    }
    created = client.put("/api/apim/pools/p1", json=payload, headers=WRITE).json()

    assert client.put("/api/apim/pools/p1", json=payload, headers=WRITE).status_code == 409
    weighted = {
        **payload,
        "members": [{**payload["members"][0], "weight": 3}, payload["members"][1]],
    }
    updated = client.put(
        "/api/apim/pools/p1", json={**weighted, "etag": created["etag"]}, headers=WRITE
    )
    assert updated.status_code == 200
    assert updated.json()["members"][0]["weight"] == 3
    stale = client.put(
        "/api/apim/pools/p1", json={**payload, "etag": created["etag"]}, headers=WRITE
    )
    assert stale.status_code == 409
    assert "Reload" in stale.json()["detail"]


def test_pool_validation(client: TestClient) -> None:
    _member(client, "pool-a")
    _member(client, "pool-b")
    zero_group = {
        "members": [
            {"backend_id": "pool-a", "priority": 0, "weight": 1},
            {"backend_id": "pool-b", "priority": 1, "weight": 0},
        ]
    }
    duplicate = {
        "members": [
            {"backend_id": "pool-a", "priority": 0, "weight": 1},
            {"backend_id": "POOL-A", "priority": 0, "weight": 1},
        ]
    }
    single = {"members": [{"backend_id": "pool-a", "priority": 0, "weight": 1}]}
    for payload in (zero_group, duplicate, single):
        assert client.put("/api/apim/pools/p", json=payload, headers=WRITE).status_code == 422


def test_pools_cannot_nest_or_reference_missing_backends(client: TestClient) -> None:
    _member(client, "pool-a")
    _member(client, "pool-b")
    members = [
        {"backend_id": "pool-a", "priority": 0, "weight": 1},
        {"backend_id": "pool-b", "priority": 0, "weight": 1},
    ]
    assert (
        client.put("/api/apim/pools/inner", json={"members": members}, headers=WRITE).status_code
        == 200
    )
    nested = [members[0], {"backend_id": "inner", "priority": 0, "weight": 1}]
    missing = [members[0], {"backend_id": "nope", "priority": 0, "weight": 1}]
    for candidate in (nested, missing):
        response = client.put("/api/apim/pools/outer", json={"members": candidate}, headers=WRITE)
        assert response.status_code == 400
        assert "Single backend" in response.json()["detail"]


def test_session_affinity_is_rejected_on_ga_api_version(apim: FakeApim) -> None:
    client = _client(apim, _settings(pool_api_version=GA_API_VERSION))
    try:
        assert client.get("/api/apim/capabilities").json()["session_affinity_supported"] is False
        response = client.put(
            "/api/apim/pools/p",
            json={
                "members": [
                    {"backend_id": "a", "priority": 0, "weight": 1},
                    {"backend_id": "b", "priority": 0, "weight": 1},
                ],
                "session_affinity": True,
            },
            headers=WRITE,
        )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_protected_resources_are_read_only(apim: FakeApim) -> None:
    client = _client(apim, _settings(protected_prefixes="other-, finops-"))
    try:
        apim.add_backend("other-route-member-abc", {"type": "Single", "url": "https://x"})
        apim.add_backend(
            "other-route-pool-abc",
            {
                "type": "Pool",
                "pool": {
                    "services": [
                        {
                            "id": f"{SERVICE}/backends/other-route-member-abc",
                            "priority": None,
                            "weight": None,
                        },
                    ]
                },
            },
        )
        pools = client.get("/api/apim/pools").json()
        assert pools[0]["protected"] is True
        assert pools[0]["members"] == [
            {"backend_id": "other-route-member-abc", "priority": 0, "weight": 1}
        ]
        assert (
            client.delete("/api/apim/pools/other-route-pool-abc", headers=WRITE).status_code == 403
        )
        assert (
            client.delete("/api/apim/backends/other-route-member-abc", headers=WRITE).status_code
            == 403
        )
        assert all(r.method == "GET" for r in apim.requests)
    finally:
        app.dependency_overrides.clear()


def test_backends_created_elsewhere_are_not_overwritten(client: TestClient, apim: FakeApim) -> None:
    apim.add_backend("hand-made", {"type": "Single", "url": "https://x", "description": "ops"})
    etag = client.get("/api/apim/backends/hand-made").json()["etag"]
    response = client.put(
        "/api/apim/backends/hand-made", json={"url": "https://y", "etag": etag}, headers=WRITE
    )
    assert response.status_code == 409
    assert client.delete("/api/apim/backends/hand-made", headers=WRITE).status_code == 409


def test_members_and_bound_pools_cannot_be_deleted(client: TestClient, apim: FakeApim) -> None:
    _member(client, "pool-a")
    _member(client, "pool-b")
    members = [
        {"backend_id": "pool-a", "priority": 0, "weight": 1},
        {"backend_id": "pool-b", "priority": 0, "weight": 1},
    ]
    client.put("/api/apim/pools/p1", json={"members": members}, headers=WRITE)
    apim.add_api("chat", "chat", DEFAULT_POLICY)
    assert (
        client.put("/api/apim/apis/chat/binding", json={"pool_id": "p1"}, headers=WRITE).status_code
        == 200
    )

    member = client.delete("/api/apim/backends/pool-a", headers=WRITE)
    assert member.status_code == 409
    assert "p1" in member.json()["detail"]
    pool = client.delete("/api/apim/pools/p1", headers=WRITE)
    assert pool.status_code == 409
    assert "Chat" in pool.json()["detail"]

    assert client.delete("/api/apim/apis/chat/binding", headers=WRITE).status_code == 204
    assert client.delete("/api/apim/pools/p1", headers=WRITE).status_code == 204
    # Its members went with it: this app created them and no other pool uses them.
    assert "pool-a" not in apim.backends
    assert "pool-b" not in apim.backends


def test_bind_and_unbind_round_trip_the_policy(client: TestClient, apim: FakeApim) -> None:
    _member(client, "pool-a")
    _member(client, "pool-b")
    client.put(
        "/api/apim/pools/p1",
        json={
            "members": [
                {"backend_id": "pool-a", "priority": 0, "weight": 1},
                {"backend_id": "pool-b", "priority": 1, "weight": 1},
            ]
        },
        headers=WRITE,
    )
    original = DEFAULT_POLICY.replace(
        "<outbound>",
        '<outbound>\n        <set-header name="x-a" exists-action="override">'
        "<value>1</value></set-header>",
    )
    apim.add_api("chat", "chat", original)
    apim.add_api("chat;rev=1", "chat", DEFAULT_POLICY)  # Real shape: no isCurrent field.

    bound = client.put(
        "/api/apim/apis/chat/binding",
        json={"pool_id": "P1", "managed_identity_resource": "https://cognitiveservices.azure.com"},
        headers=WRITE,
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["binding"] == {
        "pool_id": "p1",
        "retry_on_429": True,
        "max_attempts_per_request": 2,
        "retry_interval_seconds": 1,
        "first_fast_retry": True,
        "backend_timeout_seconds": 120,
        "managed_identity_resource": "https://cognitiveservices.azure.com",
    }
    policy = apim.policies["chat"]
    assert '<set-backend-service backend-id="p1" />' in policy
    assert (
        '<authentication-managed-identity resource="https://cognitiveservices.azure.com" />'
        in policy
    )
    assert 'count="1" interval="1" first-fast-retry="true"' in policy
    assert (
        '<forward-request timeout="120" buffer-request-body="true" buffer-response="false" />'
        in policy
    )
    assert policy.index("<base />") < policy.index("set-backend-service")
    assert [api["id"] for api in client.get("/api/apim/apis").json()] == ["chat"]

    assert client.delete("/api/apim/apis/chat/binding", headers=WRITE).status_code == 204
    assert apim.policies["chat"] == original


def _two_member_pool(client: TestClient) -> None:
    _member(client, "pool-a")
    _member(client, "pool-b")
    response = client.put(
        "/api/apim/pools/p1",
        json={
            "members": [
                {"backend_id": "pool-a", "priority": 0, "weight": 1},
                {"backend_id": "pool-b", "priority": 1, "weight": 1},
            ]
        },
        headers=WRITE,
    )
    assert response.status_code == 200, response.text


def test_binding_preview_writes_nothing(client: TestClient, apim: FakeApim) -> None:
    _two_member_pool(client)
    apim.add_api("chat", "chat", DEFAULT_POLICY)
    before = len(apim.requests)

    preview = client.post(
        "/api/apim/apis/chat/binding/preview",
        json={
            "pool_id": "p1",
            "max_attempts_per_request": 3,
            "retry_interval_seconds": 2,
            "first_fast_retry": False,
            "backend_timeout_seconds": 60,
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["current"] == DEFAULT_POLICY
    assert '<set-backend-service backend-id="p1" />' in body["proposed"]
    assert all(request.method == "GET" for request in apim.requests[before:])
    assert apim.policies["chat"] == DEFAULT_POLICY

    # The settings the preview showed are what a real bind writes and reads back.
    bound = client.put(
        "/api/apim/apis/chat/binding",
        json={
            "pool_id": "p1",
            "max_attempts_per_request": 3,
            "retry_interval_seconds": 2,
            "first_fast_retry": False,
            "backend_timeout_seconds": 60,
        },
        headers=WRITE,
    )
    assert apim.policies["chat"] == body["proposed"]
    binding = bound.json()["binding"]
    assert (
        binding["max_attempts_per_request"],
        binding["retry_interval_seconds"],
        binding["first_fast_retry"],
        binding["backend_timeout_seconds"],
    ) == (3, 2, False, 60)

    policy = client.get("/api/apim/apis/chat/policy").json()
    assert policy["xml"] == body["proposed"]
    assert policy["etag"]


def test_binding_preview_reports_conflicts_and_missing_policies(
    client: TestClient, apim: FakeApim
) -> None:
    _two_member_pool(client)
    foreign = DEFAULT_POLICY.replace(
        "<base />\n    </inbound>",
        '<base />\n<set-backend-service backend-id="other" />\n    </inbound>',
    )
    apim.add_api("foreign", "f", foreign)
    apim.add_api("bare", "bare")

    conflict = client.post("/api/apim/apis/foreign/binding/preview", json={"pool_id": "p1"})
    assert conflict.status_code == 409
    assert "set-backend-service" in conflict.json()["detail"]
    assert (
        client.post("/api/apim/apis/foreign/binding/preview", json={"pool_id": "nope"}).status_code
        == 404
    )

    assert client.get("/api/apim/apis/bare/policy").json() == {
        "api_id": "bare",
        "xml": None,
        "etag": None,
    }
    without_retry = client.post(
        "/api/apim/apis/bare/binding/preview", json={"pool_id": "p1", "retry_on_429": False}
    ).json()
    assert without_retry["current"] is None
    assert "<retry" not in without_retry["proposed"]


def test_binding_refuses_to_override_operator_policy(client: TestClient, apim: FakeApim) -> None:
    _member(client, "pool-a")
    _member(client, "pool-b")
    client.put(
        "/api/apim/pools/p1",
        json={
            "members": [
                {"backend_id": "pool-a", "priority": 0, "weight": 1},
                {"backend_id": "pool-b", "priority": 0, "weight": 1},
            ]
        },
        headers=WRITE,
    )
    foreign = DEFAULT_POLICY.replace(
        "<base />\n    </inbound>",
        '<base />\n<set-backend-service backend-id="other" />\n    </inbound>',
    )
    custom_backend = DEFAULT_POLICY.replace(
        "<backend>\n        <base />",
        '<backend>\n        <choose><when condition="true"><forward-request /></when></choose>',
    )
    apim.add_api("foreign", "f", foreign)
    apim.add_api("custom", "c", custom_backend)

    apis = {api["id"]: api for api in client.get("/api/apim/apis").json()}
    assert apis["foreign"]["foreign_backend_selection"] is True
    assert (
        client.put(
            "/api/apim/apis/foreign/binding", json={"pool_id": "p1"}, headers=WRITE
        ).status_code
        == 409
    )
    assert (
        client.put(
            "/api/apim/apis/custom/binding", json={"pool_id": "p1"}, headers=WRITE
        ).status_code
        == 409
    )
    without_retry = client.put(
        "/api/apim/apis/custom/binding",
        json={"pool_id": "p1", "retry_on_429": False},
        headers=WRITE,
    )
    assert without_retry.status_code == 200
    assert apim.policies["custom"].count("<choose>") == 1
    assert apim.policies["foreign"] == foreign


def test_api_without_policy_gets_the_default_template(client: TestClient, apim: FakeApim) -> None:
    _member(client, "pool-a")
    _member(client, "pool-b")
    client.put(
        "/api/apim/pools/p1",
        json={
            "members": [
                {"backend_id": "pool-a", "priority": 0, "weight": 1},
                {"backend_id": "pool-b", "priority": 0, "weight": 1},
            ]
        },
        headers=WRITE,
    )
    apim.add_api("bare", "bare")
    response = client.put("/api/apim/apis/bare/binding", json={"pool_id": "p1"}, headers=WRITE)
    assert response.status_code == 200
    assert "<outbound>" in apim.policies["bare"]


def test_writes_require_the_csrf_header(client: TestClient) -> None:
    response = client.put("/api/apim/backends/pool-a", json={"url": "https://a"})
    assert response.status_code == 403


def test_easy_auth_owners_and_readers(apim: FakeApim) -> None:
    client = _client(apim, _settings(auth_mode="easy_auth", owners="Owner@Contoso.com"))
    try:
        assert client.get("/api/apim/backends").status_code == 401
        reader = {"X-MS-CLIENT-PRINCIPAL-NAME": "reader@contoso.com"}
        owner = {"X-MS-CLIENT-PRINCIPAL-NAME": "owner@contoso.com"}
        assert client.get("/api/me", headers=reader).json()["can_write"] is False
        assert client.get("/api/apim/backends", headers=reader).status_code == 200
        body = {"url": "https://a.example.com"}
        assert (
            client.put(
                "/api/apim/backends/pool-a", json=body, headers={**reader, **WRITE}
            ).status_code
            == 403
        )
        assert (
            client.put(
                "/api/apim/backends/pool-a", json=body, headers={**owner, **WRITE}
            ).status_code
            == 200
        )
    finally:
        app.dependency_overrides.clear()


def test_disabled_auth_is_refused_on_app_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_SITE_NAME", "pool-manager")
    with pytest.raises(RuntimeError):
        _settings().validate_runtime()


def test_arm_retries_throttling_with_retry_after() -> None:
    calls: list[int] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"value": []})

    arm = ArmClient(_settings(), FakeCredential(), httpx.MockTransport(handler), sleep=slept.append)
    assert list(arm.list("/backends", api_version=GA_API_VERSION)) == []
    assert slept == [3.0]


def test_arm_refuses_foreign_next_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [], "nextLink": "https://evil.example/x"})

    arm = ArmClient(_settings(), FakeCredential(), httpx.MockTransport(handler))
    with pytest.raises(Exception, match="another host"):
        list(arm.list("/backends", api_version=GA_API_VERSION))


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/vnd.ms-azure-apim.policy+xml", DEFAULT_POLICY),
        ("text/plain", DEFAULT_POLICY.replace("<", "&lt;").replace(">", "&gt;")),
        ("application/json", json.dumps({"properties": {"value": DEFAULT_POLICY}})),
    ],
)
def test_policy_responses_are_read_in_every_apim_representation(
    content_type: str, body: str
) -> None:
    response = httpx.Response(200, text=body, headers={"Content-Type": content_type})
    assert policy_response_value(response) == DEFAULT_POLICY


def _seed_gpt6(apim: FakeApim) -> tuple[str, str]:
    east = apim.add_account(
        "foundry-east",
        deployments=[
            ("gpt-6-astra", "gpt-6-astra", "OpenAI"),
            ("claude-opus-5", "claude-opus-5", "Anthropic"),  # Not on /openai/deployments.
        ],
    )
    sweden = apim.add_account(
        "aoai-sweden",
        kind="OpenAI",
        location="swedencentral",
        deployments=[("gpt-6-astra-dz", "gpt-6-astra", "OpenAI")],
    )
    apim.add_account("speech", kind="SpeechServices", deployments=[("x", "x", "OpenAI")])
    apim.add_account(
        "broken", deployments=[("gpt-6-astra", "gpt-6-astra", "OpenAI")], state="Failed"
    )
    return east, sweden


def test_discovery_lists_openai_deployments_with_per_deployment_urls(
    client: TestClient, apim: FakeApim
) -> None:
    _seed_gpt6(apim)
    deployments = client.get("/api/deployments").json()

    assert [(d["account_name"], d["deployment"]) for d in deployments] == [
        ("aoai-sweden", "gpt-6-astra-dz"),
        ("foundry-east", "gpt-6-astra"),
    ]
    sweden = deployments[0]
    assert sweden["model_name"] == "gpt-6-astra"
    assert (
        sweden["backend_url"]
        == "https://aoai-sweden.openai.azure.com/openai/deployments/gpt-6-astra-dz"
    )
    assert sweden["suggested_backend_id"] == "pool-aoai-sweden-gpt-6-astra-dz"
    assert sweden["local_auth_disabled"] is True
    assert sweden["backend_id"] is None


def test_import_creates_managed_identity_members_once(client: TestClient, apim: FakeApim) -> None:
    east, sweden = _seed_gpt6(apim)
    refs = [
        {"account_id": east, "deployment": "gpt-6-astra"},
        {"account_id": sweden, "deployment": "gpt-6-astra-dz"},
    ]
    result = client.post("/api/deployments/import", json={"deployments": refs}, headers=WRITE)
    assert result.status_code == 200, result.text
    ids = result.json()["backend_ids"]
    assert ids == ["pool-foundry-east-gpt-6-astra", "pool-aoai-sweden-gpt-6-astra-dz"]
    assert result.json()["created"] == ids

    backend = client.get(f"/api/apim/backends/{ids[0]}").json()
    assert backend["auth_type"] == "managed_identity"
    assert backend["url"] == "https://foundry-east.openai.azure.com/openai/deployments/gpt-6-astra"
    assert backend["circuit_breaker"]["failure_count"] == 1
    assert not apim.named_values

    # Importing again reuses the backends, and discovery now reports them.
    again = client.post("/api/deployments/import", json={"deployments": refs}, headers=WRITE)
    assert again.json() == {"backend_ids": ids, "created": []}
    assert [d["backend_id"] for d in client.get("/api/deployments").json()] == [ids[1], ids[0]]

    # The imported members form a pool directly.
    pool = client.put(
        "/api/apim/pools/gpt-6-astra",
        json={
            "members": [
                {"backend_id": ids[0], "priority": 0, "weight": 1},
                {"backend_id": ids[1], "priority": 0, "weight": 1},
            ]
        },
        headers=WRITE,
    )
    assert pool.status_code == 200


def test_import_rejects_unknown_or_unsupported_deployments(
    client: TestClient, apim: FakeApim
) -> None:
    east, _ = _seed_gpt6(apim)
    for deployment in ("claude-opus-5", "missing"):
        response = client.post(
            "/api/deployments/import",
            json={"deployments": [{"account_id": east, "deployment": deployment}]},
            headers=WRITE,
        )
        assert response.status_code == 404
    assert not apim.backends


def test_import_avoids_ids_taken_by_unrelated_backends(client: TestClient, apim: FakeApim) -> None:
    east, _ = _seed_gpt6(apim)
    apim.add_backend(
        "pool-foundry-east-gpt-6-astra", {"type": "Single", "url": "https://elsewhere"}
    )
    result = client.post(
        "/api/deployments/import",
        json={"deployments": [{"account_id": east, "deployment": "gpt-6-astra"}]},
        headers=WRITE,
    ).json()
    assert result["created"][0].startswith("pool-foundry-east-gpt-6-astra-")
    assert (
        apim.backends["pool-foundry-east-gpt-6-astra"]["properties"]["url"] == "https://elsewhere"
    )


def test_unreadable_subscriptions_do_not_hide_others(apim: FakeApim) -> None:
    _seed_gpt6(apim)
    client = _client(apim, _settings(discovery_subscriptions="forbidden, sub"))
    try:
        assert len(client.get("/api/deployments").json()) == 2
        capabilities = client.get("/api/apim/capabilities").json()
        assert capabilities["discovery_subscriptions"] == ["forbidden", "sub"]
        assert capabilities["apim_principal_id"] == "apim-mi"
    finally:
        app.dependency_overrides.clear()


def test_suggested_ids_are_valid_apim_names() -> None:
    assert suggested_backend_id("pool-", "acct", "gpt-5.6-luna") == "pool-acct-gpt-5-6-luna"
    long_id = suggested_backend_id("pool-", "a" * 60, "gpt-6-astra-global-standard")
    assert len(long_id) <= 80
    assert long_id == suggested_backend_id("pool-", "a" * 60, "gpt-6-astra-global-standard")


def _import(client: TestClient, refs: list[tuple[str, str]]) -> list[str]:
    response = client.post(
        "/api/deployments/import",
        json={"deployments": [{"account_id": a, "deployment": d} for a, d in refs]},
        headers=WRITE,
    )
    assert response.status_code == 200, response.text
    ids: list[str] = response.json()["backend_ids"]
    return ids


def _pool_body(ids: list[str]) -> dict[str, Any]:
    return {"members": [{"backend_id": i, "priority": 0, "weight": 1} for i in ids]}


def test_pool_members_must_serve_the_same_model_read_from_arm(
    client: TestClient, apim: FakeApim
) -> None:
    east = apim.add_account("aoai-east", deployments=[("gpt-4o", "gpt-4o", "OpenAI")])
    # Same deployment name, different model: only the deployment resource tells them apart.
    west = apim.add_account("aoai-west", deployments=[("gpt-4o", "gpt-4o-mini", "OpenAI")])
    ids = _import(client, [(east, "gpt-4o"), (west, "gpt-4o")])
    before = len(apim.backends)

    check = client.post("/api/apim/pools/model-check", json={"members": ids})
    assert check.status_code == 200, check.text
    body = check.json()
    assert body["ok"] is False
    assert body["model_names"] == ["gpt-4o", "gpt-4o-mini"]
    assert [m["model_name"] for m in body["members"]] == ["gpt-4o", "gpt-4o-mini"]
    assert body["members"][1]["deployment_id"] == f"{west}/deployments/gpt-4o"
    assert any(r.url.path.endswith("/aoai-west/deployments/gpt-4o") for r in apim.requests)

    rejected = client.put("/api/apim/pools/p1", json=_pool_body(ids), headers=WRITE)
    assert rejected.status_code == 409
    assert "same Foundry model" in rejected.json()["detail"]
    assert "gpt-4o-mini" in rejected.json()["detail"]
    assert len(apim.backends) == before  # Nothing was written.

    # Different versions of one model are allowed.
    apim.deployments[west][0]["properties"]["model"].update(name="gpt-4o", version="2024-05-13")
    assert client.post("/api/apim/pools/model-check", json={"members": ids}).json()["ok"] is True
    assert client.put("/api/apim/pools/p1", json=_pool_body(ids), headers=WRITE).status_code == 200


def test_unverifiable_foundry_members_are_rejected(client: TestClient, apim: FakeApim) -> None:
    east = apim.add_account("aoai-east", deployments=[("gpt-4o", "gpt-4o", "OpenAI")])
    west = apim.add_account("aoai-west", deployments=[("gpt-4o-2", "gpt-4o", "OpenAI")])
    ids = _import(client, [(east, "gpt-4o"), (west, "gpt-4o-2")])

    # A Foundry deployment URL whose account is outside the discovery subscriptions.
    client.put(
        "/api/apim/backends/pool-elsewhere",
        json={
            "url": "https://aoai-elsewhere.openai.azure.com/openai/deployments/gpt-4o",
            "auth": {"type": "managed_identity"},
        },
        headers=WRITE,
    )
    outside = client.put(
        "/api/apim/pools/p1", json=_pool_body([ids[0], "pool-elsewhere"]), headers=WRITE
    )
    assert outside.status_code == 409
    assert "cannot verify the model of pool-elsewhere" in outside.json()["detail"]

    # A deployment that was deleted after import.
    apim.deployments[west] = []
    deleted = client.post("/api/apim/pools/model-check", json={"members": ids}).json()
    assert deleted["ok"] is False
    assert deleted["members"][1]["problem"] == "deployment gpt-4o-2 does not exist"
    assert client.put("/api/apim/pools/p1", json=_pool_body(ids), headers=WRITE).status_code == 409


def test_members_that_are_not_foundry_deployments_are_not_model_checked(
    client: TestClient, apim: FakeApim
) -> None:
    east = apim.add_account("aoai-east", deployments=[("gpt-4o", "gpt-4o", "OpenAI")])
    ids = _import(client, [(east, "gpt-4o")])
    _member(client, "pool-custom")  # https://pool-custom.openai.azure.com/openai: no deployment
    client.put(
        "/api/apim/backends/pool-proxy",
        json={
            "url": "https://other-gateway.example.com/openai/deployments/anything",
            "auth": {"type": "none"},
        },
        headers=WRITE,
    )
    check = client.post(
        "/api/apim/pools/model-check", json={"members": [*ids, "pool-custom", "pool-proxy"]}
    ).json()
    assert check["ok"] is True
    assert [m["deployment_id"] is not None for m in check["members"]] == [True, False, False]
    assert check["model_names"] == ["gpt-4o"]
    assert client.post("/api/apim/pools/model-check", json={"members": ["nope"]}).status_code == 400


def test_creating_a_pool_api_creates_and_binds_an_endpoint(
    client: TestClient, apim: FakeApim
) -> None:
    east = apim.add_account("aoai-east", deployments=[("gpt-4o", "gpt-4o", "OpenAI")])
    west = apim.add_account("aoai-west", deployments=[("gpt-4o-2", "gpt-4o", "OpenAI")])
    ids = _import(client, [(east, "gpt-4o"), (west, "gpt-4o-2")])
    assert (
        client.put(
            "/api/apim/pools/pool-gpt-4o",
            json={**_pool_body(ids), "title": "gpt-4o"},
            headers=WRITE,
        ).status_code
        == 200
    )

    created = client.post("/api/apim/pools/pool-gpt-4o/api", json={"path": "gpt-4o"}, headers=WRITE)
    assert created.status_code == 200, created.text
    view = created.json()
    assert (view["id"], view["display_name"], view["path"], view["managed"]) == (
        "pool-gpt-4o",
        "gpt-4o",
        "gpt-4o",
        True,
    )
    # Imported members use the APIM identity, so the binding adds its token.
    assert view["binding"]["pool_id"] == "pool-gpt-4o"
    assert view["binding"]["managed_identity_resource"] == "https://cognitiveservices.azure.com"
    assert view["binding"]["retry_on_429"] is True

    api = apim.apis["pool-gpt-4o"]["properties"]
    assert api["subscriptionRequired"] is True
    assert (view["subscription_required"], view["subscription_key_header"]) == (
        True,
        "Ocp-Apim-Subscription-Key",
    )
    assert api["protocols"] == ["https"]
    assert api["description"].startswith(MANAGED_MARKER)
    create, _ = _last_body(apim, "PUT", "/apis/pool-gpt-4o")
    assert create.headers["If-None-Match"] == "*"
    assert apim.operations[("pool-gpt-4o", "post-wildcard")]["properties"] == {
        "displayName": "POST /*",
        "method": "POST",
        "urlTemplate": "/*",
    }
    assert '<set-backend-service backend-id="pool-gpt-4o" />' in apim.policies["pool-gpt-4o"]
    assert [api["id"] for api in client.get("/api/apim/apis").json()] == ["pool-gpt-4o"]
    assert (
        client.get("/api/apim/capabilities").json()["gateway_url"] == "https://apim.azure-api.net"
    )

    # Retrying after a partial failure completes the same API instead of failing.
    again = client.post("/api/apim/pools/pool-gpt-4o/api", json={"path": "gpt-4o"}, headers=WRITE)
    assert again.status_code == 200, again.text

    # The bound pool still cannot be deleted.
    assert client.delete("/api/apim/pools/pool-gpt-4o", headers=WRITE).status_code == 409


def test_pool_api_refuses_taken_paths_and_foreign_apis(client: TestClient, apim: FakeApim) -> None:
    _two_member_pool(client)  # Members use no auth, so no managed identity policy is added.
    apim.add_api("chat", "chat", DEFAULT_POLICY)
    apim.add_api("p1", "elsewhere", DEFAULT_POLICY)  # Created outside this app.

    taken = client.post(
        "/api/apim/pools/p1/api", json={"api_id": "p1-api", "path": "/chat/"}, headers=WRITE
    )
    assert taken.status_code == 409
    assert "already used by API chat" in taken.json()["detail"]

    foreign = client.post(
        "/api/apim/pools/p1/api", json={"api_id": "p1", "path": "p1"}, headers=WRITE
    )
    assert foreign.status_code == 409
    assert "already exists" in foreign.json()["detail"]
    assert apim.policies["p1"] == DEFAULT_POLICY

    plain = client.post(
        "/api/apim/pools/p1/api", json={"api_id": "p1-api", "path": "p1"}, headers=WRITE
    )
    assert plain.status_code == 200, plain.text
    assert plain.json()["binding"]["managed_identity_resource"] is None
    create = "/api/apim/pools/{}/api"
    assert (
        client.post(create.format("missing"), json={"path": "x"}, headers=WRITE).status_code == 404
    )
    assert client.post(create.format("p1"), json={"path": "a b"}, headers=WRITE).status_code == 422
    assert (
        client.post(create.format("p1"), json={"path": "x"}).status_code == 403
    )  # No CSRF header.


def test_a_pool_can_get_a_second_api_at_another_path(client: TestClient, apim: FakeApim) -> None:
    _two_member_pool(client)
    first = client.post("/api/apim/pools/p1/api", json={"path": "chat"}, headers=WRITE)
    assert first.status_code == 200, first.text
    second = client.post("/api/apim/pools/p1/api", json={"path": "v2/chat"}, headers=WRITE)
    assert second.status_code == 200, second.text
    assert (first.json()["id"], second.json()["id"]) == ("p1", "p1-v2-chat")
    assert second.json()["display_name"] == "p1 (/v2/chat)"
    bound = [api["id"] for api in client.get("/api/apim/apis").json() if api["binding"]]
    assert sorted(bound) == ["p1", "p1-v2-chat"]


def test_enabling_the_apim_identity_shows_up_without_a_restart(
    client: TestClient, apim: FakeApim
) -> None:
    apim.service_principal = None
    assert client.get("/api/apim/capabilities").json()["apim_principal_id"] is None
    apim.service_principal = "apim-mi"
    capabilities = client.get("/api/apim/capabilities").json()
    assert capabilities["apim_principal_id"] == "apim-mi"
    assert capabilities["apim_resource_id"] == SERVICE


_TIGHT_BREAKER = {
    "failure_count": 5,
    "interval_seconds": 30,
    "trip_duration_seconds": 120,
    "accept_retry_after": False,
    "status_code_ranges": [{"minimum": 429, "maximum": 429}],
    "error_reasons": ["Timeout"],
}


def test_pool_breaker_is_written_to_its_managed_members(client: TestClient, apim: FakeApim) -> None:
    _member(client, "m-a")
    _member(client, "m-b")
    apim.add_backend(
        "external",
        {"type": "Single", "url": "https://external.example.com/openai", "protocol": "http"},
    )
    body = {**_pool_body(["m-a", "m-b", "external"]), "circuit_breaker": _TIGHT_BREAKER}
    response = client.put("/api/apim/pools/p1", json=body, headers=WRITE)
    assert response.status_code == 200, response.text

    backends = {item["id"]: item for item in client.get("/api/apim/backends").json()}
    assert backends["m-a"]["circuit_breaker"] == _TIGHT_BREAKER
    assert backends["m-b"]["circuit_breaker"] == _TIGHT_BREAKER
    # A backend this app didn't create is read-only and keeps its own (here: none).
    assert backends["external"]["has_circuit_breaker"] is False
    # Each member write is conditional on the ETag just read.
    member_puts = [
        r for r in apim.requests if r.method == "PUT" and r.url.path.endswith(("/m-a", "/m-b"))
    ]
    assert member_puts[-2:] and all(r.headers.get("If-Match") for r in member_puts[-2:])

    # Saving again with the same rule rewrites nothing.
    before = len(apim.requests)
    pool = client.get("/api/apim/pools/p1").json()
    again = {**body, "etag": pool["etag"]}
    assert client.put("/api/apim/pools/p1", json=again, headers=WRITE).status_code == 200
    rewritten = [
        r
        for r in apim.requests[before:]
        if r.method == "PUT" and r.url.path.endswith(("/m-a", "/m-b"))
    ]
    assert rewritten == []


def test_pool_without_a_breaker_leaves_member_rules_alone(
    client: TestClient, apim: FakeApim
) -> None:
    _member(client, "m-a")
    _member(client, "m-b")
    before = {
        item["id"]: item["circuit_breaker"] for item in client.get("/api/apim/backends").json()
    }
    assert (
        client.put("/api/apim/pools/p1", json=_pool_body(["m-a", "m-b"]), headers=WRITE).status_code
        == 200
    )
    after = {
        item["id"]: item["circuit_breaker"] for item in client.get("/api/apim/backends").json()
    }
    assert after == before


def test_removed_members_are_deleted_once_no_pool_uses_them(
    client: TestClient, apim: FakeApim
) -> None:
    _member(client, "m-a")
    _member(client, "m-b")
    _member(client, "m-c", type="api_key", header_name="api-key", secret={"value": "s3cret"})
    _member(client, "shared")
    apim.add_backend(
        "external",
        {"type": "Single", "url": "https://external.example.com/openai", "protocol": "http"},
    )
    client.put(
        "/api/apim/pools/p1",
        json=_pool_body(["m-a", "m-b", "m-c", "shared", "external"]),
        headers=WRITE,
    )
    client.put("/api/apim/pools/p2", json=_pool_body(["m-a", "shared"]), headers=WRITE)
    assert "m-c-credential" in apim.named_values

    pool = client.get("/api/apim/pools/p1").json()
    body = {**_pool_body(["m-b", "m-a"]), "etag": pool["etag"]}
    assert client.put("/api/apim/pools/p1", json=body, headers=WRITE).status_code == 200

    # m-c was only in p1: gone, with the credential named value this app made for it.
    assert "m-c" not in apim.backends
    assert "m-c-credential" not in apim.named_values
    # shared is still in p2, and external isn't this app's to delete.
    assert "shared" in apim.backends
    assert "external" in apim.backends
    assert {"m-a", "m-b"} <= set(apim.backends)
