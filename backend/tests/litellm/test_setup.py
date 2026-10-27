"""The setup wizard: permission checks and the fixes the app makes with the user's rights."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pool_manager.access import allows, user_token
from pool_manager.app import app
from pool_manager.litellm.access import LiteLLMAccessChecker
from tests.fake_azure import GATEWAY_APP, GATEWAY_PRINCIPAL, OPENAI_USER, TENANT, FakeAzure
from tests.litellm.fake_litellm import MASTER_KEY, URL
from tests.litellm.test_app import WRITE, _client, _settings, import_members, seed

READ = ["Microsoft.App/containerApps/read", "*/read"]
SECRETS = ["Microsoft.App/containerApps/listSecrets/action"]


def _jwt(**claims: Any) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    payload = {
        "aud": "https://management.azure.com/",
        "oid": "user-oid",
        "tid": TENANT,
        "upn": "dev@contoso.com",
        "exp": int(time.time()) + 3600,
        **claims,
    }
    return f"{part({'alg': 'none'})}.{part(payload)}.sig"


@pytest.fixture
def azure() -> FakeAzure:
    return FakeAzure()


@pytest.fixture
def client(azure: FakeAzure) -> Iterator[TestClient]:
    yield _client(azure, _settings())
    app.dependency_overrides.clear()


def _checks(client: TestClient, **headers: str) -> dict[str, dict[str, Any]]:
    response = client.get("/api/setup", headers=headers)
    assert response.status_code == 200, response.text
    return {check["id"]: check for check in response.json()["checks"]}


def _members(client: TestClient, azure: FakeAzure) -> tuple[str, str]:
    """A pool of two managed-identity members on two accounts; only the first has the role."""
    east, west = seed(azure)
    azure.role_assignments = [
        item for item in azure.role_assignments if item["properties"]["scope"] != west
    ]
    members = import_members(client, [east, west])
    body = {
        "members": [{"backend_id": m, "priority": 0, "weight": 1} for m in members],
    }
    assert client.put("/api/litellm/pools/gpt6", json=body, headers=WRITE).status_code == 200
    return east, west


def test_local_report_with_full_access(client: TestClient, azure: FakeAzure) -> None:
    east, west = _members(client, azure)
    report = client.get("/api/setup").json()

    assert report["mode"] == "local"
    assert report["can_operate"] is True
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["sign_in"]["status"] == "ok"
    assert checks["gateway_target"]["status"] == "ok"
    assert checks["gateway_access"]["status"] == "ok"
    assert checks["gateway_health"] == {
        **checks["gateway_health"],
        "status": "ok",
        "detail": "1.106.4",
    }
    assert checks["discovery:sub"]["status"] == "ok"
    assert checks["gateway_identity"] == {
        **checks["gateway_identity"],
        "status": "ok",
        "subject": GATEWAY_PRINCIPAL,
    }
    assert checks[f"foundry_access:{east.casefold()}"]["status"] == "ok"
    # The other account lacks the role, and the signed-in user may assign it.
    west_check = checks[f"foundry_access:{west.casefold()}"]
    assert west_check["status"] == "action"
    assert west_check["fix"] == "grant_foundry_access"
    assert f"--assignee-object-id {GATEWAY_PRINCIPAL}" in west_check["request"]["command"]
    assert west_check["request"]["role"] == "Cognitive Services OpenAI User"
    assert report["ready"] is False


def test_progress_follows_the_report(
    client: TestClient, azure: FakeAzure, monkeypatch: pytest.MonkeyPatch
) -> None:
    _members(client, azure)
    seen: dict[str, Any] = {}
    original = LiteLLMAccessChecker.gateway_identity

    def spy(self: LiteLLMAccessChecker, permissions: Any) -> Any:
        seen["identity"] = self.progress("local-developer").model_dump()
        return original(self, permissions)

    monkeypatch.setattr(LiteLLMAccessChecker, "gateway_identity", spy)
    assert client.get("/api/setup").status_code == 200

    steps = seen["identity"]["steps"]
    assert seen["identity"]["running"] is True
    assert [(step["kind"], step["state"]) for step in steps] == [
        ("sign_in", "done"),
        ("gateway_target", "done"),
        ("gateway_access", "done"),
        ("gateway_health", "done"),
        ("discovery", "done"),
        ("gateway_identity", "running"),
    ]
    assert steps[1]["subject"] == "gw"
    assert (steps[4]["done"], steps[4]["total"]) == (1, 1)
    # Nothing is kept once the report is sent.
    assert client.get("/api/setup/progress").json() == {"running": False, "steps": []}


def test_without_the_master_key_nothing_can_be_managed(
    client: TestClient, azure: FakeAzure
) -> None:
    azure.allow("app", GATEWAY_APP, *READ)
    report = client.get("/api/setup").json()
    checks = {check["id"]: check for check in report["checks"]}

    access = checks["gateway_access"]
    assert access["status"] == "blocked"
    assert access["missing"] == SECRETS
    assert f'--scope "{GATEWAY_APP}"' in access["request"]["command"]
    assert access["request"]["role"] == "Container Apps Contributor"
    assert report["can_operate"] is False
    assert "gateway_health" not in checks


def test_custom_role_is_suggested_when_provisioned(client: TestClient, azure: FakeAzure) -> None:
    custom = (
        "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Authorization/roleDefinitions/c1"
    )
    azure.role_definitions[custom.casefold()] = {
        "id": custom,
        "properties": {"roleName": "AI Gateway Pool Manager abc123", "type": "CustomRole"},
    }
    azure.allow("app", GATEWAY_APP, *READ)
    assert _checks(client)["gateway_access"]["request"]["role"] == "AI Gateway Pool Manager abc123"


def test_an_unhealthy_gateway_blocks_the_rest(client: TestClient, azure: FakeAzure) -> None:
    azure.litellm.db = "Not connected"
    report = client.get("/api/setup").json()
    health = next(check for check in report["checks"] if check["id"] == "gateway_health")

    assert health["status"] == "blocked"
    assert health["detail"] == "database Not connected"
    assert report["can_operate"] is False

    azure.litellm.db = "connected"
    azure.litellm.master_key = "rotated"
    health = _checks(client)["gateway_health"]
    assert (health["status"], health["detail"]) == ("blocked", "master_key_refused")


def test_a_deploying_gateway_is_pending(azure: FakeAzure) -> None:
    azure.add_resource_group("rg-new")
    client = _client(
        azure, _settings(subscription_id="sub", resource_group="rg-new", gateway_name="fresh")
    )
    try:
        azure.arm_deployments[
            "/subscriptions/sub/resourcegroups/rg-new/providers/"
            "microsoft.resources/deployments/ai-gateway-fresh"
        ] = {
            "id": "x",
            "properties": {"provisioningState": "Running", "timestamp": "2026-10-26T09:00:00Z"},
            "operations": [],
        }
        report = client.get("/api/setup").json()
    finally:
        app.dependency_overrides.clear()

    target = report["checks"][1]
    assert (target["id"], target["status"], target["detail"]) == (
        "gateway_target",
        "pending",
        "Deploying",
    )
    assert target["started_at"] == "2026-10-26T09:00:00Z"
    assert report["can_operate"] is False


def test_a_failed_first_deployment_stops_setup_there(azure: FakeAzure) -> None:
    azure.add_resource_group("rg-new")
    client = _client(
        azure, _settings(subscription_id="sub", resource_group="rg-new", gateway_name="fresh")
    )
    try:
        azure.arm_deployments[
            "/subscriptions/sub/resourcegroups/rg-new/providers/"
            "microsoft.resources/deployments/ai-gateway-fresh"
        ] = {
            "id": "x",
            "properties": {
                "provisioningState": "Failed",
                "error": {"code": "ParameterOutOfRange", "message": "Version should be in: []"},
            },
            "operations": [],
        }
        report = client.get("/api/setup").json()
    finally:
        app.dependency_overrides.clear()

    # No Container App yet, so there is no gateway to reach or check access to.
    assert [check["id"] for check in report["checks"]] == ["sign_in", "gateway_target"]
    target = report["checks"][1]
    assert (target["status"], target["detail"]) == ("action", "DeployFailed")
    assert target["missing"] == ["ParameterOutOfRange: Version should be in: []"]
    assert report["can_operate"] is False


def test_a_missing_gateway_is_blocked(azure: FakeAzure) -> None:
    client = _client(azure, _settings(gateway_name="gone"))
    try:
        target = _checks(client)["gateway_target"]
    finally:
        app.dependency_overrides.clear()

    assert (target["status"], target["detail"]) == ("blocked", "not_found")


def test_discovery_counts_resource_group_access(client: TestClient, azure: FakeAzure) -> None:
    seed(azure)
    azure.allow("app", GATEWAY_APP, "*")
    azure.allow("app", "/subscriptions/sub/resourceGroups/rg-ai", "*")
    # No subscription-level role, but listing finds accounts the user can see.
    assert _checks(client)["discovery:sub"]["status"] == "ok"

    azure.accounts.clear()
    check = _checks(client)["discovery:sub"]
    assert check["status"] == "action"
    assert check["request"]["role"] == "Reader"


def test_grant_foundry_access(client: TestClient, azure: FakeAzure) -> None:
    _, west = _members(client, azure)
    body = {"account_ids": [west]}
    assert client.post("/api/setup/foundry-access/grant", json=body).status_code == 403  # CSRF

    response = client.post("/api/setup/foundry-access/grant", json=body, headers=WRITE)
    assert response.status_code == 200, response.text
    assert [check["status"] for check in response.json()] == ["ok"]
    granted = azure.role_assignments[-1]["properties"]
    assert granted == {
        "roleDefinitionId": OPENAI_USER,
        "principalId": GATEWAY_PRINCIPAL,
        "principalType": "ServicePrincipal",
        "scope": west,
    }
    # Granting again is a no-op, not an error.
    count = len(azure.role_assignments)
    again = client.post("/api/setup/foundry-access/grant", json=body, headers=WRITE)
    assert again.status_code == 200
    assert len(azure.role_assignments) == count


def test_a_grant_counts_before_azure_lists_it(client: TestClient, azure: FakeAzure) -> None:
    _, west = _members(client, azure)
    body = {"account_ids": [west]}
    # Azure's role assignment list misses a new assignment for a while after it is created.
    azure.role_assignment_lag = True

    granted = client.post("/api/setup/foundry-access/grant", json=body, headers=WRITE)
    checked = client.post("/api/setup/foundry-access", json=body)

    assert [check["status"] for check in granted.json()] == ["ok"]
    assert [(check["status"], check["detail"]) for check in checked.json()] == [("ok", "granted")]
    # Once Azure lists it, it is simply there.
    azure.hidden_assignments.clear()
    listed = client.post("/api/setup/foundry-access", json=body).json()
    assert [(check["status"], check["detail"]) for check in listed] == [("ok", None)]


def test_grant_is_limited_to_known_accounts(client: TestClient, azure: FakeAzure) -> None:
    _members(client, azure)
    other = "/subscriptions/sub/resourceGroups/x/providers/Microsoft.CognitiveServices/accounts/y"
    response = client.post(
        "/api/setup/foundry-access/grant", json={"account_ids": [other]}, headers=WRITE
    )
    assert response.status_code == 400
    assert not [item for item in azure.role_assignments if item["properties"]["scope"] == other]


def test_grant_needs_permission_to_assign_roles(client: TestClient, azure: FakeAzure) -> None:
    _, west = _members(client, azure)
    azure.allow("app", "/subscriptions/sub", "*/read", "Microsoft.App/*")
    check = _checks(client)[f"foundry_access:{west.casefold()}"]
    assert check["status"] == "action"
    assert check["fix"] is None  # Only an admin request.
    response = client.post(
        "/api/setup/foundry-access/grant", json={"account_ids": [west]}, headers=WRITE
    )
    assert response.status_code == 403
    assert "cannot assign roles on aoai-west" in response.json()["detail"]


def test_foundry_access_check_for_selected_accounts(client: TestClient, azure: FakeAzure) -> None:
    east, west = seed(azure)
    azure.role_assignments = [
        item for item in azure.role_assignments if item["properties"]["scope"] != west
    ]
    response = client.post("/api/setup/foundry-access", json={"account_ids": [east, west]})
    assert [check["status"] for check in response.json()] == ["ok", "action"]
    bad = client.post("/api/setup/foundry-access", json={"account_ids": ["not-an-id"]})
    assert bad.status_code == 422


def test_a_gateway_without_an_identity(azure: FakeAzure) -> None:
    azure.add_gateway("gw", principal=None)
    client = _client(azure, _settings())
    try:
        check = _checks(client)["gateway_identity"]
    finally:
        app.dependency_overrides.clear()

    assert check["status"] == "action"
    assert check["fix"] is None
    assert "az containerapp identity assign -g rg -n gw" in check["request"]["command"]


# -- external gateways ------------------------------------------------------------------------


def _external(**overrides: Any) -> dict[str, Any]:
    return {
        "resource_group": "",
        "gateway_name": "aks-gateway",
        "gateway_url": URL,
        **overrides,
    }


def test_an_external_gateway_needs_its_key(azure: FakeAzure) -> None:
    client = _client(azure, _settings(**_external()))
    try:
        checks = _checks(client)
    finally:
        app.dependency_overrides.clear()

    assert checks["gateway_target"]["status"] == "ok"
    assert checks["gateway_access"]["status"] == "ok"
    health = checks["gateway_health"]
    assert health["status"] == "blocked"
    assert "master key" in health["detail"]


def test_an_external_gateway_with_its_key(azure: FakeAzure) -> None:
    client = _client(
        azure, _settings(**_external(gateway_master_key=MASTER_KEY, gateway_principal_id="aks"))
    )
    try:
        report = client.get("/api/setup").json()
    finally:
        app.dependency_overrides.clear()

    checks = {check["id"]: check for check in report["checks"]}
    assert report["can_operate"] is True
    assert checks["gateway_health"]["status"] == "ok"
    assert checks["gateway_identity"]["subject"] == "aks"
    # Nothing was asked of a Container App: there is none.
    assert not [r for r in azure.requests if "containerApps" in r.url.path]


def test_an_external_gateway_without_its_identity(azure: FakeAzure) -> None:
    client = _client(azure, _settings(**_external(gateway_master_key=MASTER_KEY)))
    try:
        check = _checks(client)["gateway_identity"]
    finally:
        app.dependency_overrides.clear()

    assert (check["status"], check["detail"]) == ("action", "external_unknown")


# -- signing in -----------------------------------------------------------------------------


class _SignIn:
    """Fails like a missing ``az login`` until the user signs in from the wizard."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    def get_token(self, *_: str, **__: Any) -> Any:
        if not self.calls:
            raise RuntimeError("Please run 'az login' to set up an account.\nmore detail")
        return type("Token", (), {"token": "token", "expires_on": 4_102_444_800})()

    def sign_in(self, tenant_id: str | None, scope: str) -> None:
        self.calls.append((tenant_id, scope))


