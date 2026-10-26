"""An in-memory APIM management API behind ``httpx.MockTransport``.

It implements the ETag and conditional-write behaviour the app relies on, and
records every request so tests can assert on API versions and headers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import count
from typing import Any
from urllib.parse import unquote

import httpx

SERVICE = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim"
TENANT = "00000000-0000-0000-0000-00000000aaaa"
# A second subscription, for choosing and creating services in the setup wizard.
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
_APIM_PROVIDER = "/providers/Microsoft.ApiManagement"
_AUTHZ = "/providers/Microsoft.Authorization/"
_METRICS = "/providers/Microsoft.Insights/metrics"
_COST_QUERY = "/providers/Microsoft.CostManagement/query"
# The Retail Prices API's API Management meters for one region, trimmed to what cost.py reads.
RETAIL_PRICES = [
    {"meterName": "Basic v2 Unit", "unitOfMeasure": "1 Hour", "retailPrice": 0.20548},
    {"meterName": "Basic v2 Secondary Unit", "unitOfMeasure": "1 Hour", "retailPrice": 0.20548},
    {"meterName": "Basic v2 Calls", "unitOfMeasure": "10K", "retailPrice": 0},
    {"meterName": "Basic v2 Calls", "unitOfMeasure": "10K", "retailPrice": 0.03},
    {"meterName": "Developer Unit", "unitOfMeasure": "1 Hour", "retailPrice": 0.0658},
    {"meterName": "Developer Workspace Pack", "unitOfMeasure": "1/Hour", "retailPrice": 0},
    {"meterName": "Consumption Calls", "unitOfMeasure": "10K", "retailPrice": 0.035},
]
FULL_ACCESS = [{"actions": ["*"], "notActions": [], "dataActions": [], "notDataActions": []}]
OPENAI_USER = (
    "/subscriptions/sub/providers/Microsoft.Authorization/roleDefinitions/"
    "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
)


@dataclass
class _Token:
    token: str = "token"
    expires_on: int = 4_102_444_800


class FakeCredential:
    def __init__(self) -> None:
        # Tenant of every token request (None: the default tenant).
        self.tenants: list[str | None] = []

    def get_token(self, *_: str, tenant_id: str | None = None, **__: Any) -> _Token:
        self.tenants.append(tenant_id)
        return _Token()


@dataclass
class FakeApim:
    backends: dict[str, dict[str, Any]] = field(default_factory=dict)
    named_values: dict[str, dict[str, Any]] = field(default_factory=dict)
    apis: dict[str, dict[str, Any]] = field(default_factory=dict)
    policies: dict[str, str] = field(default_factory=dict)
    # Principal of the APIM system-assigned identity; None when it is not enabled.
    service_principal: str | None = "apim-mi"
    # API operations, keyed by (API name, operation name).
    operations: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    etags: dict[str, str] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    # Cognitive Services accounts (ARM bodies) and their deployments, keyed by account ID.
    accounts: list[dict[str, Any]] = field(default_factory=list)
    deployments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # What each account can deploy (its ``models`` list), keyed by account ID.
    account_models: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Free capacity per (location, model, version): {sku: thousands of TPM}.
    capacities: dict[tuple[str, str, str], dict[str, int]] = field(default_factory=dict)
    # Callers Azure refuses deployment writes to, and an error the next create fails with.
    deploy_forbidden: set[str] = field(default_factory=set)
    deploy_error: tuple[int, str] | None = None
    # Bearer token -> caller name. "token" is the app (FakeCredential); tests add users.
    callers: dict[str, str] = field(default_factory=lambda: {"token": "app"})
    # Effective permissions by (caller, scope); a caller without any entry has full access.
    permissions: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    # Role assignments (ARM bodies) and role definitions keyed by lower-case ID.
    role_assignments: list[dict[str, Any]] = field(default_factory=list)
    role_definitions: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            OPENAI_USER.casefold(): {
                "id": OPENAI_USER,
                "properties": {
                    "roleName": "Cognitive Services OpenAI User",
                    "type": "BuiltInRole",
                    "permissions": [
                        {
                            "actions": ["Microsoft.CognitiveServices/*/read"],
                            "notActions": [],
                            "dataActions": ["Microsoft.CognitiveServices/accounts/OpenAI/*"],
                            "notDataActions": [],
                        }
                    ],
                },
            }
        }
    )
    service_provisioning: str = "Succeeded"
    # Where the service's gateway answers; a custom domain replaces Azure's host name.
    gateway_url: str = "https://apim.azure-api.net"
    # APIM subscriptions (keys) by name, and the products each API belongs to.
    apim_subscriptions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Azure Monitor series by (lower-case resource ID, metric name); see add_metric.
    metric_data: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    api_products: dict[str, list[str]] = field(default_factory=dict)
    # Month-to-date cost by lower-case resource ID; callers Cost Management refuses, and an
    # error code (status, code) every cost query fails with.
    costs: dict[str, tuple[float, str]] = field(default_factory=dict)
    cost_forbidden: set[str] = field(default_factory=set)
    cost_error: tuple[int, str] | None = None
    # Retail Prices API items per region; prices_down makes the API unreachable.
    retail_prices: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {"eastus2": RETAIL_PRICES}
    )
    prices_down: bool = False
    # Everything above "sub" for the setup wizard: tenants, subscriptions, resource groups and
    # other APIM services (keyed by lower-case ARM ID), provider registration and APIM SKUs.
    tenants: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"tenantId": TENANT, "displayName": "Contoso", "defaultDomain": "contoso.com"}
        ]
    )
    subscriptions: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "subscriptionId": SUBSCRIPTION,
                "displayName": "Dev",
                "tenantId": TENANT,
                "state": "Enabled",
            }
        ]
    )
    resource_groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Subscription -> registration state of Microsoft.ApiManagement; "Registered" if absent.
    providers: dict[str, str] = field(default_factory=dict)
    # Polls of a "Registering" provider before it reports "Registered".
    register_polls: int = 2
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

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

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

    def add_account(
        self,
        name: str,
        *,
        kind: str = "AIServices",
        location: str = "eastus2",
        subscription: str = "sub",
        deployments: list[tuple[str, str, str]] = (),  # type: ignore[assignment]
        state: str = "Succeeded",
    ) -> str:
        """Adds an account; ``deployments`` are ``(name, model, format)`` tuples."""
        account_id = (
            f"/subscriptions/{subscription}/resourceGroups/rg-ai"
            f"/providers/Microsoft.CognitiveServices/accounts/{name}"
        )
        self.accounts.append(
            {
                "id": account_id,
                "name": name,
                "kind": kind,
                "location": location,
                "properties": {
                    "endpoint": f"https://{name}.cognitiveservices.azure.com/",
                    "endpoints": {
                        "OpenAI Language Model Instance API": f"https://{name}.openai.azure.com/"
                    },
                    "disableLocalAuth": True,
                },
            }
        )
        self.account_models[account_id] = _catalog()
        self.deployments[account_id] = [
            {
                "name": deployment,
                "sku": {"name": "GlobalStandard", "capacity": 100},
                "properties": {
                    "model": {"name": model, "version": "2026-09-03", "format": model_format},
                    "provisioningState": state,
                },
            }
            for deployment, model, model_format in deployments
        ]
        return account_id

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

    def add_resource_group(self, name: str, subscription: str = SUBSCRIPTION) -> None:
        key = f"/subscriptions/{subscription}/resourceGroups/{name}".casefold()
        self.resource_groups[key] = {"name": name, "location": "eastus2"}

    def allow(self, caller: str, scope: str, *actions: str) -> None:
        """Gives ``caller`` these actions at ``scope``, on top of its earlier grants."""
        entries = self.permissions.setdefault((caller, scope.casefold()), [])
        entries.append(
            {"actions": list(actions), "notActions": [], "dataActions": [], "notDataActions": []}
        )

    def _effective(self, caller: str, scope: str) -> list[dict[str, Any]]:
        """Permissions at ``scope``, inherited from every parent scope as in ARM."""
        if not any(key[0] == caller for key in self.permissions):
            return FULL_ACCESS
        scope = scope.casefold()
        return [
            entry
            for (who, at), entries in self.permissions.items()
            if who == caller and (scope == at or scope.startswith(f"{at}/"))
            for entry in entries
        ]

    def _may(self, caller: str, scope: str, action: str) -> bool:
        from pool_manager.access import allows

        return allows(self._effective(caller, scope), action)

    def grant_openai_user(self, account_id: str, principal: str = "apim-mi") -> None:
        self.role_assignments.append(
            {
                "id": f"{account_id}{_AUTHZ}roleAssignments/seeded",
                "properties": {
                    "scope": account_id,
                    "principalId": principal,
                    "roleDefinitionId": OPENAI_USER,
                },
            }
        )

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

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = unquote(request.url.path)
        if request.url.host == "prices.azure.com":
            return self._prices(request)
        if "Authorization" not in request.headers and path.count("/") == 2:
            # ARM's challenge names the subscription's tenant.
            challenge = (
                f'Bearer authorization_uri="https://login.microsoftonline.com/{TENANT}", '
                'error="invalid_token"'
            )
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        bearer = request.headers["Authorization"].removeprefix("Bearer ")
        assert bearer in self.callers, bearer
        caller = self.callers[bearer]
        if _AUTHZ in path:
            return self._authorization(request, caller, path)
        if path.endswith(_METRICS):
            return self._metrics(request, caller, path.removesuffix(_METRICS))
        if path.endswith(_COST_QUERY):
            return self._cost(request, caller, path.removesuffix(_COST_QUERY))
        if "/Microsoft.CognitiveServices/" in path:
            return self._cognitive(request, caller, path)
        if path == SERVICE:
            return self._service(request, caller)
        if not path.startswith(SERVICE):
            return self._root(request, caller, path)
        parts = path[len(SERVICE) :].strip("/").split("/")
        body = json.loads(request.content) if request.content else None

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

    def _root(self, request: httpx.Request, caller: str, path: str) -> httpx.Response:
        """Tenants, subscriptions, resource groups, the provider and other APIM services."""
        if path == "/tenants":
            return httpx.Response(200, json={"value": self.tenants})
        if path == "/subscriptions":
            return httpx.Response(200, json={"value": self.subscriptions})
        parts = path.strip("/").split("/")
        subscription = parts[1]
        body = json.loads(request.content) if request.content else None
        if parts[2:] == ["resourcegroups"]:
            prefix = f"/subscriptions/{subscription}/".casefold()
            groups = [
                group for key, group in self.resource_groups.items() if key.startswith(prefix)
            ]
            return httpx.Response(200, json={"value": groups})
        if len(parts) == 4 and parts[2].casefold() == "resourcegroups":
            return self._resource_group(request, caller, path, parts[3], body)
        provider = f"/subscriptions/{subscription}{_APIM_PROVIDER}"
        if path == provider:
            state = self.providers.get(subscription, "Registered")
            if state == "Registering":
                self.register_polls -= 1
                if self.register_polls < 0:
                    state = self.providers[subscription] = "Registered"
            return httpx.Response(200, json={"registrationState": state})
        if path == f"{provider}/register":
            self.providers[subscription] = "Registering"
            return httpx.Response(200, json={"registrationState": "Registering"})
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
        return _error(400, f"unhandled {request.method} {path}")

    def _resource_group(
        self, request: httpx.Request, caller: str, path: str, name: str, body: Any
    ) -> httpx.Response:
        key = path.casefold()
        if request.method == "PUT":
            if not self._may(
                caller, path, "Microsoft.Resources/subscriptions/resourceGroups/write"
            ):
                return _error(403, "AuthorizationFailed")
            self.resource_groups[key] = {"name": name, "location": body["location"], **body}
            return httpx.Response(201, json=self.resource_groups[key])
        group = self.resource_groups.get(key)
        return httpx.Response(200, json=group) if group else _error(404, "ResourceGroupNotFound")

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
        if self.providers.get(path.split("/")[2], "Registered") != "Registered":
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

    def _authorization(self, request: httpx.Request, caller: str, path: str) -> httpx.Response:
        scope, _, rest = path.partition(_AUTHZ)
        if rest == "permissions":
            return httpx.Response(200, json={"value": self._effective(caller, scope)})
        if rest == "roleAssignments" and request.method == "GET":
            principal = request.url.params["$filter"].split("'")[1]
            matching = [
                item
                for item in self.role_assignments
                if item["properties"]["principalId"] == principal
            ]
            return httpx.Response(200, json={"value": matching})
        if rest.startswith("roleAssignments/") and request.method == "PUT":
            if not self._may(caller, scope, "Microsoft.Authorization/roleAssignments/write"):
                return _error(403, "AuthorizationFailed")
            properties = json.loads(request.content)["properties"]
            for item in self.role_assignments:
                existing = item["properties"]
                if (
                    existing["scope"].casefold() == scope.casefold()
                    and existing["principalId"] == properties["principalId"]
                    and existing["roleDefinitionId"].casefold()
                    == properties["roleDefinitionId"].casefold()
                ):
                    return _error(409, "RoleAssignmentExists")
            item = {"id": path, "properties": {**properties, "scope": scope}}
            self.role_assignments.append(item)
            return httpx.Response(201, json=item)
        if rest == "roleDefinitions":
            custom = [
                role
                for role in self.role_definitions.values()
                if role["properties"]["type"] == "CustomRole"
            ]
            return httpx.Response(200, json={"value": custom})
        if rest.startswith("roleDefinitions/"):
            role = self.role_definitions.get(path.casefold())
            return httpx.Response(200, json=role) if role else _error(404, "RoleDefinitionNotFound")
        return _error(400, f"unhandled {request.method} {path}")

    def _cost(self, request: httpx.Request, caller: str, scope: str) -> httpx.Response:
        """Cost Management's query: one ActualCost total for the resources in the filter."""
        if request.method != "POST":
            return _error(405, "MethodNotAllowed")
        if caller in self.cost_forbidden:
            return _error(403, "RBACAccessDenied")
        if self.cost_error is not None:
            return _error(*self.cost_error)
        body = json.loads(request.content)
        assert body["type"] == "ActualCost" and body["timeframe"] == "MonthToDate", body
        resources = body["dataset"]["filter"]["dimensions"]["values"]
        assert all(resource.startswith(scope.casefold()) for resource in resources)
        found = [self.costs[item] for item in resources if item in self.costs]
        rows = [[sum(amount for amount, _ in found), found[0][1]]] if found else []
        columns = [{"name": "Cost", "type": "Number"}, {"name": "Currency", "type": "String"}]
        return httpx.Response(200, json={"properties": {"columns": columns, "rows": rows}})

    def _prices(self, request: httpx.Request) -> httpx.Response:
        """The public Retail Prices API: no sign-in, filtered by region, in USD."""
        if self.prices_down:
            return _error(503, "ServiceUnavailable")
        assert "Authorization" not in request.headers  # Never send an ARM token there.
        query = request.url.params.get("$filter", "")
        assert "serviceName eq 'API Management'" in query, query
        region = next((r for r in self.retail_prices if f"'{r}'" in query), None)
        items = [
            {"currencyCode": "USD", "serviceName": "API Management", **item}
            for item in self.retail_prices.get(region or "", [])
        ]
        return httpx.Response(200, json={"Items": items, "NextPageLink": None})

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

    def add_metric(
        self, resource: str, metric: str, points: list[tuple[str, float]], **dimensions: str
    ) -> None:
        """A metric series of ``resource``; ``dimensions`` are filtered or split on by name."""
        self.metric_data.setdefault((resource.casefold(), metric), []).append(
            {"dims": {k.casefold(): v for k, v in dimensions.items()}, "points": points}
        )

    def _metrics(self, request: httpx.Request, caller: str, resource: str) -> httpx.Response:
        """Azure Monitor metrics: ``X eq 'v'`` filters, ``X eq '*'`` splits, joined by ``and``."""
        if not self._may(caller, resource, "Microsoft.Insights/metrics/read"):
            return _error(403, "AuthorizationFailed")
        params = request.url.params
        aggregation = params["aggregation"].casefold()
        conditions: dict[str, str] = {}
        for clause in filter(None, params.get("$filter", "").split(" and ")):
            name, _, value = clause.partition(" eq ")
            conditions[name.strip().casefold()] = value.strip().strip("'")
        value = []
        for name in params["metricnames"].split(","):
            series = []
            for item in self.metric_data.get((resource.casefold(), name), []):
                dims = item["dims"]
                if any(want != "*" and dims.get(dim) != want for dim, want in conditions.items()):
                    continue
                split = {dim: dims.get(dim, "") for dim, want in conditions.items() if want == "*"}
                series.append(
                    {
                        "metadatavalues": [
                            {"name": {"value": dim}, "value": v} for dim, v in split.items()
                        ],
                        "data": [{"timeStamp": t, aggregation: v} for t, v in item["points"]],
                    }
                )
            value.append({"name": {"value": name}, "unit": "Count", "timeseries": series})
        return httpx.Response(200, json={"value": value})

    def _cognitive(self, request: httpx.Request, caller: str, path: str) -> httpx.Response:
        if path.endswith("/modelCapacities"):
            location = path.split("/locations/")[1].split("/")[0]
            query = request.url.params
            key = (location, query["modelName"], query["modelVersion"])
            assert query["modelFormat"] == "OpenAI"
            value = [
                {"properties": {"skuName": sku, "availableCapacity": free}}
                for sku, free in self.capacities.get(key, {}).items()
            ]
            return httpx.Response(200, json={"value": value})
        if path.endswith("/models"):
            models = self.account_models.get(path.removesuffix("/models"))
            if models is None:
                return _error(404, "ResourceNotFound")
            return httpx.Response(200, json={"value": models})
        if request.method == "PUT" and "/deployments/" in path:
            return self._create_deployment(request, caller, path)
        account = next((a for a in self.accounts if a["id"].casefold() == path.casefold()), None)
        if account is not None:
            return httpx.Response(200, json=account)
        if path.endswith("/providers/Microsoft.CognitiveServices/accounts"):
            subscription = path.split("/")[2]
            if subscription == "forbidden":
                return _error(403, "AuthorizationFailed")
            return httpx.Response(
                200,
                json={
                    "value": [
                        account
                        for account in self.accounts
                        if account["id"].startswith(f"/subscriptions/{subscription}/")
                    ]
                },
            )
        if "/deployments/" in path:
            account_id, _, name = path.rpartition("/deployments/")
            deployment = next(
                (item for item in self.deployments.get(account_id, []) if item["name"] == name),
                None,
            )
            if deployment is None:
                return _error(404, "DeploymentNotFound")
            body = {**deployment, "id": f"{account_id}/deployments/{name}"}
            # A new deployment finishes after being looked at once, like a short provisioning.
            if deployment["properties"]["provisioningState"] == "Creating":
                deployment["properties"]["provisioningState"] = "Succeeded"
            return httpx.Response(200, json=body)
        if path.endswith("/deployments"):
            account_id = path.removesuffix("/deployments")
            if account_id not in self.deployments:
                return _error(404, "ResourceNotFound")
            return httpx.Response(200, json={"value": self.deployments[account_id]})
        return _error(400, f"unhandled {path}")

    def _create_deployment(self, request: httpx.Request, caller: str, path: str) -> httpx.Response:
        if caller in self.deploy_forbidden:
            return _error(403, "AuthorizationFailed")
        if self.deploy_error is not None:
            status, code = self.deploy_error
            self.deploy_error = None
            return _error(status, code)
        account_id, _, name = path.rpartition("/deployments/")
        body = json.loads(request.content)
        deployment = {
            "name": name,
            "sku": body["sku"],
            "properties": {**body["properties"], "provisioningState": "Creating"},
        }
        self.deployments.setdefault(account_id, []).append(deployment)
        return httpx.Response(201, json={**deployment, "id": path})

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


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": code}})


