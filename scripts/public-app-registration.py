"""azd hooks for the public app (deploy/container-apps): its multi-tenant Entra app registration.

preprovision
    Creates the registration the first time (any work or school tenant may sign in) and keeps
    it as the app needs it: the ``access_as_user`` scope on ``api://<client ID>`` with v2 tokens,
    and the delegated Azure Service Management permission (``user_impersonation``) that the
    backend exchanges sign-ins for. Sets ``ENTRA_CLIENT_ID`` in the azd environment.

postprovision
    Adds the container app's ``/redirect.html`` as a single-page-app redirect URI, and the
    federated identity credential that lets the app's user-assigned managed identity stand in
    for a client secret, so the registration has no secret at all.

Both steps only add what is missing and never remove anything. They use the Azure CLI (``az``)
signed in to the tenant that owns the registration; Python's standard library is enough.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import Any
from urllib.parse import quote

DISPLAY_NAME = "APIM Backend Pool Manager (public)"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "access_as_user"
# Azure Service Management and its user_impersonation scope (the same IDs in every tenant).
ARM_APP_ID = "797f4846-ba00-4fd7-ba43-dac1f8f63013"
ARM_USER_IMPERSONATION = "41094075-9dad-400e-a0bd-54e686782033"
FIC_NAME = "pool-public-container-app"
FIC_AUDIENCE = "api://AzureADTokenExchange"


def az(*args: str) -> Any:
    executable = shutil.which("az")
    if not executable:
        sys.exit(
            "The Azure CLI (az) is needed: https://learn.microsoft.com/cli/azure/install-azure-cli"
        )
    done = subprocess.run(
        [executable, *args, "--output", "json"], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        sys.exit(f"az {' '.join(args[:3])} failed:\n{done.stderr.strip()}")
    return json.loads(done.stdout) if done.stdout.strip() else None


def graph(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    args = ["rest", "--method", method, "--uri", f"{GRAPH}{path}"]
    if body is None:
        return az(*args)
    # A file, so no shell has to quote the JSON.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as file:
        json.dump(body, file)
    try:
        return az(*args, "--headers", "Content-Type=application/json", "--body", f"@{file.name}")
    finally:
        os.unlink(file.name)


def azd_env_set(name: str, value: str) -> None:
    executable = shutil.which("azd")
    if not executable:
        sys.exit("azd is needed to run this hook")
    subprocess.run([executable, "env", "set", name, value], check=True)


def by_app_id(client_id: str) -> str:
    return quote(f"appId eq '{client_id}'")


def find_app(client_id: str) -> dict[str, Any] | None:
    found = graph("GET", f"/applications?$filter={by_app_id(client_id)}")
    items = (found or {}).get("value") or []
    return items[0] if items else None


def preprovision() -> None:
    client_id = os.environ.get("ENTRA_CLIENT_ID", "").strip()
    app = find_app(client_id) if client_id else None
    if app is None:
        if client_id:
            sys.exit(f"ENTRA_CLIENT_ID {client_id} is not an app registration in this tenant")
        app = graph(
            "POST",
            "/applications",
            {"displayName": DISPLAY_NAME, "signInAudience": "AzureADMultipleOrgs"},
        )
        print(f"Created the app registration {DISPLAY_NAME} ({app['appId']})")
    client_id = app["appId"]
    api = app.get("api") or {}
    scopes = list(api.get("oauth2PermissionScopes") or [])
    if not any(scope.get("value") == SCOPE for scope in scopes):
        scopes.append(
            {
                "id": str(uuid.uuid4()),
                "value": SCOPE,
                "type": "User",
                "isEnabled": True,
                "adminConsentDisplayName": "Use APIM Backend Pool Manager as you",
                "adminConsentDescription": (
                    "Lets the app manage API Management backend pools and Foundry deployments "
                    "with the signed-in user's own Azure rights."
                ),
                "userConsentDisplayName": "Use APIM Backend Pool Manager as you",
                "userConsentDescription": (
                    "Lets the app manage API Management backend pools and Foundry deployments "
                    "with your own Azure rights."
                ),
            }
        )
    identifier = f"api://{client_id}"
    graph(
        "PATCH",
        f"/applications/{app['id']}",
        {
            "signInAudience": "AzureADMultipleOrgs",
            "identifierUris": sorted({*app.get("identifierUris", []), identifier}),
            # v2 tokens: the backend checks their issuer against the token's own tenant.
            "api": {**api, "requestedAccessTokenVersion": 2, "oauth2PermissionScopes": scopes},
        },
    )
    # The permissions a tenant consents to: Azure Service Management as the user, and this app's
    # own access_as_user, so that one admin consent in a customer tenant (adminconsent URL)
    # installs the app and covers every token it asks for. Set once the scope exists.
    own_scope = next(scope["id"] for scope in scopes if scope.get("value") == SCOPE)
    current = app.get("requiredResourceAccess") or []
    wanted = {ARM_APP_ID: ARM_USER_IMPERSONATION, client_id: own_scope}
    required = [item for item in current if item.get("resourceAppId") not in wanted]
    for resource, scope_id in wanted.items():
        entry = next(
            (item for item in current if item.get("resourceAppId") == resource),
            {"resourceAppId": resource, "resourceAccess": []},
        )
        if not any(access.get("id") == scope_id for access in entry["resourceAccess"]):
            entry["resourceAccess"].append({"id": scope_id, "type": "Scope"})
        required.append(entry)
    graph("PATCH", f"/applications/{app['id']}", {"requiredResourceAccess": required})
    principals = graph("GET", f"/servicePrincipals?$filter={by_app_id(client_id)}")
    if not (principals or {}).get("value"):
        graph("POST", "/servicePrincipals", {"appId": client_id})
    azd_env_set("ENTRA_CLIENT_ID", client_id)
    print(f"App registration ready: {identifier}/{SCOPE}")


def postprovision() -> None:
    def required(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            sys.exit(f"{name} is missing from the azd environment; run azd provision")
        return value

    client_id = required("ENTRA_CLIENT_ID")
    url = required("PUBLIC_APP_URL").rstrip("/")
    principal = required("PUBLIC_APP_IDENTITY_PRINCIPAL_ID")
    tenant = required("PUBLIC_APP_IDENTITY_TENANT_ID")
    login = os.environ.get("ENTRA_LOGIN_ENDPOINT", "https://login.microsoftonline.com/").rstrip("/")
    app = find_app(client_id)
    if app is None:
        sys.exit(f"App registration {client_id} not found")
    redirect = f"{url}/redirect.html"
    redirects = list((app.get("spa") or {}).get("redirectUris") or [])
    if redirect not in redirects:
        graph(
            "PATCH", f"/applications/{app['id']}", {"spa": {"redirectUris": [*redirects, redirect]}}
        )
        print(f"Added the redirect URI {redirect}")
    credentials = graph("GET", f"/applications/{app['id']}/federatedIdentityCredentials")
    existing = (credentials or {}).get("value") or []
    if not any(item.get("subject") == principal for item in existing):
        names = {item.get("name") for item in existing}
        name = FIC_NAME if FIC_NAME not in names else f"{FIC_NAME}-{principal[:8]}"
        graph(
            "POST",
            f"/applications/{app['id']}/federatedIdentityCredentials",
            {
                "name": name,
                "issuer": f"{login}/{tenant}/v2.0",
                "subject": principal,
                "audiences": [FIC_AUDIENCE],
                "description": "The public app's managed identity, instead of a client secret",
            },
        )
        print("Added the federated credential for the app's managed identity")
    print(f"\nThe public app is at {url}")
    print(
        "Before inviting other organizations, verify the publisher of the app registration "
        "(Microsoft Entra > App registrations > Branding & properties); many tenants only allow "
        "consent to verified apps."
    )


if __name__ == "__main__":
    steps = {"preprovision": preprovision, "postprovision": postprovision}
    if len(sys.argv) != 2 or sys.argv[1] not in steps:
        sys.exit(f"usage: {sys.argv[0]} {{{'|'.join(steps)}}}")
    steps[sys.argv[1]]()
