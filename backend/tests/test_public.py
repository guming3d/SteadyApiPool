"""The public app (auth_mode=entra): sign-in with users' own Entra tokens, the on-behalf-of
exchange, and isolation between users signed in at the same time."""

from __future__ import annotations

import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from pool_manager import target as target_module
from pool_manager.api_test import ApiTester
from pool_manager.app import app, get_sessions, get_settings, get_verifier
from pool_manager.arm import ArmClient
from pool_manager.config import Settings
from pool_manager.entra import (
    ARM_USER_SCOPE,
    EntraUser,
    OboExchanger,
    SignInRequired,
    TokenVerifier,
    UserCredential,
)
from pool_manager.models import ApiTestRequest
from pool_manager.service import PoolManagerService, ServiceError
from pool_manager.sessions import Sessions, parse_target
from tests.fake_apim import SUBSCRIPTION, TENANT, FakeApim
from tests.test_app import WRITE, _settings

CLIENT = "c1c1c1c1-0000-0000-0000-000000000001"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
AUTHORITY = "https://login.microsoftonline.com"
TARGET = "sub/rg/apim"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _app_token(user: str = "alice", tenant: str = TENANT, *, key: Any = _KEY, **claims: Any) -> str:
    """A token for this app's API, as Entra would sign it."""
    now = int(time.time())
    payload = {
        "aud": CLIENT,
        "iss": f"{AUTHORITY}/{tenant}/v2.0",
        "tid": tenant,
        "oid": f"oid-{user}",
        "preferred_username": f"{user}@contoso.com",
        "scp": "access_as_user",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        **claims,
    }
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, key, algorithm="RS256")


def _arm_token(user: EntraUser) -> str:
    """What the exchange returns: an ARM token for the user (unsigned; the fake reads it)."""
    claims = {"oid": user.object_id, "tid": user.tenant_id, "upn": user.name}
    return jwt.encode(claims, "not-verified-here-padding-for-hmac-key", algorithm="HS256")


class _Public:
    def __init__(self, apim: FakeApim, **overrides: Any) -> None:
        self.apim = apim
        self.now = 1000.0
        self.exchanges: list[tuple[str, str]] = []
        self.exchange_error: SignInRequired | None = None
        self.settings = _settings(
            auth_mode="entra",
            entra_client_id=CLIENT,
            subscription_id="",
            resource_group="",
            apim_name="",
            **overrides,
        )
        self.guest_credential = _GuestCredential()
        apim.callers[_GuestCredential.TOKEN] = "app-guest"
        self.sessions = Sessions(
            self.settings,
            self._exchange,
            transport=apim.transport(),
            sleep=lambda _: None,
            clock=lambda: self.now,
            guest_credential=self.guest_credential,
        )
        verifier = TokenVerifier(self.settings, keys=lambda _: _KEY.public_key())
        app.dependency_overrides[get_settings] = lambda: self.settings
        app.dependency_overrides[get_verifier] = lambda: verifier
        app.dependency_overrides[get_sessions] = lambda: self.sessions
        self.client = TestClient(app)

    def _exchange(self, user: EntraUser, scope: str) -> tuple[str, float]:
        if self.exchange_error:
            raise self.exchange_error
        self.exchanges.append((user.key, scope))
        token = _arm_token(user)
        self.apim.callers[token] = user.name.split("@")[0]
        return token, time.time() + 3600

    def headers(
        self, user: str = "alice", *, target: str | None = TARGET, write: bool = False
    ) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {_app_token(user)}"}
        if target:
            headers["X-Pool-Manager-Target"] = target
        if write:
            headers.update(WRITE)
        return headers

    def get(self, path: str, user: str = "alice", **options: Any) -> httpx.Response:
        return self.client.get(path, headers=self.headers(user, **options))