def test_local_sign_in(azure: FakeAzure) -> None:
    credential = _SignIn()
    client = _client(azure, _settings(), sign_in=credential, credential=credential)
    try:
        report = client.get("/api/setup").json()
        assert report["can_operate"] is False
        assert [check["id"] for check in report["checks"]] == ["sign_in"]
        assert report["checks"][0] == {
            **report["checks"][0],
            "status": "action",
            "fix": "sign_in",
            "detail": "Please run 'az login' to set up an account.",
        }
        assert client.post("/api/setup/sign-in", headers=WRITE).status_code == 204
        assert credential.calls == [(TENANT, "https://management.azure.com/.default")]
        assert _checks(client)["sign_in"]["status"] == "ok"
    finally:
        app.dependency_overrides.clear()


def test_wrong_tenant_asks_to_sign_in_again(azure: FakeAzure) -> None:
    token = _jwt(tid="11111111-1111-1111-1111-111111111111")
    azure.callers[token] = "app"
    credential = type(
        "Credential",
        (),
        {"get_token": lambda self, *_, **__: type("T", (), {"token": token, "expires_on": 4e9})()},
    )()
    client = _client(azure, _settings(), credential=credential, sign_in=_SignIn())
    try:
        check = _checks(client)["sign_in"]
        assert check["status"] == "action"
        assert check["fix"] == "sign_in"
        assert TENANT in check["detail"]
    finally:
        app.dependency_overrides.clear()


