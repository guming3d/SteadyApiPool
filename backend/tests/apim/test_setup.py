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
from pool_manager.apim.access import ApimAccessChecker
from pool_manager.app import app
from tests.apim.fake_apim import SERVICE, TENANT, FakeApim
from tests.apim.test_app import WRITE, _client, _import, _seed_gpt6, _settings
from tests.fake_azure import OPENAI_USER

APIM = "Microsoft.ApiManagement/service"
READ = [f"{APIM}/read", f"{APIM}/*/read"]
WRITE_ACTIONS = [
    f"{APIM}/backends/*",
    f"{APIM}/namedValues/*",
    f"{APIM}/apis/write",
    f"{APIM}/apis/delete",
    f"{APIM}/apis/operations/write",
    f"{APIM}/apis/policies/write",
]


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
def apim() -> FakeApim:
    return FakeApim()


@pytest.fixture
def client(apim: FakeApim) -> Iterator[TestClient]:
    yield _client(apim, _settings())
    app.dependency_overrides.clear()


def _checks(client: TestClient, **headers: str) -> dict[str, dict[str, Any]]:
    response = client.get("/api/setup", headers=headers)
    assert response.status_code == 200, response.text
    return {check["id"]: check for check in response.json()["checks"]}


def _mi_members(client: TestClient, apim: FakeApim) -> tuple[str, str]:
    """Imports two managed-identity members on two accounts; the first already has the role."""
    east, sweden = _seed_gpt6(apim)
    _import(client, [(east, "gpt-6-astra"), (sweden, "gpt-6-astra-dz")])
    apim.grant_openai_user(east)
    return east, sweden


def test_local_report_with_full_access(client: TestClient, apim: FakeApim) -> None:
    east, sweden = _mi_members(client, apim)
    report = client.get("/api/setup").json()

    assert report["mode"] == "local"
    assert report["can_operate"] is True
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["sign_in"]["status"] == "ok"
    assert checks["gateway_access"]["status"] == "ok"
    assert checks["discovery:sub"]["status"] == "ok"
    assert checks["gateway_identity"]["status"] == "ok"
    assert checks[f"foundry_access:{east.casefold()}"]["status"] == "ok"
    # The other account lacks the role, and the signed-in user may assign it.
    sweden_check = checks[f"foundry_access:{sweden.casefold()}"]
    assert sweden_check["status"] == "action"
    assert sweden_check["fix"] == "grant_foundry_access"
    assert "--assignee-object-id apim-mi" in sweden_check["request"]["command"]
    assert sweden_check["request"]["role"] == "Cognitive Services OpenAI User"
    assert report["ready"] is False