class _GuestCredential:
    """The app's managed identity, as the guest view reads with it."""

    TOKEN = "guest-mi-token"

    def __init__(self) -> None:
        self.calls = 0

    def get_token(self, *_: str, **__: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(token=self.TOKEN, expires_on=time.time() + 3600)


GUEST = {"X-Pool-Manager-Guest": "1"}
GUEST_SERVICE = {
    "guest_subscription_id": "sub",
    "guest_resource_group": "rg",
    "guest_apim_name": "apim",
}


@pytest.fixture
def apim() -> FakeApim:
    return FakeApim()


@pytest.fixture
def public(apim: FakeApim, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Public]:
    def no_state_file(*_: Any, **__: Any) -> None:
        raise AssertionError("the public app must not write a state file")

    monkeypatch.setattr(target_module, "save_target", no_state_file)
    monkeypatch.setattr(target_module, "clear_saved_target", no_state_file)
    yield _Public(apim)
    app.dependency_overrides.clear()


# -- what the browser learns before signing in --------------------------------------------


def test_config_tells_the_browser_how_to_sign_in(public: _Public) -> None:
    response = public.client.get("/api/config")
    assert response.status_code == 200
    assert response.json() == {
        "mode": "public",
        "client_id": CLIENT,
        "authority": f"{AUTHORITY}/organizations",
        "api_scope": f"api://{CLIENT}/access_as_user",
        "consent_scopes": [ARM_USER_SCOPE],
        "guest": False,
    }
    policy = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in policy and f"connect-src 'self' {AUTHORITY}" in policy
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" in response.headers
    # MSAL's silent renewal frames the redirect page, from this origin only.
    bridge = public.client.get("/redirect.html")
    assert "frame-ancestors 'self'" in bridge.headers["Content-Security-Policy"]
    assert bridge.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_config_outside_the_public_app_needs_no_sign_in(apim: FakeApim) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings()
    try:
        response = TestClient(app).get("/api/config")
        assert response.json()["mode"] == "local"
        assert "Content-Security-Policy" not in response.headers
    finally:
        app.dependency_overrides.clear()


# -- signing in ------------------------------------------------------------------------------


def test_every_api_call_needs_a_token(public: _Public) -> None:
    assert public.client.get("/api/me").status_code == 401
    assert public.client.get("/api/setup/progress").status_code == 401
    # Easy Auth's headers mean nothing here: any client could send them.
    forged = {"X-MS-CLIENT-PRINCIPAL-NAME": "admin@contoso.com", "X-MS-CLIENT-PRINCIPAL-ID": "x"}
    assert public.client.get("/api/me", headers=forged).status_code == 401
    me = public.get("/api/me")
    assert me.status_code == 200
    assert me.json() == {
        "name": "alice@contoso.com",
        "can_write": True,
        "authenticated": True,
        "tenant_id": TENANT,
        "guest": False,
    }


@pytest.mark.parametrize(
    ("token", "detail"),
    [
        (_app_token(key=_OTHER_KEY), "Signature verification failed"),
        (_app_token(aud="api://someone-else"), "Audience"),
        (_app_token(exp=int(time.time()) - 3600), "expired"),
        # Signed with a real key but claiming to come from another tenant.
        (_app_token(iss=f"{AUTHORITY}/{OTHER_TENANT}/v2.0"), "issuer"),
        (_app_token(scp=None, roles=["Pools.Manage"]), "access_as_user"),
        (_app_token(oid=None), "oid"),
    ],
)
def test_tokens_are_verified(public: _Public, token: str, detail: str) -> None:
    response = public.client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert detail in response.json()["detail"]
    assert len(public.sessions) == 0


def test_the_api_uri_is_an_accepted_audience(public: _Public) -> None:
    token = _app_token(aud=f"api://{CLIENT}")
    response = public.client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_tenants_can_be_limited(apim: FakeApim) -> None:
    public = _Public(apim, entra_allowed_tenants=OTHER_TENANT)
    try:
        response = public.get("/api/me")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


# -- acting as the user, and keeping users apart ---------------------------------------------


def test_azure_calls_use_the_users_own_token(public: _Public) -> None:
    public.apim.add_account("aoai-east", deployments=[("gpt", "gpt-4o", "OpenAI")])
    response = public.get("/api/deployments")
    assert response.status_code == 200, response.text
    assert [item["deployment"] for item in response.json()] == ["gpt"]
    assert public.exchanges == [(f"{TENANT}/oid-alice", ARM_USER_SCOPE)]
    bearers = {request.headers.get("Authorization") for request in public.apim.requests}
    assert bearers == {f"Bearer {_arm_token(_user('alice'))}"}


def test_users_never_share_a_session_or_its_caches(public: _Public) -> None:
    apim = public.apim
    apim.add_account("aoai-east", deployments=[("gpt", "gpt-4o", "OpenAI")])
    assert len(public.get("/api/deployments", "alice").json()) == 1
    # Alice's list stays cached for a few minutes; Bob's session has its own cache.
    apim.add_account("aoai-west", location="westus3", deployments=[("gpt", "gpt-4o", "OpenAI")])
    assert len(public.get("/api/deployments", "alice").json()) == 1
    assert len(public.get("/api/deployments", "bob").json()) == 2
    assert len(public.sessions) == 2
    # Bob's ARM calls carried Bob's token, never Alice's.
    bob = f"Bearer {_arm_token(_user('bob'))}"
    assert any(request.headers.get("Authorization") == bob for request in apim.requests)


def test_each_service_a_user_opens_has_its_own_session(public: _Public) -> None:
    public.get("/api/me")
    public.get("/api/me", target=None)
    assert len(public.sessions) == 2
    # One exchange per user, shared by their sessions.
    public.get("/api/capabilities")
    public.get("/api/target", target=None)
    assert len(public.exchanges) == 1


def test_the_service_comes_from_the_browser(public: _Public) -> None:
    response = public.get("/api/capabilities", target=None)
    assert response.status_code == 409
    assert public.get("/api/capabilities", target="sub/rg/bad name").status_code == 400
    assert public.get("/api/capabilities", target="sub/rg").status_code == 400
    assert public.get("/api/capabilities").json()["apim_name"] == "apim"
    target = public.get("/api/target").json()
    assert (target["apim_name"], target["source"], target["can_change"]) == (
        "apim",
        "browser",
        True,
    )


def test_choosing_a_service_saves_nothing_on_the_server(public: _Public) -> None:
    public.apim.add_service("apim-dev")
    body = {"subscription_id": SUBSCRIPTION, "resource_group": "rg-apim", "apim_name": "apim-dev"}
    response = public.client.put(
        "/api/target", json=body, headers=public.headers(target=None, write=True)
    )
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "browser"
    assert response.json()["tenant_id"] == TENANT


def test_the_setup_report_runs_as_the_user(public: _Public) -> None:
    report = public.get("/api/setup").json()
    assert report["mode"] == "public"
    assert report["local_sign_in"] is None
    sign_in = next(check for check in report["checks"] if check["kind"] == "sign_in")
    assert sign_in["status"] == "ok"
    assert not any(check["kind"] in {"app_apim", "azure_token"} for check in report["checks"])


def test_missing_consent_asks_for_it(public: _Public) -> None:
    public.exchange_error = SignInRequired("AADSTS65001: consent", code="ConsentRequired")
    report = public.get("/api/setup").json()
    sign_in = report["checks"][0]
    assert (sign_in["kind"], sign_in["status"], sign_in["fix"]) == ("sign_in", "action", "consent")
    assert report["can_operate"] is False


def test_another_tenant_needs_its_own_sign_in(public: _Public) -> None:
    response = public.get(f"/api/target/subscriptions?tenant_id={OTHER_TENANT}", target=None)
    assert response.status_code == 401
    assert f"sign in to {OTHER_TENANT}" in response.json()["detail"]


def test_signing_out_forgets_the_user(public: _Public) -> None:
    public.get("/api/capabilities")
    public.get("/api/me", "bob")
    response = public.client.post("/api/session/sign-out", headers=public.headers())
    assert response.status_code == 403  # The CSRF header is required.
    response = public.client.post("/api/session/sign-out", headers=public.headers(write=True))
    assert response.status_code == 204
    assert len(public.sessions) == 1  # Bob is still signed in.
    public.get("/api/capabilities")
    assert [key for key, _ in public.exchanges].count(f"{TENANT}/oid-alice") == 2


def test_idle_sessions_end(public: _Public) -> None:
    public.get("/api/capabilities")
    public.now += public.settings.session_idle_seconds + 1
    public.get("/api/capabilities", "bob")
    assert len(public.sessions) == 1
    public.get("/api/capabilities")
    assert len(public.exchanges) == 3


def test_requests_per_user_are_limited(apim: FakeApim) -> None:
    public = _Public(apim, user_requests_per_minute=3)
    try:
        assert [public.get("/api/me").status_code for _ in range(4)] == [200, 200, 200, 429]
        assert public.get("/api/me", "bob").status_code == 200
        public.now += 61
        assert public.get("/api/me").status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_the_session_store_is_bounded(apim: FakeApim) -> None:
    public = _Public(apim, max_sessions=2)
    try:
        for user in ("alice", "bob", "carol"):
            public.get("/api/me", user)
        assert len(public.sessions) == 2
    finally:
        app.dependency_overrides.clear()


# -- the pieces on their own -------------------------------------------------------------------


def _user(name: str = "alice", *, expires_on: float | None = None) -> EntraUser:
    return EntraUser(
        tenant_id=TENANT,
        object_id=f"oid-{name}",
        name=f"{name}@contoso.com",
        token=_app_token(name),
        expires_on=expires_on or time.time() + 3600,
    )


def test_the_credential_belongs_to_one_user() -> None:
    exchanged: list[str] = []

    def exchange(user: EntraUser, scope: str) -> tuple[str, float]:
        exchanged.append(scope)
        return "arm", time.time() + 3600

    credential = UserCredential(_user(), exchange)
    with pytest.raises(ValueError):
        credential.update(_user("bob"))
    assert credential.get_token("https://management.azure.com/.default").token == "arm"
    credential.get_token("https://management.azure.com/.default")
    assert exchanged == [ARM_USER_SCOPE]  # Cached in memory until it nears expiry.
    with pytest.raises(SignInRequired, match="sign in to"):
        credential.get_token("https://management.azure.com/.default", tenant_id=OTHER_TENANT)
    credential.clear()
    expired = UserCredential(_user(expires_on=time.time() - 120), exchange)
    with pytest.raises(SignInRequired, match="expired"):
        expired.get_token("https://management.azure.com/.default")


def test_the_exchange_is_a_plain_obo_request() -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"access_token": "arm-token", "expires_in": 3599})

    settings = Settings(_env_file=None, auth_mode="entra", entra_client_id=CLIENT)  # type: ignore[call-arg]
    exchanger = OboExchanger(settings, lambda: "mi-assertion", httpx.MockTransport(handle))
    user = _user()
    token, expires_on = exchanger.exchange(user, ARM_USER_SCOPE)
    assert token == "arm-token" and expires_on > time.time() + 3000
    request = sent[0]
    assert str(request.url) == f"{AUTHORITY}/{TENANT}/oauth2/v2.0/token"
    form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
    assert form == {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": CLIENT,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": "mi-assertion",
        "assertion": user.token,
        "scope": ARM_USER_SCOPE,
        "requested_token_use": "on_behalf_of",
    }


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            {"error": "invalid_grant", "error_codes": [65001], "error_description": "AADSTS65001"},
            "ConsentRequired",
        ),
        ({"error": "invalid_grant", "suberror": "consent_required"}, "ConsentRequired"),
        ({"error": "invalid_grant", "error_codes": [50013]}, "SignInRequired"),
    ],
)
def test_exchange_errors_say_what_to_do(body: dict[str, Any], code: str) -> None:
    settings = Settings(_env_file=None, auth_mode="entra", entra_client_id=CLIENT)  # type: ignore[call-arg]
    transport = httpx.MockTransport(lambda _: httpx.Response(400, json=body))
    exchanger = OboExchanger(settings, lambda: "mi-assertion", transport)
    with pytest.raises(SignInRequired) as raised:
        exchanger.exchange(_user(), ARM_USER_SCOPE)
    assert raised.value.code == code