# -- deployed (Easy Auth) --------------------------------------------------------------


@pytest.fixture
def deployed(azure: FakeAzure) -> Iterator[TestClient]:
    yield _client(
        azure, _settings(auth_mode="easy_auth", owners="owner@contoso.com", delegated_access=True)
    )
    app.dependency_overrides.clear()


def _user(azure: FakeAzure, *actions: str, **claims: Any) -> dict[str, str]:
    token = _jwt(**claims)
    azure.callers[token] = "user"
    azure.allow("user", GATEWAY_APP, *actions)
    return {"X-MS-CLIENT-PRINCIPAL-NAME": "dev@contoso.com", "X-MS-TOKEN-AAD-ACCESS-TOKEN": token}


def test_deployed_user_with_azure_roles_can_write(deployed: TestClient, azure: FakeAzure) -> None:
    headers = _user(azure, *READ, *SECRETS)
    assert deployed.get("/api/me", headers=headers).json()["can_write"] is True
    body = {
        "url": "https://a.openai.azure.com/openai/deployments/x",
        "auth": {"type": "managed_identity"},
    }
    response = deployed.put("/api/litellm/backends/pool-a", json=body, headers={**headers, **WRITE})
    assert response.status_code == 200, response.text

    checks = _checks(deployed, **headers)
    assert checks["azure_token"]["status"] == "ok"
    assert checks["gateway_access"] == {
        **checks["gateway_access"],
        "status": "ok",
        "granted_by": "azure_rbac",
    }
    assert checks["app_gateway"]["status"] == "ok"
    assert checks["app_discovery:sub"]["status"] == "ok"
    report = deployed.get("/api/setup", headers=headers).json()
    assert report["mode"] == "deployed"
    assert report["user"] == {
        "name": "dev@contoso.com",
        "object_id": "user-oid",
        "tenant_id": TENANT,
    }