def _catalog() -> list[dict[str, Any]]:
    """A small ``models`` list: two gpt-4o versions, a retired model and a partner model."""

    def sku(name: str, default: int = 10, maximum: int = 1000) -> dict[str, Any]:
        return {"name": name, "capacity": {"default": default, "maximum": maximum}}

    return [
        {
            "format": "OpenAI",
            "name": "gpt-4o",
            "version": "2024-11-20",
            "isDefaultVersion": True,
            "lifecycleStatus": "GenerallyAvailable",
            "capabilities": {
                "chatCompletion": "true",
                "responses": "true",
                "assistants": "false",
                "globalFineTune": "true",
            },
            "deprecation": {"inference": "2027-03-01T00:00:00Z"},
            "skus": [
                sku("Standard"),
                sku("GlobalStandard", 50),
                sku("DataZoneStandard"),
                sku("GlobalProvisionedManaged"),
                # Azure lists the types again for fine-tuned deployments of the model.
                {
                    **sku("GlobalStandard", 1, 10_000_000),
                    "usageName": "OpenAI.GlobalStandard.gpt-4o-finetune",
                },
            ],
        },
        {
            "format": "OpenAI",
            "name": "gpt-4o",
            "version": "2024-08-06",
            "isDefaultVersion": False,
            "lifecycleStatus": "Legacy",
            "capabilities": {"chatCompletion": "true"},
            "skus": [sku("Standard")],
        },
        {
            "format": "OpenAI",
            "name": "gpt-35-turbo",
            "version": "0125",
            "isDefaultVersion": True,
            "lifecycleStatus": "Deprecated",
            "skus": [sku("Standard")],
        },
        {
            "format": "OpenAI",
            "name": "gpt-4o-ptu-only",
            "version": "1",
            "isDefaultVersion": True,
            "lifecycleStatus": "GenerallyAvailable",
            "skus": [sku("ProvisionedManaged")],
        },
        {
            "format": "Anthropic",
            "name": "claude-sonnet-5",
            "version": "2",
            "isDefaultVersion": True,
            "lifecycleStatus": "GenerallyAvailable",
            "skus": [sku("GlobalStandard")],
        },
    ]