def test_the_public_app_only_calls_azure_gateways(apim: FakeApim) -> None:
    from tests.fake_apim import FakeCredential

    settings = _settings(auth_mode="entra", entra_client_id=CLIENT)
    service = PoolManagerService(
        settings, ArmClient(settings, FakeCredential(), apim.transport(), sleep=lambda _: None)
    )
    gateway = httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True}))
    tester = ApiTester(settings, service, gateway)
    bound = SimpleNamespace(path="chat", subscription_key_header="Ocp-Apim-Subscription-Key")
    tester._bound_api = lambda _: bound  # type: ignore[method-assign,assignment,return-value]
    request = ApiTestRequest(path="chat/completions", body={"messages": []})
    assert tester.call("chat", request, "alice").status == 200
    for url in ("https://api.contoso.com", "https://apim.azure-api.net:8443"):
        apim.gateway_url = url
        service.forget_service()
        with pytest.raises(ServiceError, match="gateway host names"):
            tester.call("chat", request, "alice")


def test_target_header_parts_are_decoded() -> None:
    assert parse_target("sub/r%C3%A9sum%C3%A9(1)/apim") == ("sub", "résumé(1)", "apim")
    assert parse_target(None) is None
    with pytest.raises(ServiceError):
        parse_target("sub/rg/apim/extra")