def test_progress_follows_the_report(
    client: TestClient, apim: FakeApim, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mi_members(client, apim)
    seen: dict[str, Any] = {}
    original = ApimAccessChecker.gateway_identity

    def spy(self: ApimAccessChecker, *args: Any) -> Any:
        seen["identity"] = self.progress("local-developer").model_dump()
        return original(self, *args)

    monkeypatch.setattr(ApimAccessChecker, "gateway_identity", spy)
    assert client.get("/api/setup").status_code == 200

    steps = seen["identity"]["steps"]
    assert seen["identity"]["running"] is True
    assert [(step["kind"], step["state"]) for step in steps] == [
        ("sign_in", "done"),
        ("gateway_target", "done"),
        ("gateway_access", "done"),
        ("discovery", "done"),
        ("gateway_identity", "running"),
    ]
    assert steps[1]["subject"] == "apim"
    assert (steps[3]["done"], steps[3]["total"]) == (1, 1)
    # Nothing is kept once the report is sent.
    assert client.get("/api/setup/progress").json() == {"running": False, "steps": []}


def test_progress_counts_foundry_accounts(
    client: TestClient, apim: FakeApim, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mi_members(client, apim)
    seen: list[Any] = []
    original = ApimAccessChecker._finish

    def spy(self: ApimAccessChecker, *args: Any) -> Any:
        seen.append(self.progress("local-developer").steps[-2:])
        return original(self, *args)

    monkeypatch.setattr(ApimAccessChecker, "_finish", spy)
    client.get("/api/setup")
    members, foundry = seen[0]
    assert (members.kind, members.state) == ("members", "done")
    assert (foundry.kind, foundry.done, foundry.total) == ("foundry_access", 2, 2)


def test_read_only_and_no_access_are_reported(client: TestClient, apim: FakeApim) -> None:
    apim.allow("app", SERVICE, *READ)
    check = _checks(client)["gateway_access"]
    assert check["status"] == "action"
    assert f"{APIM}/backends/write" in check["missing"]
    assert f'--scope "{SERVICE}"' in check["request"]["command"]
    assert check["request"]["role"] == "API Management Service Contributor"
    assert check["request"]["portal_url"].endswith(f"{SERVICE}/users")

    apim.permissions.clear()
    apim.allow("app", "/subscriptions/sub/resourceGroups/other", "*")
    report = client.get("/api/setup?refresh=true").json()
    access = next(item for item in report["checks"] if item["id"] == "gateway_access")
    assert access["status"] == "blocked"
    assert report["can_operate"] is False


def test_custom_role_is_suggested_when_provisioned(client: TestClient, apim: FakeApim) -> None:
    custom = (
        "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Authorization/roleDefinitions/c1"
    )
    apim.role_definitions[custom.casefold()] = {
        "id": custom,
        "properties": {"roleName": "APIM Backend Pool Manager abc123", "type": "CustomRole"},
    }
    apim.allow("app", SERVICE, *READ)
    assert (
        _checks(client)["gateway_access"]["request"]["role"] == "APIM Backend Pool Manager abc123"
    )


def test_discovery_counts_resource_group_access(client: TestClient, apim: FakeApim) -> None:
    _seed_gpt6(apim)
    apim.allow("app", SERVICE, "*")
    apim.allow("app", "/subscriptions/sub/resourceGroups/rg-ai", "*")
    # No subscription-level role, but listing finds accounts the user can see.
    assert _checks(client)["discovery:sub"]["status"] == "ok"

    apim.accounts.clear()
    check = _checks(client)["discovery:sub"]
    assert check["status"] == "action"
    assert check["request"]["role"] == "Reader"


def test_grant_foundry_access(client: TestClient, apim: FakeApim) -> None:
    _, sweden = _mi_members(client, apim)
    body = {"account_ids": [sweden]}
    assert client.post("/api/setup/foundry-access/grant", json=body).status_code == 403  # CSRF

    response = client.post("/api/setup/foundry-access/grant", json=body, headers=WRITE)
    assert response.status_code == 200, response.text
    assert [check["status"] for check in response.json()] == ["ok"]
    granted = apim.role_assignments[-1]["properties"]
    assert granted == {
        "roleDefinitionId": OPENAI_USER,
        "principalId": "apim-mi",
        "principalType": "ServicePrincipal",
        "scope": sweden,
    }
    # Granting again is a no-op, not an error.
    again = client.post("/api/setup/foundry-access/grant", json=body, headers=WRITE)
    assert again.status_code == 200
    assert len(apim.role_assignments) == 2


def test_grant_is_limited_to_known_accounts(client: TestClient, apim: FakeApim) -> None:
    _mi_members(client, apim)
    other = "/subscriptions/sub/resourceGroups/x/providers/Microsoft.CognitiveServices/accounts/y"
    response = client.post(
        "/api/setup/foundry-access/grant", json={"account_ids": [other]}, headers=WRITE
    )
    assert response.status_code == 400
    assert not [item for item in apim.role_assignments if item["properties"]["scope"] == other]


def test_grant_needs_permission_to_assign_roles(client: TestClient, apim: FakeApim) -> None:
    _, sweden = _mi_members(client, apim)
    apim.allow("app", "/subscriptions/sub", "*/read", f"{APIM}/*")
    check = _checks(client)[f"foundry_access:{sweden.casefold()}"]
    assert check["status"] == "action"
    assert check["fix"] is None  # Only an admin request.
    response = client.post(
        "/api/setup/foundry-access/grant", json={"account_ids": [sweden]}, headers=WRITE
    )
    assert response.status_code == 403
    assert "cannot assign roles on aoai-sweden" in response.json()["detail"]


def test_foundry_access_check_for_selected_accounts(client: TestClient, apim: FakeApim) -> None:
    east, sweden = _seed_gpt6(apim)
    apim.grant_openai_user(east)
    response = client.post("/api/setup/foundry-access", json={"account_ids": [east, sweden]})
    assert [check["status"] for check in response.json()] == ["ok", "action"]
    bad = client.post("/api/setup/foundry-access", json={"account_ids": ["not-an-id"]})
    assert bad.status_code == 422


def test_enable_apim_identity(client: TestClient, apim: FakeApim) -> None:
    apim.service_principal = None
    check = _checks(client)["gateway_identity"]
    assert check["status"] == "action"
    assert check["fix"] == "enable_apim_identity"
    assert "--enable-managed-identity true" in check["request"]["command"]

    response = client.post("/api/apim/setup/apim-identity", headers=WRITE)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    patch = next(request for request in apim.requests if request.method == "PATCH")
    assert json.loads(patch.content) == {"identity": {"type": "SystemAssigned"}}
    assert client.get("/api/apim/capabilities").json()["apim_principal_id"] == "apim-mi-new"


def test_enable_apim_identity_needs_service_write(client: TestClient, apim: FakeApim) -> None:
    apim.service_principal = None
    apim.allow("app", SERVICE, *READ, *WRITE_ACTIONS)
    assert _checks(client)["gateway_identity"]["fix"] is None
    assert client.post("/api/apim/setup/apim-identity", headers=WRITE).status_code == 403


def test_identity_being_enabled_is_pending(client: TestClient, apim: FakeApim) -> None:
    apim.service_principal = None
    apim.service_provisioning = "Updating"
    assert _checks(client)["gateway_identity"]["status"] == "pending"


class _SignIn:
    """Fails like a missing ``az login`` until the user signs in from the wizard."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    def get_token(self, *_: str) -> Any:
        if not self.calls:
            raise RuntimeError("Please run 'az login' to set up an account.\nmore detail")
        return type("Token", (), {"token": "token", "expires_on": 4_102_444_800})()

    def sign_in(self, tenant_id: str | None, scope: str) -> None:
        self.calls.append((tenant_id, scope))


def test_local_sign_in(apim: FakeApim) -> None:
    credential = _SignIn()
    client = _client(apim, _settings(), sign_in=credential, credential=credential)
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


def test_wrong_tenant_asks_to_sign_in_again(apim: FakeApim) -> None:
    token = _jwt(tid="11111111-1111-1111-1111-111111111111")
    apim.callers[token] = "app"
    credential = type(
        "Credential",
        (),
        {"get_token": lambda self, *_: type("T", (), {"token": token, "expires_on": 4e9})()},
    )()
    client = _client(apim, _settings(), credential=credential, sign_in=_SignIn())
    try:
        check = _checks(client)["sign_in"]
        assert check["status"] == "action"
        assert check["fix"] == "sign_in"
        assert TENANT in check["detail"]
    finally:
        app.dependency_overrides.clear()


# -- deployed (Easy Auth) --------------------------------------------------------------


@pytest.fixture
def deployed(apim: FakeApim) -> Iterator[TestClient]:
    yield _client(
        apim, _settings(auth_mode="easy_auth", owners="owner@contoso.com", delegated_access=True)
    )
    app.dependency_overrides.clear()


def _user(apim: FakeApim, *actions: str, **claims: Any) -> dict[str, str]:
    token = _jwt(**claims)
    apim.callers[token] = "user"
    apim.allow("user", SERVICE, *actions)
    return {"X-MS-CLIENT-PRINCIPAL-NAME": "dev@contoso.com", "X-MS-TOKEN-AAD-ACCESS-TOKEN": token}


def test_deployed_user_with_azure_roles_can_write(deployed: TestClient, apim: FakeApim) -> None:
    headers = _user(apim, *READ, *WRITE_ACTIONS)
    assert deployed.get("/api/me", headers=headers).json()["can_write"] is True
    body = {"url": "https://a.example.com", "auth": {"type": "none"}}
    response = deployed.put("/api/apim/backends/pool-a", json=body, headers={**headers, **WRITE})
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


def test_deployed_progress_needs_sign_in(deployed: TestClient, apim: FakeApim) -> None:
    assert deployed.get("/api/setup/progress").status_code == 401
    response = deployed.get("/api/setup/progress", headers=_user(apim, *READ))
    assert response.json() == {"running": False, "steps": []}
    # Unlike other reads, the poll doesn't ask Azure whether the caller may write.
    assert not apim.requests


def test_deployed_reader_stays_read_only(deployed: TestClient, apim: FakeApim) -> None:
    headers = _user(apim, *READ)
    assert deployed.get("/api/me", headers=headers).json()["can_write"] is False
    body = {"url": "https://a.example.com", "auth": {"type": "none"}}
    assert (
        deployed.put(
            "/api/apim/backends/pool-a", json=body, headers={**headers, **WRITE}
        ).status_code
        == 403
    )
    check = _checks(deployed, **headers)["gateway_access"]
    assert check["status"] == "action"
    assert (
        "--assignee-object-id user-oid --assignee-principal-type User"
        in check["request"]["command"]
    )


def test_rbac_writers_can_be_turned_off(apim: FakeApim) -> None:
    client = _client(apim, _settings(auth_mode="easy_auth", rbac_writers=False))
    try:
        headers = _user(apim, *READ, *WRITE_ACTIONS)
        assert client.get("/api/me", headers=headers).json()["can_write"] is False
    finally:
        app.dependency_overrides.clear()


def test_deployed_without_a_token_asks_for_consent(deployed: TestClient, apim: FakeApim) -> None:
    reader = {"X-MS-CLIENT-PRINCIPAL-NAME": "reader@contoso.com"}
    checks = _checks(deployed, **reader)
    assert checks["azure_token"]["fix"] == "consent"
    assert checks["gateway_access"]["status"] == "unknown"
    owner = {"X-MS-CLIENT-PRINCIPAL-NAME": "owner@contoso.com"}
    assert _checks(deployed, **owner)["gateway_access"]["granted_by"] == "owners"
    # Fixes need the user's own token.
    response = deployed.post("/api/apim/setup/apim-identity", headers={**owner, **WRITE})
    assert response.status_code == 401


def test_delegated_access_off_needs_an_admin(apim: FakeApim) -> None:
    client = _client(apim, _settings(auth_mode="easy_auth"))
    try:
        check = _checks(client, **{"X-MS-CLIENT-PRINCIPAL-NAME": "a@contoso.com"})["azure_token"]
        assert check["fix"] is None
        assert "ENABLE_DELEGATED_AZURE_ACCESS" in check["request"]["command"]
    finally:
        app.dependency_overrides.clear()


def test_deployed_token_problems(deployed: TestClient, apim: FakeApim) -> None:
    expired = _user(apim, *READ, exp=int(time.time()) - 10)
    assert _checks(deployed, **expired)["azure_token"]["fix"] == "refresh_token"
    graph = _user(apim, *READ, aud="https://graph.microsoft.com")
    check = _checks(deployed, **graph)["azure_token"]
    assert check["fix"] == "consent"
    assert "graph.microsoft.com" in check["detail"]


def test_deployed_grant_runs_as_the_user(deployed: TestClient, apim: FakeApim) -> None:
    owner_client = _client(apim, _settings())  # Import as the app first.
    _, sweden = _mi_members(owner_client, apim)
    deployed = _client(
        apim, _settings(auth_mode="easy_auth", owners="owner@contoso.com", delegated_access=True)
    )
    headers = _user(apim, *READ)
    apim.allow("user", sweden, "Microsoft.Authorization/roleAssignments/write")
    check = _checks(deployed, **headers)[f"foundry_access:{sweden.casefold()}"]
    assert check["fix"] == "grant_foundry_access"

    response = deployed.post(
        "/api/setup/foundry-access/grant",
        json={"account_ids": [sweden]},
        headers={**headers, **WRITE},
    )
    assert response.status_code == 200, response.text
    put = next(
        request
        for request in apim.requests
        if request.method == "PUT" and "roleAssignments" in request.url.path
    )
    assert put.headers["Authorization"] == f"Bearer {headers['X-MS-TOKEN-AAD-ACCESS-TOKEN']}"


def test_permission_matching() -> None:
    entries = [{"actions": ["Microsoft.ApiManagement/*"], "notActions": ["*/delete"]}]
    assert allows(entries, "Microsoft.ApiManagement/service/backends/write")
    assert allows(entries, "microsoft.apimanagement/service/read")
    assert not allows(entries, "Microsoft.ApiManagement/service/backends/delete")
    # Another assignment can still allow what the first one excludes.
    assert allows([*entries, {"actions": ["*"]}], "Microsoft.ApiManagement/service/backends/delete")
    data = [{"dataActions": ["Microsoft.CognitiveServices/*"]}]
    assert allows(
        data,
        "Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action",
        data=True,
    )
    assert not allows(data, "Microsoft.CognitiveServices/accounts/read")


def test_a_token_for_someone_else_is_ignored(deployed: TestClient, apim: FakeApim) -> None:
    writer = _user(apim, *READ, *WRITE_ACTIONS, oid="owner-oid")
    # A reader replays a token whose oid is not their own principal ID.
    headers = {**writer, "X-MS-CLIENT-PRINCIPAL-ID": "reader-oid"}
    assert deployed.get("/api/me", headers=headers).json()["can_write"] is False
    assert user_token(headers) is None
    assert user_token({**writer, "X-MS-CLIENT-PRINCIPAL-ID": "OWNER-OID"}) is not None


def test_forged_claims_do_not_reuse_a_cached_answer(deployed: TestClient, apim: FakeApim) -> None:
    owner = _user(apim, *READ, *WRITE_ACTIONS, oid="same-oid")
    assert deployed.get("/api/me", headers=owner).json()["can_write"] is True
    # Same unverified oid, different token: ARM is asked again and says no.
    forged = _jwt(oid="same-oid", upn="forged")
    apim.callers[forged] = "reader"
    apim.allow("reader", SERVICE, *READ)
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
