"""scripts/public-app-registration.py: the public app's azd hooks, against an in-memory Graph."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "public-app-registration.py"
PRINCIPAL = "11111111-2222-3333-4444-555555555555"
TENANT = "00000000-0000-0000-0000-00000000aaaa"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("public_app_registration", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Graph:
    """Applications, service principals and federated credentials, as Graph would keep them."""

    def __init__(self) -> None:
        self.apps: dict[str, dict[str, Any]] = {}
        self.principals: list[str] = []
        self.credentials: dict[str, list[dict[str, Any]]] = {}
        self.env: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path))
        path = unquote(path)
        if path.startswith("/applications?$filter="):
            app_id = path.split("'")[1]
            return {"value": [app for app in self.apps.values() if app["appId"] == app_id]}
        if path == "/applications" and method == "POST":
            assert body is not None
            number = len(self.apps) + 1
            app = {"id": f"object-{number}", "appId": f"app-{number}", **body}
            self.apps[app["id"]] = app
            return app
        if path.startswith("/servicePrincipals?$filter="):
            app_id = path.split("'")[1]
            return {"value": [{"appId": app_id}] if app_id in self.principals else []}
        if path == "/servicePrincipals":
            assert body is not None
            self.principals.append(body["appId"])
            return body
        parts = path.strip("/").split("/")
        if parts[0] == "applications" and len(parts) == 2 and method == "PATCH":
            assert body is not None
            self.apps[parts[1]].update(body)
            return None
        if parts[2:] == ["federatedIdentityCredentials"]:
            items = self.credentials.setdefault(parts[1], [])
            if method == "POST":
                assert body is not None
                items.append(body)
                return body
            return {"value": items}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> _Graph:
    module = _load()
    fake = _Graph()
    monkeypatch.setattr(module, "graph", fake)
    monkeypatch.setattr(module, "azd_env_set", fake.env.__setitem__)
    fake.module = module  # type: ignore[attr-defined]
    return fake


def _run(graph: _Graph, step: str, monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for name, value in {**graph.env, **env}.items():
        monkeypatch.setenv(name, value)
    getattr(graph.module, step)()  # type: ignore[attr-defined]


def test_preprovision_creates_a_multi_tenant_api(
    graph: _Graph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
    _run(graph, "preprovision", monkeypatch)
    app = graph.apps["object-1"]
    assert graph.env == {"ENTRA_CLIENT_ID": "app-1"}
    assert app["signInAudience"] == "AzureADMultipleOrgs"
    assert app["identifierUris"] == ["api://app-1"]
    assert app["api"]["requestedAccessTokenVersion"] == 2
    [scope] = app["api"]["oauth2PermissionScopes"]
    assert (scope["value"], scope["type"], scope["isEnabled"]) == ("access_as_user", "User", True)
    # Azure Service Management as the user, and the app's own scope, so that one admin consent
    # in another tenant covers every token the app asks for.
    assert app["requiredResourceAccess"] == [
        {
            "resourceAppId": "797f4846-ba00-4fd7-ba43-dac1f8f63013",
            "resourceAccess": [{"id": "41094075-9dad-400e-a0bd-54e686782033", "type": "Scope"}],
        },
        {"resourceAppId": "app-1", "resourceAccess": [{"id": scope["id"], "type": "Scope"}]},
    ]
    assert graph.principals == ["app-1"]


def test_preprovision_again_changes_nothing_it_needs(
    graph: _Graph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
    _run(graph, "preprovision", monkeypatch)
    scope_id = graph.apps["object-1"]["api"]["oauth2PermissionScopes"][0]["id"]
    _run(graph, "preprovision", monkeypatch)
    app = graph.apps["object-1"]
    assert len(graph.apps) == 1 and graph.principals == ["app-1"]
    # The scope keeps its ID: Entra refuses to replace an enabled scope.
    assert [scope["id"] for scope in app["api"]["oauth2PermissionScopes"]] == [scope_id]
    assert [len(item["resourceAccess"]) for item in app["requiredResourceAccess"]] == [1, 1]


def test_an_unknown_client_id_stops(graph: _Graph, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="not an app registration"):
        _run(graph, "preprovision", monkeypatch, ENTRA_CLIENT_ID="missing")


def test_postprovision_adds_the_redirect_and_the_federated_credential(
    graph: _Graph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
    _run(graph, "preprovision", monkeypatch)
    env = {
        "PUBLIC_APP_URL": "https://ca-pool.example.azurecontainerapps.io/",
        "PUBLIC_APP_IDENTITY_PRINCIPAL_ID": PRINCIPAL,
        "PUBLIC_APP_IDENTITY_TENANT_ID": TENANT,
        "ENTRA_LOGIN_ENDPOINT": "https://login.microsoftonline.com/",
    }
    for _ in range(2):  # Running it twice adds nothing twice.
        _run(graph, "postprovision", monkeypatch, **env)
    assert graph.apps["object-1"]["spa"]["redirectUris"] == [
        "https://ca-pool.example.azurecontainerapps.io/redirect.html"
    ]
    assert graph.credentials["object-1"] == [
        {
            "name": "pool-public-container-app",
            "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            "subject": PRINCIPAL,
            "audiences": ["api://AzureADTokenExchange"],
            "description": "The public app's managed identity, instead of a client secret",
        }
    ]


def test_postprovision_needs_the_provisioned_outputs(
    graph: _Graph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PUBLIC_APP_URL", raising=False)
    with pytest.raises(SystemExit, match="PUBLIC_APP_URL"):
        _run(graph, "postprovision", monkeypatch, ENTRA_CLIENT_ID="app-1")


def test_the_script_runs_on_old_pythons() -> None:
    import ast

    ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 9))
    assert sys.version_info >= (3, 9)