def test_runtime_rules_for_the_public_app(monkeypatch: pytest.MonkeyPatch) -> None:
    def settings(**values: Any) -> Settings:
        return Settings(_env_file=None, **values)  # type: ignore[call-arg]

    with pytest.raises(RuntimeError, match="ENTRA_CLIENT_ID"):
        settings(auth_mode="entra").validate_runtime()
    fixed = {"subscription_id": "s", "resource_group": "g", "apim_name": "a"}
    with pytest.raises(RuntimeError, match="SUBSCRIPTION_ID"):
        settings(auth_mode="entra", entra_client_id=CLIENT, **fixed).validate_runtime()
    with pytest.raises(RuntimeError, match="DISCOVERY_SUBSCRIPTIONS"):
        settings(
            auth_mode="entra", entra_client_id=CLIENT, discovery_subscriptions="s"
        ).validate_runtime()
    settings(auth_mode="entra", entra_client_id=CLIENT).validate_runtime()
    # Never an unauthenticated app on Container Apps.
    monkeypatch.setenv("CONTAINER_APP_NAME", "pool-manager")
    with pytest.raises(RuntimeError, match="not allowed"):
        settings(auth_mode="disabled").validate_runtime()


# -- the read-only guest view --------------------------------------------------------------


@pytest.fixture
def guest(apim: FakeApim, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Public]:
    def no_state_file(*_: Any, **__: Any) -> None:
        raise AssertionError("the public app must not write a state file")

    monkeypatch.setattr(target_module, "save_target", no_state_file)
    yield _Public(apim, **GUEST_SERVICE)
    app.dependency_overrides.clear()


