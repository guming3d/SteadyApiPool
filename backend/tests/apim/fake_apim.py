"""An in-memory APIM service on top of the in-memory Azure (tests/fake_azure.py).

It implements the APIM management API the app calls (backends, named values, APIs, operations,
policies, subscriptions, the service and its identity), with the ETag and conditional-write
behaviour the app relies on, plus choosing, creating, deleting and restoring services. Everything
else (permissions, Foundry, metrics, cost, prices, tenants, ...) is FakeAzure's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import count
from typing import Any

import httpx

from tests.fake_azure import (
    RETAIL_PRICES as AZURE_RETAIL_PRICES,
)
from tests.fake_azure import (
    SUBSCRIPTION,
    TENANT,
    FakeAzure,
    FakeCredential,
    _error,
)

__all__ = ["RETAIL_PRICES", "SERVICE", "SUBSCRIPTION", "TENANT", "FakeApim", "FakeCredential"]

SERVICE = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim"
APIM_PRINCIPAL = "apim-mi"
_APIM_PROVIDER = "/providers/Microsoft.ApiManagement"
# The Retail Prices API's API Management meters for one region, trimmed to what cost.py reads.
RETAIL_PRICES = AZURE_RETAIL_PRICES["API Management"]


@dataclass
class FakeApim(FakeAzure):
    backends: dict[str, dict[str, Any]] = field(default_factory=dict)
    named_values: dict[str, dict[str, Any]] = field(default_factory=dict)
    apis: dict[str, dict[str, Any]] = field(default_factory=dict)
    policies: dict[str, str] = field(default_factory=dict)
    # Principal of the APIM system-assigned identity; None when it is not enabled.
    service_principal: str | None = APIM_PRINCIPAL
    # API operations, keyed by (API name, operation name).
    operations: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    etags: dict[str, str] = field(default_factory=dict)
    service_provisioning: str = "Succeeded"
    # Where the service's gateway answers; a custom domain replaces Azure's host name.
    gateway_url: str = "https://apim.azure-api.net"
    # APIM subscriptions (keys) by name, and the products each API belongs to.
    apim_subscriptions: dict[str, dict[str, Any]] = field(default_factory=dict)
    api_products: dict[str, list[str]] = field(default_factory=dict)
    # Other APIM services (keyed by lower-case ARM ID), for the setup wizard.
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    taken_names: set[str] = field(default_factory=set)
    # Soft-deleted services by lower-case name (Azure keeps them 48 hours), and what other
    # services contain: lower-case service ID -> {"backends": [...], "apis": [...]}.
    deleted_services: dict[str, dict[str, Any]] = field(default_factory=dict)
    service_contents: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)
    skus: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"resourceType": "service", "name": "BasicV2", "locations": ["eastus2"]},
            {"resourceType": "service", "name": "BasicV2", "locations": ["swedencentral"]},
            {"resourceType": "service", "name": "BasicV2", "locations": ["eastus2euap"]},
            {
                "resourceType": "service",
                "name": "StandardV2",
                "locations": ["westus3"],
                "restrictions": [{"type": "Location", "reasonCode": "NotAvailableForSubscription"}],
            },
            {"resourceType": "service", "name": "Developer", "locations": ["East US 2"]},
            {"resourceType": "service", "name": "Consumption", "locations": ["eastus2"]},
        ]
    )
    _versions: count = field(default_factory=lambda: count(1))  # type: ignore[type-arg]

    # -- seeding ------------------------------------------------------------------------

    def add_backend(self, name: str, properties: dict[str, Any]) -> None:
        self.backends[name] = {"name": name, "properties": properties}
        self._bump(f"backends/{name}")

    def add_api(self, name: str, path: str, policy: str | None = None, **extra: Any) -> None:
        self.apis[name] = {
            "name": name,
            "properties": {"displayName": name.title(), "path": path, **extra},
        }
        if policy is not None:
            self.policies[name] = policy
            self._bump(f"policy/{name}")

    def add_service(
        self, name: str, *, group: str = "rg-apim", subscription: str = SUBSCRIPTION, **extra: Any
    ) -> str:
        service_id = (
            f"/subscriptions/{subscription}/resourceGroups/{group}{_APIM_PROVIDER}/service/{name}"
        )
        self.services[service_id.casefold()] = {
            "id": service_id,
            "name": name,
            "location": "eastus2",
            "sku": {"name": "StandardV2", "capacity": 1},
            "properties": {"provisioningState": "Succeeded"},
            **extra,
        }
        return service_id

    def add_subscription(self, name: str, scope: str, state: str = "active") -> None:
        """An APIM subscription; ``scope`` is relative to the service, e.g. ``/apis/x``."""
        self.apim_subscriptions[name] = {
            "name": name,
            "properties": {"scope": f"{SERVICE}{scope}", "displayName": name, "state": state},
        }

    def grant_openai_user(self, account_id: str, principal: str = APIM_PRINCIPAL) -> None:
        super().grant_openai_user(account_id, principal)

    # -- routing -----------------------------------------------------------------------

    def gateway_resource(
        self, request: httpx.Request, caller: str, path: str, body: Any
    ) -> httpx.Response | None:
        if path == SERVICE:
            return self._service(request, caller)
        if path.startswith(f"{SERVICE}/"):
            return self._in_service(request, caller, path, body)
        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "subscriptions":
            return None
        subscription = parts[1]
        provider = f"/subscriptions/{subscription}{_APIM_PROVIDER}"
        if path == f"{provider}/skus":
            return httpx.Response(200, json={"value": self.skus})
        if path == f"{provider}/service":
            prefix = f"/subscriptions/{subscription}/".casefold()
            matching = [item for key, item in self.services.items() if key.startswith(prefix)]
            return httpx.Response(200, json={"value": matching})
        if path == f"{provider}/checkNameAvailability":
            names = self.taken_names | {item["name"] for item in self.services.values()}
            taken = body["name"].casefold() in {name.casefold() for name in names}
            message = f"{body['name']} is already in use." if taken else ""
            return httpx.Response(200, json={"nameAvailable": not taken, "message": message})
        if path == f"{provider}/deletedservices":
            records = [
                {k: v for k, v in r.items() if k != "_service"}
                for r in self.deleted_services.values()
            ]
            return httpx.Response(200, json={"value": records})
        if len(parts) == 8 and parts[4] == "locations" and parts[6] == "deletedservices":
            record = self.deleted_services.get(parts[7].casefold())
            if record is None:
                return _error(404, "ResourceNotFound")
            return httpx.Response(200, json={k: v for k, v in record.items() if k != "_service"})
        if f"{_APIM_PROVIDER}/service/" in path and len(parts) == 8:
            return self._other_service(request, caller, path, body)
        if (
            f"{_APIM_PROVIDER}/service/" in path
            and len(parts) == 9
            and parts[8] in {"backends", "apis"}
        ):
            service = path.rpartition("/")[0].casefold()
            return httpx.Response(
                200, json={"value": self.service_contents.get(service, {}).get(parts[8], [])}
            )
        return None

    def _in_service(
        self, request: httpx.Request, caller: str, path: str, body: Any
    ) -> httpx.Response:
        parts = path[len(SERVICE) :].strip("/").split("/")
        if parts[0] == "subscriptions":
            return self._subscription(request, caller, parts[1:], body)
        if parts[0] == "apis" and parts[2:] == ["products"]:
            products = [{"name": name} for name in self.api_products.get(parts[1], [])]
            return httpx.Response(200, json={"value": products})
        if parts == ["backends"]:
            return httpx.Response(200, json={"value": list(self.backends.values())})
        if parts[0] == "backends" and len(parts) == 2:
            return self._resource(request, self.backends, parts[1], f"backends/{parts[1]}", body)
        if parts[0] == "namedValues" and len(parts) == 2:
            return self._resource(
                request, self.named_values, parts[1], f"namedValues/{parts[1]}", body
            )
        if parts == ["apis"]:
            return httpx.Response(200, json={"value": list(self.apis.values())})
        if parts[0] == "apis" and len(parts) == 2:
            if request.method == "PUT":
                return self._resource(request, self.apis, parts[1], f"apis/{parts[1]}", body)
            if request.method == "DELETE":
                return self._delete_api(request, caller, parts[1])
            api = self.apis.get(parts[1])
            if api is None:
                return _error(404, "ResourceNotFound")
            etag = self.etags.get(f"apis/{parts[1]}")
            return httpx.Response(200, json=api, headers={"ETag": etag} if etag else {})
        if parts[0] == "apis" and len(parts) == 4 and parts[2] == "operations":
            return self._operation(request, parts[1], parts[3], body)
        if parts[0] == "apis" and parts[2:] == ["policies", "policy"]:
            return self._policy(request, parts[1], body)
        return _error(400, f"unhandled {request.method} {path}")

    # -- the service -------------------------------------------------------------------

    def _bump(self, key: str) -> str:
        self.etags[key] = f'"{next(self._versions)}"'
        return self.etags[key]

    def _conditional(self, request: httpx.Request, key: str, exists: bool) -> httpx.Response | None:
        if_match = request.headers.get("If-Match")
        if request.headers.get("If-None-Match") == "*" and exists:
            return _error(412, "PreconditionFailed")
        if if_match and if_match != "*" and (not exists or self.etags.get(key) != if_match):
            return _error(412, "PreconditionFailed")
        return None

    def _service(self, request: httpx.Request, caller: str) -> httpx.Response:
        if request.method == "PATCH":
            if not self._may(caller, SERVICE, "Microsoft.ApiManagement/service/write"):
                return _error(403, "AuthorizationFailed")
            body = json.loads(request.content)
            assert "SystemAssigned" in body["identity"]["type"]
            self.service_principal = "apim-mi-new"
            return httpx.Response(202)
        identity = (
            {"type": "SystemAssigned", "principalId": self.service_principal}
            if self.service_principal
            else None
        )
        return httpx.Response(
            200,
            json={
                "name": "apim",
                # APIM reports the display name of its region.
                "location": "East US 2",
                "sku": {"name": "BasicV2", "capacity": 2},
                "identity": identity,
                "properties": {
                    "gatewayUrl": self.gateway_url,
                    "provisioningState": self.service_provisioning,
                    # Azure's placeholder until the service is up; systemData has the real time.
                    "createdAtUtc": "0001-01-01T00:00:00",
                },
                "systemData": {"createdAt": "2026-10-25T10:00:00.1234567Z"},
            },
        )

    def _subscription(
        self, request: httpx.Request, caller: str, parts: list[str], body: Any
    ) -> httpx.Response:
        action = "Microsoft.ApiManagement/service/subscriptions"
        if not parts:
            if not self._may(caller, SERVICE, f"{action}/read"):
                return _error(403, "AuthorizationFailed")
            return httpx.Response(200, json={"value": list(self.apim_subscriptions.values())})
        name = parts[0]
        existing = self.apim_subscriptions.get(name)
        if parts[1:] == ["listSecrets"]:
            if not self._may(caller, SERVICE, f"{action}/listSecrets/action"):
                return _error(403, "AuthorizationFailed")
            if existing is None:
                return _error(404, "ResourceNotFound")
            return httpx.Response(
                200, json={"primaryKey": f"primary-{name}", "secondaryKey": f"secondary-{name}"}
            )
        if request.method == "GET":
            if not self._may(caller, SERVICE, f"{action}/read"):
                return _error(403, "AuthorizationFailed")
            return httpx.Response(200, json=existing) if existing else _error(404, "NotFound")
        if request.method != "PUT":
            return _error(405, "MethodNotAllowed")
        if not self._may(caller, SERVICE, f"{action}/write"):
            return _error(403, "AuthorizationFailed")
        if request.headers.get("If-None-Match") == "*" and existing:
            return _error(412, "PreconditionFailed")
        properties = dict(body["properties"])
        # ARM answers with the full scope ID.
        properties["scope"] = f"{SERVICE}{properties['scope']}"
        self.apim_subscriptions[name] = {"name": name, "properties": properties}
        return httpx.Response(201, json=self.apim_subscriptions[name])

    def _other_service(
        self, request: httpx.Request, caller: str, path: str, body: Any
    ) -> httpx.Response:
        key = path.casefold()
        if request.method == "GET":
            service = self.services.get(key)
            return httpx.Response(200, json=service) if service else _error(404, "ResourceNotFound")
        if request.method == "DELETE":
            if not self._may(caller, path, "Microsoft.ApiManagement/service/delete"):
                return _error(403, "AuthorizationFailed")
            service = self.services.pop(key, None)
            if service is None:
                return httpx.Response(204)
            # Azure soft-deletes: the record keeps the service for 48 hours.
            name = service["name"]
            location = str(service.get("location") or "eastus2").replace(" ", "").lower()
            self.deleted_services[name.casefold()] = {
                "id": f"/subscriptions/{path.split('/')[2]}{_APIM_PROVIDER}/locations/{location}"
                f"/deletedservices/{name}",
                "name": name,
                "location": "East US 2",
                "properties": {
                    "serviceId": path,
                    "deletionDate": "2026-10-24T08:00:00Z",
                    "scheduledPurgeDate": "2026-10-26T08:00:00Z",
                },
                "_service": service,
            }
            return httpx.Response(202)
        if request.method != "PUT":
            return _error(405, "MethodNotAllowed")
        if (body.get("properties") or {}).get("restore"):
            record = self.deleted_services.pop(path.rpartition("/")[2].casefold(), None)
            if record is None or record["properties"]["serviceId"].casefold() != key:
                return _error(404, "ServiceNotFound")
            restored = record["_service"]
            restored["properties"] = {**restored["properties"], "provisioningState": "Activating"}
            self.services[key] = restored
            return httpx.Response(200, json=restored)
        if request.headers.get("If-None-Match") == "*" and key in self.services:
            return _error(412, "PreconditionFailed")
        if not self._may(caller, path, "Microsoft.ApiManagement/service/write"):
            return _error(403, "AuthorizationFailed")
        registration = self.providers.get(path.split("/")[2], {})
        if registration.get("Microsoft.ApiManagement", "Registered") != "Registered":
            return _error(409, "MissingSubscriptionRegistration")
        if path.rpartition(_APIM_PROVIDER)[0].casefold() not in self.resource_groups:
            return _error(404, "ResourceGroupNotFound")
        self.services[key] = {
            "id": path,
            "name": path.rpartition("/")[2],
            **body,
            "properties": {
                **body["properties"],
                "provisioningState": "Activating",
                "createdAtUtc": "0001-01-01T00:00:00",
            },
            "systemData": {"createdAt": "2026-10-25T10:00:00.1234567Z"},
        }
        return httpx.Response(201, json=self.services[key])

    def _delete_api(self, request: httpx.Request, caller: str, name: str) -> httpx.Response:
        """Like APIM: the API goes with its policy and operations; ``If-Match`` is required."""
        if not self._may(caller, SERVICE, "Microsoft.ApiManagement/service/apis/delete"):
            return _error(403, "AuthorizationFailed")
        if not request.headers.get("If-Match"):
            return _error(400, "IfMatchRequired")
        key = f"apis/{name}"
        if (failed := self._conditional(request, key, name in self.apis)) is not None:
            return failed
        if self.apis.pop(name, None) is None:
            return httpx.Response(204)
        self.policies.pop(name, None)
        for operation in [item for item in self.operations if item[0] == name]:
            del self.operations[operation]
        return httpx.Response(200)

    def _operation(self, request: httpx.Request, api: str, name: str, body: Any) -> httpx.Response:
        if api not in self.apis:
            return _error(404, "ResourceNotFound")
        key = (api, name)
        if request.method != "PUT":
            return _error(400, f"unhandled {request.method} operation")
        if request.headers.get("If-None-Match") == "*" and key in self.operations:
            return _error(412, "PreconditionFailed")
        self.operations[key] = {"name": name, "properties": dict(body["properties"])}
        return httpx.Response(201, json=self.operations[key])

    def _resource(
        self,
        request: httpx.Request,
        store: dict[str, dict[str, Any]],
        name: str,
        key: str,
        body: Any,
    ) -> httpx.Response:
        # APIM resource names are case-insensitive.
        name = next(
            (existing for existing in store if existing.casefold() == name.casefold()), name
        )
        key = f"{key.split('/')[0]}/{name}"
        exists = name in store
        if request.method == "GET":
            if not exists:
                return _error(404, "ResourceNotFound")
            return httpx.Response(200, json=store[name], headers={"ETag": self.etags[key]})
        if (failed := self._conditional(request, key, exists)) is not None:
            return failed
        if request.method == "PUT":
            properties = dict(body["properties"])
            properties.pop("value", None)  # Secrets are write-only.
            store[name] = {"name": name, "properties": properties}
            etag = self._bump(key)
            return httpx.Response(
                201 if not exists else 200, json=store[name], headers={"ETag": etag}
            )
        if request.method == "DELETE":
            if not request.headers.get("If-Match"):
                return _error(400, "IfMatchRequired")
            store.pop(name, None)
            return httpx.Response(200 if exists else 204)
        return _error(405, "MethodNotAllowed")

    def _policy(self, request: httpx.Request, api: str, body: Any) -> httpx.Response:
        key = f"policy/{api}"
        exists = api in self.policies
        if request.method == "GET":
            if not exists:
                return _error(404, "ResourceNotFound")
            assert request.url.params.get("format") == "xml"
            # Like real APIM: the policy GET returns the XML itself, not a JSON envelope.
            return httpx.Response(
                200,
                text=self.policies[api],
                headers={
                    "ETag": self.etags[key],
                    "Content-Type": "application/vnd.ms-azure-apim.policy+xml; charset=utf-8",
                },
            )
        if (failed := self._conditional(request, key, exists)) is not None:
            return failed
        assert body["properties"]["format"] == "xml"
        self.policies[api] = body["properties"]["value"]
        return httpx.Response(200, json=body, headers={"ETag": self._bump(key)})