def test_deployed_progress_needs_sign_in(deployed: TestClient, azure: FakeAzure) -> None:
    assert deployed.get("/api/setup/progress").status_code == 401
    response = deployed.get("/api/setup/progress", headers=_user(azure, *READ))
    assert response.json() == {"running": False, "steps": []}
    # Unlike other reads, the poll doesn't ask Azure whether the caller may write.
    assert not azure.requests


def test_deployed_reader_stays_read_only(deployed: TestClient, azure: FakeAzure) -> None:
    headers = _user(azure, *READ)
    assert deployed.get("/api/me", headers=headers).json()["can_write"] is False
    body = {"url": "https://a.openai.azure.com/openai/deployments/x"}
    assert (
        deployed.put(
            "/api/litellm/backends/pool-a", json=body, headers={**headers, **WRITE}
        ).status_code
        == 403
    )
    check = _checks(deployed, **headers)["gateway_access"]
    assert check["status"] == "action"
    assert (
        "--assignee-object-id user-oid --assignee-principal-type User"
        in check["request"]["command"]
    )


def test_the_app_identity_needs_the_master_key(deployed: TestClient, azure: FakeAzure) -> None:
    headers = _user(azure, *READ, *SECRETS)
    azure.allow("app", GATEWAY_APP, *READ)
    report = deployed.get("/api/setup", headers=headers).json()
    app_check = next(check for check in report["checks"] if check["id"] == "app_gateway")

    assert app_check["status"] == "blocked"
    assert "azd provision" in app_check["request"]["command"]
    assert report["can_operate"] is False