def _as_guest(public: _Public, method: str, path: str, **options: Any) -> httpx.Response:
    headers = {**GUEST, **options.pop("headers", {})}
    return public.client.request(method, path, headers=headers, **options)


def test_the_config_offers_the_guest_view(guest: _Public) -> None:
    assert guest.client.get("/api/config").json()["guest"] is True


def test_guests_browse_the_configured_service_read_only(guest: _Public) -> None:
    guest.apim.add_account("aoai-east", deployments=[("gpt", "gpt-4o", "OpenAI")])
    me = _as_guest(guest, "GET", "/api/me").json()
    assert me == {
        "name": "guest",
        "can_write": False,
        "authenticated": False,
        "tenant_id": None,
        "guest": True,
    }
    assert _as_guest(guest, "GET", "/api/capabilities").json()["apim_name"] == "apim"
    assert _as_guest(guest, "GET", "/api/pools").status_code == 200
    assert len(_as_guest(guest, "GET", "/api/deployments").json()) == 1
    report = _as_guest(guest, "GET", "/api/setup").json()
    assert (report["mode"], report["ready"], report["can_operate"]) == ("guest", True, True)
    assert [check["kind"] for check in report["checks"]] == ["apim_target"]
    target = _as_guest(guest, "GET", "/api/target").json()
    assert (target["apim_name"], target["source"], target["can_change"]) == ("apim", "guest", False)
    # Every Azure call ran as the app's managed identity, never with a user token.
    bearers = {request.headers.get("Authorization") for request in guest.apim.requests}
    assert bearers == {f"Bearer {_GuestCredential.TOKEN}"}
    assert guest.exchanges == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/api/pools/p1", {"members": []}),
        ("DELETE", "/api/pools/p1", None),
        ("POST", "/api/deployments/import", {"deployments": []}),
        ("GET", "/api/apis/chat/keys", None),
        ("POST", "/api/apis/chat/keys", None),
        ("POST", "/api/apis/chat/keys/k1/secret", None),
        ("POST", "/api/apis/chat/test", {"path": "chat/completions", "body": {}}),
        ("POST", "/api/setup/apim-identity", None),
        ("GET", "/api/target/tenants", None),
        (
            "PUT",
            "/api/target",
            {"subscription_id": SUBSCRIPTION, "resource_group": "rg", "apim_name": "apim"},
        ),
        (
            "POST",
            "/api/cleanup",
            {"pools": [], "delete_apis": [], "unbind_apis": [], "confirm_name": "apim"},
        ),
        ("GET", "/api/foundry/models?account_id=x", None),
        ("POST", "/api/session/sign-out", None),
    ],
)
def test_guests_can_change_or_reveal_nothing(
    guest: _Public, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = _as_guest(guest, method, path, headers=WRITE, json=body)
    assert response.status_code == 403, response.text
    assert "read-only" in response.json()["detail"] or "guest view" in response.json()["detail"]
    assert all(request.method == "GET" for request in guest.apim.requests)


def test_guests_cant_choose_another_service_or_bring_a_token(guest: _Public) -> None:
    guest.apim.add_service("apim-other")
    headers = {"X-Pool-Manager-Target": f"{SUBSCRIPTION}/rg-apim/apim-other"}
    assert (
        _as_guest(guest, "GET", "/api/capabilities", headers=headers).json()["apim_name"] == "apim"
    )
    # Easy Auth's token header means nothing here.
    forged = {"X-MS-TOKEN-AAD-ACCESS-TOKEN": _arm_token(_user("mallory"))}
    cost = _as_guest(guest, "GET", "/api/cost", headers=forged).json()
    assert cost["month_to_date"] is None
    bearers = {request.headers.get("Authorization") for request in guest.apim.requests}
    assert bearers <= {f"Bearer {_GuestCredential.TOKEN}", None}


def test_a_signed_in_user_is_never_a_guest(guest: _Public) -> None:
    headers = {**GUEST, **guest.headers()}
    me = guest.client.get("/api/me", headers=headers).json()
    assert (me["name"], me["guest"], me["can_write"]) == ("alice@contoso.com", False, True)


def test_guests_share_one_session_and_are_rate_limited(apim: FakeApim) -> None:
    public = _Public(apim, **GUEST_SERVICE, guest_requests_per_minute=2)
    try:
        assert [_as_guest(public, "GET", "/api/me").status_code for _ in range(3)] == [
            200,
            200,
            429,
        ]
        assert len(public.sessions) == 1
    finally:
        app.dependency_overrides.clear()


def test_without_a_guest_service_there_is_no_guest_view(public: _Public) -> None:
    assert _as_guest(public, "GET", "/api/me").status_code == 404
    assert public.client.get("/api/config").json()["guest"] is False


def test_the_guest_service_is_all_or_nothing() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, auth_mode="entra", entra_client_id=CLIENT, guest_apim_name="apim"
    )
    with pytest.raises(RuntimeError, match="GUEST_SUBSCRIPTION_ID"):
        settings.validate_runtime()
