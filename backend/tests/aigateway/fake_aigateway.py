"""An in-memory Azure AI Gateway on top of the in-memory Azure (tests/fake_azure.py).

It implements the ARM API the app calls on ``Microsoft.ApiManagement/aigateways`` (the gateway
and its identity; the default workspace's model providers, models, aliases and telemetry
exporters; runtime API keys and their secrets), with the ETag and conditional-write behaviour
the app relies on and secrets that are write-only, plus listing, creating and deleting gateways
and the gateway's runtime surface for test calls. Everything else (permissions, Foundry,
metrics, cost, prices, logs, tenants, ...) is FakeAzure's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import count
from typing import Any

import httpx

from tests.fake_azure import SUBSCRIPTION, TENANT, FakeAzure, FakeCredential, _error

__all__ = ["GATEWAY", "SUBSCRIPTION", "TENANT", "FakeAiGateway", "FakeCredential"]

_PROVIDER = "/providers/Microsoft.ApiManagement"
GATEWAY = f"/subscriptions/sub/resourceGroups/rg{_PROVIDER}/aigateways/aigw"
GATEWAY_PRINCIPAL = "aigw-mi"
HOST = "aigw.eastus2-01.ai.gateway.azure.com"
APP_INSIGHTS = (
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Insights/components/aigw-ai"
)
WORKSPACE = (
    "/subscriptions/sub/resourceGroups/rg/providers"
    "/Microsoft.OperationalInsights/workspaces/aigw-logs"
)
FOUNDRY_USER = (
    "/subscriptions/sub/providers/Microsoft.Authorization/roleDefinitions/"
    "53ca6127-db72-4b80-b1b0-d745d6d5456d"
)
_WS = "/workspaces/default"


@dataclass
class FakeAiGateway(FakeAzure):
    gateway_id: str = GATEWAY
    principal: str | None = GATEWAY_PRINCIPAL
    provisioning: str = "Succeeded"
    host: str | None = HOST
    # Children by lower-case ARM ID (relative to nothing: full IDs), with their ETags.
    children: dict[str, dict[str, Any]] = field(default_factory=dict)
    etags: dict[str, str] = field(default_factory=dict)
    # Secrets the gateway keeps but never returns: provider headers by provider ID, key values.
    header_secrets: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    # Other gateways in the subscription (lower-case ID -> body), for the setup wizard.
    gateways: dict[str, dict[str, Any]] = field(default_factory=dict)
    taken_names: set[str] = field(default_factory=set)
    # The gateway's runtime: requests to it, and what each alias answers (status, headers).
    runtime_requests: list[httpx.Request] = field(default_factory=list)
    runtime_answer: tuple[int, dict[str, str]] = (200, {"x-ms-served-model": "gpt-6-luna"})
    _versions: count = field(default_factory=lambda: count(1))  # type: ignore[type-arg]

    def __post_init__(self) -> None:
        super().__post_init__()
        self.role_definitions[FOUNDRY_USER.casefold()] = {
            "id": FOUNDRY_USER,
            "properties": {
                "roleName": "Foundry User",
                "type": "BuiltInRole",
                "permissions": [
                    {
                        "actions": ["Microsoft.CognitiveServices/*/read"],
                        "notActions": [],
                        "dataActions": ["Microsoft.CognitiveServices/*"],
                        "notDataActions": [],
                    }
                ],
            },
        }
        self.offered.setdefault(
            "Microsoft.ApiManagement/aigateways", ["East US 2", "Sweden Central", "East US 2 EUAP"]
        )

    # -- seeding ------------------------------------------------------------------------

    def _put(self, resource_id: str, body: dict[str, Any]) -> dict[str, Any]:
        name = resource_id.rpartition("/")[2]
        item = {"id": resource_id, "name": name, **body}
        self.children[resource_id.casefold()] = item
        self.etags[resource_id.casefold()] = f'"{next(self._versions)}"'
        return item

    def add_provider(self, name: str, properties: dict[str, Any]) -> str:
        resource_id = f"{self.gateway_id}{_WS}/modelProviders/{name}"
        self._put(resource_id, {"properties": properties})
        return resource_id

    def add_model(self, provider: str, name: str, properties: dict[str, Any]) -> str:
        resource_id = f"{self.gateway_id}{_WS}/modelProviders/{provider}/models/{name}"
        self._put(resource_id, {"properties": properties})
        return resource_id

    def add_alias(self, name: str, properties: dict[str, Any]) -> str:
        resource_id = f"{self.gateway_id}{_WS}/aliases/{name}"
        self._put(resource_id, {"properties": properties})
        return resource_id

    def add_exporter(self, component: str = APP_INSIGHTS) -> None:
        self._put(
            f"{self.gateway_id}{_WS}/telemetryExporters/insights",
            {
                "properties": {
                    "telemetryExporterType": "ApplicationInsights",
                    "applicationInsights": {"resourceId": component},
                }
            },
        )

    def add_aigateway(
        self, name: str, *, group: str = "rg-aigw", managed: bool = True, **extra: Any
    ) -> str:
        gateway_id = (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{group}{_PROVIDER}/aigateways/{name}"
        )
        self.gateways[gateway_id.casefold()] = {
            "id": gateway_id,
            "name": name,
            "location": "eastus2",
            "sku": {"name": "Standard"},
            "tags": {"managed-by": "steadyapipool"} if managed else {},
            "identity": {"type": "SystemAssigned", "principalId": f"{name}-mi"},
            "properties": {
                "provisioningState": "Succeeded",
                "frontend": {"defaultHostname": f"{name}.eastus2-01.ai.gateway.azure.com"},
            },
            **extra,
        }
        return gateway_id

    def children_of(self, collection: str) -> list[dict[str, Any]]:
        """The items of a collection, e.g. ``/workspaces/default/aliases``."""
        prefix = f"{self.gateway_id}{collection}/".casefold()
        return [
            item
            for key, item in self.children.items()
            if key.startswith(prefix) and "/" not in key[len(prefix) :]
        ]

    def body(self, suffix: str) -> dict[str, Any]:
        return self.children[f"{self.gateway_id}{suffix}".casefold()]

    # -- routing -----------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.host and request.url.host == self.host:
            self.runtime_requests.append(request)
            status, headers = self.runtime_answer
            return httpx.Response(
                status,
                json={"choices": [{"message": {"content": "pong"}}]},
                headers={"x-ms-region": "East US 2", "set-cookie": "a=b", **headers},
            )
        return super()._handle(request)

    def gateway_resource(
        self, request: httpx.Request, caller: str, path: str, body: Any
    ) -> httpx.Response | None:
        if path.casefold() == APP_INSIGHTS.casefold():
            return httpx.Response(
                200, json={"id": APP_INSIGHTS, "properties": {"WorkspaceResourceId": WORKSPACE}}
            )
        if path.casefold() == self.gateway_id.casefold():
            return self._gateway(request, caller, body)
        if path.casefold().startswith(f"{self.gateway_id}/".casefold()):
            return self._child(request, caller, path, body)
        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "subscriptions":
            return None
        provider = f"/subscriptions/{parts[1]}{_PROVIDER}"
        if path == f"{provider}/aigateways":
            prefix = f"/subscriptions/{parts[1]}/".casefold()
            items = [item for key, item in self.gateways.items() if key.startswith(prefix)]
            return httpx.Response(200, json={"value": items})
        if path.startswith(f"{provider}/locations/") and path.endswith("/checkNameAvailability"):
            names = self.taken_names | {item["name"] for item in self.gateways.values()}
            taken = body["name"].casefold() in {name.casefold() for name in names}
            return httpx.Response(
                200,
                json={"nameAvailable": not taken, "message": "taken" if taken else ""},
            )
        if f"{_PROVIDER}/aigateways/" in path and len(parts) == 8:
            return self._other_gateway(request, caller, path, body)
        if request.method == "GET" and any(
            path.casefold().startswith(f"{key}/") for key in self.gateways
        ):
            # Other gateways' collections (what deleting one would take): empty.
            return httpx.Response(200, json={"value": []})
        return None

    def _gateway(self, request: httpx.Request, caller: str, body: Any) -> httpx.Response:
        if request.method == "PATCH":
            if not self._may(caller, self.gateway_id, "Microsoft.ApiManagement/aigateways/write"):
                return _error(403, "AuthorizationFailed")
            assert "SystemAssigned" in body["identity"]["type"]
            self.principal = "aigw-mi-new"
            return httpx.Response(200, json={})
        if not self._may(caller, self.gateway_id, "Microsoft.ApiManagement/aigateways/read"):
            return _error(403, "AuthorizationFailed")
        identity = (
            {"type": "SystemAssigned", "principalId": self.principal}
            if self.principal
            else {"type": "None"}
        )
        properties: dict[str, Any] = {"provisioningState": self.provisioning}
        if self.host:
            properties["frontend"] = {"defaultHostname": self.host}
        return httpx.Response(
            200,
            json={
                "id": self.gateway_id,
                "name": self.gateway_id.rpartition("/")[2],
                "type": "Microsoft.ApiManagement/aigateways",
                "location": "eastus2",
                "sku": {"name": "Standard"},
                "identity": identity,
                "properties": properties,
                "systemData": {"createdAt": "2026-11-20T10:00:00Z"},
            },
            headers={"ETag": '"gw"'},
        )

    def _action(self, relative: str) -> str:
        """The RBAC action a request on a child needs, e.g. .../workspaces/aliases/write."""
        kinds = [
            part for index, part in enumerate(relative.strip("/").split("/")) if index % 2 == 0
        ]
        return "Microsoft.ApiManagement/aigateways/" + "/".join(kinds)

    def _child(self, request: httpx.Request, caller: str, path: str, body: Any) -> httpx.Response:
        relative = path[len(self.gateway_id) :]
        parts = relative.strip("/").split("/")
        if parts[-1] == "listSecrets":
            key_id = f"{self.gateway_id}/{'/'.join(parts[:-1])}".casefold()
            action = f"{self._action('/'.join(parts[:-1]))}/listSecrets/action"
            if not self._may(caller, self.gateway_id, action):
                return _error(403, "AuthorizationFailed")
            if key_id not in self.children:
                return _error(404, "ResourceNotFound")
            name = parts[-2]
            return httpx.Response(
                200, json={"primaryKey": f"primary-{name}", "secondaryKey": f"secondary-{name}"}
            )
        collection = len(parts) % 2 == 1
        verb = {"GET": "read", "PUT": "write", "PATCH": "write", "DELETE": "delete"}[request.method]
        action = f"{self._action(relative)}/{verb}"
        if not self._may(caller, self.gateway_id, action):
            return _error(403, "AuthorizationFailed")
        if collection:
            if parts == ["workspaces", "default", "models"]:
                prefix = f"{self.gateway_id}{_WS}/modelProviders/".casefold()
                items = [
                    item
                    for key, item in self.children.items()
                    if key.startswith(prefix) and "/models/" in key[len(prefix) :]
                ]
            else:
                items = self.children_of(relative.rstrip("/"))
            return httpx.Response(200, json={"value": [self._public(item) for item in items]})
        key = path.casefold()
        exists = key in self.children
        if request.method == "GET":
            if not exists:
                return _error(404, "ResourceNotFound")
            return httpx.Response(
                200, json=self._public(self.children[key]), headers={"ETag": self.etags[key]}
            )
        if_match = request.headers.get("If-Match")
        if request.headers.get("If-None-Match") == "*" and exists:
            return _error(412, "PreconditionFailed")
        if if_match and if_match != "*" and (not exists or self.etags.get(key) != if_match):
            return _error(412, "PreconditionFailed")
        if request.method == "DELETE":
            if not exists:
                return httpx.Response(204)
            if parts[-2] == "modelProviders" and any(
                other.startswith(f"{key}/models/") for other in self.children
            ):
                return _error(409, "ProviderHasModels")
            if parts[-2] == "models" and any(
                (item.get("properties") or {}).get("backendModels")
                and any(
                    str(t.get("resourceId") or "").casefold() == key
                    for t in item["properties"]["backendModels"]
                )
                for item in self.children_of(f"{_WS}/aliases")
            ):
                return _error(409, "ModelInUse")
            del self.children[key]
            self.header_secrets.pop(key, None)
            return httpx.Response(200)
        properties = dict((body or {}).get("properties") or {})
        if parts[-2] == "aliases":
            for target in properties.get("backendModels") or []:
                if str(target["resourceId"]).casefold() not in self.children:
                    return _error(400, "BackendModelNotFound")
                assert 1 <= target.get("weight", 1) <= 100
                assert 0 <= target.get("priority", 0) <= 100
        if parts[-2] == "modelProviders":
            properties = self._keep_secrets(key, properties, patch=request.method == "PATCH")
            if request.method == "PATCH" and exists:
                properties = {**self.children[key]["properties"], **properties}
        elif request.method == "PATCH" and exists:
            properties = {**self.children[key]["properties"], **properties}
        if parts[-2] == "models":
            provider = key.rpartition("/models/")[0]
            if provider not in self.children:
                return _error(404, "ModelProviderNotFound")
            kind = self.children[provider]["properties"].get("modelProviderType")
            properties["providerKind"] = kind
        item = self._put(path, {"properties": {**properties, "provisioningState": "Succeeded"}})
        return httpx.Response(
            200 if exists else 201, json=self._public(item), headers={"ETag": self.etags[key]}
        )

    def _keep_secrets(self, key: str, properties: dict[str, Any], *, patch: bool) -> dict[str, Any]:
        """Header values are write-only: kept here, never returned."""
        headers = (properties.get("authentication") or {}).get("headers")
        if headers is None:
            return properties
        if any("value" not in header for header in headers) and not patch:
            kept = {h["name"]: h for h in self.header_secrets.get(key, [])}
            headers = [kept.get(h["name"], h) for h in headers]
        self.header_secrets[key] = [dict(header) for header in headers]
        return {
            **properties,
            "authentication": {
                **properties["authentication"],
                "headers": [{"name": header["name"]} for header in headers],
            },
        }

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(item))

    def _other_gateway(
        self, request: httpx.Request, caller: str, path: str, body: Any
    ) -> httpx.Response:
        key = path.casefold()
        if request.method == "GET":
            found = self.gateways.get(key)
            return httpx.Response(200, json=found) if found else _error(404, "ResourceNotFound")
        if request.method == "DELETE":
            if not self._may(caller, path, "Microsoft.ApiManagement/aigateways/delete"):
                return _error(403, "AuthorizationFailed")
            self.gateways.pop(key, None)
            return httpx.Response(202)
        if request.method != "PUT":
            return _error(405, "MethodNotAllowed")
        if request.headers.get("If-None-Match") == "*" and key in self.gateways:
            return _error(412, "PreconditionFailed")
        if not self._may(caller, path, "Microsoft.ApiManagement/aigateways/write"):
            return _error(403, "AuthorizationFailed")
        registration = self.providers.get(path.split("/")[2], {})
        if registration.get("Microsoft.ApiManagement", "Registered") != "Registered":
            return _error(409, "MissingSubscriptionRegistration")
        if path.rpartition(_PROVIDER)[0].casefold() not in self.resource_groups:
            return _error(404, "ResourceGroupNotFound")
        name = path.rpartition("/")[2]
        self.gateways[key] = {
            "id": path,
            "name": name,
            **body,
            "identity": {"type": "SystemAssigned", "principalId": f"{name}-mi"},
            "properties": {**body["properties"], "provisioningState": "Creating"},
            "systemData": {"createdAt": "2026-11-20T10:00:00Z"},
        }
        return httpx.Response(201, json=self.gateways[key])