def test_rbac_writers_can_be_turned_off(azure: FakeAzure) -> None:
    client = _client(azure, _settings(auth_mode="easy_auth", rbac_writers=False))
    try:
        headers = _user(azure, *READ, *SECRETS)
        assert client.get("/api/me", headers=headers).json()["can_write"] is False
    finally:
        app.dependency_overrides.clear()


def test_deployed_without_a_token_asks_for_consent(deployed: TestClient, azure: FakeAzure) -> None:
    reader = {"X-MS-CLIENT-PRINCIPAL-NAME": "reader@contoso.com"}
    checks = _checks(deployed, **reader)
    assert checks["azure_token"]["fix"] == "consent"
    assert checks["gateway_access"]["status"] == "unknown"
    owner = {"X-MS-CLIENT-PRINCIPAL-NAME": "owner@contoso.com"}
    assert _checks(deployed, **owner)["gateway_access"]["granted_by"] == "owners"
    # Fixes need the user's own token.
    response = deployed.post(
        "/api/setup/foundry-access/grant",
        json={"account_ids": [seed(azure)[0]]},
        headers={**owner, **WRITE},
    )
    assert response.status_code == 401


def test_delegated_access_off_needs_an_admin(azure: FakeAzure) -> None:
    client = _client(azure, _settings(auth_mode="easy_auth"))
    try:
        check = _checks(client, **{"X-MS-CLIENT-PRINCIPAL-NAME": "a@contoso.com"})["azure_token"]
        assert check["fix"] is None
        assert "ENABLE_DELEGATED_AZURE_ACCESS" in check["request"]["command"]
    finally:
        app.dependency_overrides.clear()


def test_deployed_token_problems(deployed: TestClient, azure: FakeAzure) -> None:
    expired = _user(azure, *READ, exp=int(time.time()) - 10)
    assert _checks(deployed, **expired)["azure_token"]["fix"] == "refresh_token"
    graph = _user(azure, *READ, aud="https://graph.microsoft.com")
    check = _checks(deployed, **graph)["azure_token"]
    assert check["fix"] == "consent"
    assert "graph.microsoft.com" in check["detail"]


def test_deployed_grant_runs_as_the_user(azure: FakeAzure) -> None:
    local = _client(azure, _settings())  # Create the pool as the app first.
    _, west = _members(local, azure)
    app.dependency_overrides.clear()
    deployed = _client(
        azure, _settings(auth_mode="easy_auth", owners="owner@contoso.com", delegated_access=True)
    )
    try:
        headers = _user(azure, *READ)
        azure.allow("user", west, "Microsoft.Authorization/roleAssignments/write")
        check = _checks(deployed, **headers)[f"foundry_access:{west.casefold()}"]
        assert check["fix"] == "grant_foundry_access"

        response = deployed.post(
            "/api/setup/foundry-access/grant",
            json={"account_ids": [west]},
            headers={**headers, **WRITE},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    put = next(
        request
        for request in azure.requests
        if request.method == "PUT" and "roleAssignments" in request.url.path
    )
    assert put.headers["Authorization"] == f"Bearer {headers['X-MS-TOKEN-AAD-ACCESS-TOKEN']}"


def test_permission_matching() -> None:
    entries = [{"actions": ["Microsoft.App/*"], "notActions": ["*/delete"]}]
    assert allows(entries, "Microsoft.App/containerApps/listSecrets/action")
    assert allows(entries, "microsoft.app/containerapps/read")
    assert not allows(entries, "Microsoft.App/containerApps/delete")
    # Another assignment can still allow what the first one excludes.
    assert allows([*entries, {"actions": ["*"]}], "Microsoft.App/containerApps/delete")
    data = [{"dataActions": ["Microsoft.CognitiveServices/*"]}]
    assert allows(
        data,
        "Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action",
        data=True,
    )
    assert not allows(data, "Microsoft.CognitiveServices/accounts/read")


def test_a_token_for_someone_else_is_ignored(deployed: TestClient, azure: FakeAzure) -> None:
    writer = _user(azure, *READ, *SECRETS, oid="owner-oid")
    # A reader replays a token whose oid is not their own principal ID.
    headers = {**writer, "X-MS-CLIENT-PRINCIPAL-ID": "reader-oid"}
    assert deployed.get("/api/me", headers=headers).json()["can_write"] is False
    assert user_token(headers) is None
    assert user_token({**writer, "X-MS-CLIENT-PRINCIPAL-ID": "OWNER-OID"}) is not None


def test_forged_claims_do_not_reuse_a_cached_answer(deployed: TestClient, azure: FakeAzure) -> None:
    owner = _user(azure, *READ, *SECRETS, oid="same-oid")
    assert deployed.get("/api/me", headers=owner).json()["can_write"] is True
    # Same unverified oid, different token: ARM is asked again and says no.
    forged = _jwt(oid="same-oid", upn="forged")
    azure.callers[forged] = "reader"
    azure.allow("reader", GATEWAY_APP, *READ)
    headers = {**owner, "X-MS-TOKEN-AAD-ACCESS-TOKEN": forged}
    assert deployed.get("/api/me", headers=headers).json()["can_write"] is False


def test_user_token_reads_easy_auth_expiry() -> None:
    token = user_token(
        {
            "X-MS-TOKEN-AAD-ACCESS-TOKEN": "abc",
            "X-MS-TOKEN-AAD-EXPIRES-ON": "2026-10-24T01:28:11.0000000Z",
        }
    )
    assert token is not None
    assert token.expires_on == 1_792_805_291.0
    assert user_token({}) is None
