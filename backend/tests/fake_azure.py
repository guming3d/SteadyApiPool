"""In-memory Azure (ARM and the APIs next to it) behind ``httpx.MockTransport``.

What the app calls besides the gateway itself: tenants, subscriptions, resource groups and
providers, effective permissions and role assignments, Foundry accounts and deployments, Azure
Monitor metrics, Cost Management, the Retail Prices API, Log Analytics queries, and for LiteLLM
the gateway's Container App and its secrets, ARM template deployments and AKS. It records every
request so tests can assert on API versions and headers, and maps bearer tokens to callers.

tests/apim/fake_apim.py adds an APIM service (``gateway_resource``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

from tests.litellm.fake_litellm import FakeLiteLLM

TENANT = "00000000-0000-0000-0000-00000000aaaa"
# A second subscription, for choosing and deploying gateways in the setup wizard.
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
# The gateway's Container App (Settings: sub / rg / gw).
GATEWAY_APP = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/containerApps/gw"
GATEWAY_PRINCIPAL = "gateway-mi"
APP_INSIGHTS = (
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Insights/components/appi-gw"
)
_CONTAINER_APPS = "/providers/Microsoft.App/containerApps"
_DEPLOYMENTS = "/providers/Microsoft.Resources/deployments/"
_AUTHZ = "/providers/Microsoft.Authorization/"
_METRICS = "/providers/Microsoft.Insights/metrics"
_COST_QUERY = "/providers/Microsoft.CostManagement/query"
LOGS_HOST = "api.loganalytics.azure.com"
_PG_COMPUTE = "Azure Database for PostgreSQL Flexible Server Burstable BS Series Compute"
_PG_GENERAL = "Azure Database for PostgreSQL Flexible Server General Purpose Ddsv5 Series Compute"
# The Retail Prices API's meters for one region, trimmed to what cost.py reads.
RETAIL_PRICES = {
    "API Management": [
        {"meterName": "Basic v2 Unit", "unitOfMeasure": "1 Hour", "retailPrice": 0.20548},
        {"meterName": "Basic v2 Secondary Unit", "unitOfMeasure": "1 Hour", "retailPrice": 0.20548},
        {"meterName": "Basic v2 Calls", "unitOfMeasure": "10K", "retailPrice": 0},
        {"meterName": "Basic v2 Calls", "unitOfMeasure": "10K", "retailPrice": 0.03},
        {"meterName": "Developer Unit", "unitOfMeasure": "1 Hour", "retailPrice": 0.0658},
        {"meterName": "Developer Workspace Pack", "unitOfMeasure": "1/Hour", "retailPrice": 0},
        {"meterName": "Consumption Calls", "unitOfMeasure": "10K", "retailPrice": 0.035},
    ],
    # An AKS gateway's nodes (Linux; the Windows and Spot meters must not be taken) and tier.
    "Virtual Machines": [
        {
            "productName": "Virtual Machines Ddsv5 Series",
            "meterName": "D4ds v5",
            "retailPrice": 0.24,
        },
        {
            "productName": "Virtual Machines Ddsv5 Series Windows",
            "meterName": "D4ds v5",
            "retailPrice": 0.424,
        },
        {
            "productName": "Virtual Machines Ddsv5 Series",
            "meterName": "D4ds v5 Spot",
            "retailPrice": 0.04,
        },
    ],
    "Azure Kubernetes Service": [
        {"skuName": "Standard", "meterName": "Standard Uptime SLA", "retailPrice": 0.1},
        {"skuName": "Standard", "meterName": "Standard Long Term Support", "retailPrice": 0.6},
    ],
    "Azure Container Apps": [
        {"skuName": "Standard", "meterName": "Standard vCPU Active Usage", "retailPrice": 2.4e-05},
        {"skuName": "Standard", "meterName": "Standard vCPU Idle Usage", "retailPrice": 3e-06},
        {"skuName": "Standard", "meterName": "Standard Memory Active Usage", "retailPrice": 3e-06},
        {"skuName": "Standard", "meterName": "Standard Memory Idle Usage", "retailPrice": 3e-06},
        {"skuName": "Standard", "meterName": "Standard Requests", "retailPrice": 0.4},
    ],
    "Azure Database for PostgreSQL": [
        {
            "productName": _PG_COMPUTE,
            "skuName": "B1MS",
            "meterName": "B1MS",
            "retailPrice": 0.017,
        },
        {
            "productName": _PG_COMPUTE,
            "skuName": "B2S",
            "meterName": "B2S",
            "retailPrice": 0.068,
        },
        {
            "productName": _PG_GENERAL,
            "skuName": "vCore",
            "meterName": "vCore",
            "retailPrice": 0.089,
        },
        {
            "productName": _PG_GENERAL,
            "skuName": "2 vCore",
            "meterName": "vCore",
            "retailPrice": 0.178,
        },
        {
            "productName": "Azure Database for PostgreSQL Flex Server Storage",
            "skuName": "Storage",
            "meterName": "Storage Data Stored",
            "retailPrice": 0.115,
        },
    ],
    "Redis Cache": [
        {
            "productName": "Azure Managed Redis - Balanced",
            "skuName": "B0",
            "meterName": "B0 Cache Instance",
            "retailPrice": 0.016,
        }
    ],
}
FULL_ACCESS = [{"actions": ["*"], "notActions": [], "dataActions": [], "notDataActions": []}]
OPENAI_USER = (
    "/subscriptions/sub/providers/Microsoft.Authorization/roleDefinitions/"
    "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
)
PROVIDERS = (
    "Microsoft.ApiManagement",
    "Microsoft.App",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights",
    "Microsoft.ManagedIdentity",
    "Microsoft.DBforPostgreSQL",
    "Microsoft.Cache",
    "Microsoft.ContainerService",
    "Microsoft.Network",
)
_CLUSTERS = "/providers/Microsoft.ContainerService/managedClusters"
AKS_CLUSTER_TYPE = "Microsoft.ContainerService/managedClusters"


@dataclass
class _Token:
    token: str = "token"
    expires_on: int = 4_102_444_800


class FakeCredential:
    def __init__(self) -> None:
        # Tenant and scope of every token request (None: the default tenant).
        self.tenants: list[str | None] = []
        self.scopes: list[str] = []

    def get_token(self, *scopes: str, tenant_id: str | None = None, **__: Any) -> _Token:
        self.tenants.append(tenant_id)
        self.scopes.extend(scopes)
        return _Token()


@dataclass
class FakeAzure:
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
    # Azure lists a new role assignment only a while after creating it: these are hidden.
    role_assignment_lag: bool = False
    hidden_assignments: list[dict[str, Any]] = field(default_factory=list)
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
    # Azure Monitor series by (lower-case resource ID, metric name); see add_metric.
    metric_data: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    # Month-to-date cost by gateway tag value (LiteLLM) or lower-case resource ID (APIM);
    # callers Cost Management refuses, and an error (status, code) every cost query fails with.
    costs: dict[str, tuple[float, str]] = field(default_factory=dict)
    cost_forbidden: set[str] = field(default_factory=set)
    cost_error: tuple[int, str] | None = None
    # Retail Prices API items per region and service; prices_down makes the API unreachable.
    retail_prices: dict[str, dict[str, list[dict[str, Any]]]] = field(
        default_factory=lambda: {"eastus2": RETAIL_PRICES}
    )
    prices_down: bool = False
    # Log Analytics: rows each query answers (by a word the query contains), callers refused.
    log_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    logs_forbidden: set[str] = field(default_factory=set)
    log_queries: list[dict[str, Any]] = field(default_factory=list)
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
    # Subscription -> provider -> registration state; "Registered" if absent.
    providers: dict[str, dict[str, str]] = field(default_factory=dict)
    # Polls of a "Registering" provider before it reports "Registered".
    register_polls: int = 2
    # Regions offering Container Apps environments.
    aca_locations: list[str] = field(
        default_factory=lambda: ["East US 2", "Sweden Central", "East US 2 EUAP"]
    )
    # Container Apps (gateways) by lower-case ARM ID, and their secrets.
    container_apps: dict[str, dict[str, Any]] = field(default_factory=dict)
    container_app_secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    # Other tagged resources by lower-case ARM ID (what a gateway deployment creates).
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    # ARM deployments by lower-case ID; finish_deployments makes a new one succeed at once.
    arm_deployments: dict[str, dict[str, Any]] = field(default_factory=dict)
    deployment_failure: str | None = None
    # Regions where the subscription may not create PostgreSQL Flexible Servers, with Azure's
    # reason (the capabilities API then offers no server versions there).
    postgres_restricted: dict[str, str] = field(default_factory=dict)
    # Capacity before deploying: regions offering a resource type ("Namespace/type" -> display
    # names; types not listed are offered everywhere), PostgreSQL sizes per region (default
    # B1ms, B2s and D2ds_v5; Standard_B* are Burstable, the rest General Purpose), and free quota:
    # PostgreSQL vCores (each family the same) and Container Apps environments, per region
    # (default 100 and 10).
    offered: dict[str, list[str]] = field(default_factory=dict)
    postgres_skus: dict[str, list[str]] = field(default_factory=dict)
    postgres_vcores_free: dict[str, int] = field(default_factory=dict)
    environments_free: dict[str, int] = field(default_factory=dict)
    # ARM's validation of a deployment: errors it reports (code, message), whether it answers
    # as a long-running operation, and the bodies it was asked to validate.
    validation_errors: list[tuple[str, str]] = field(default_factory=list)
    validation_async: bool = False
    validations: list[dict[str, Any]] = field(default_factory=list)
    # Deployments stopped with cancel (lower-case IDs), and deleted resource groups.
    canceled: list[str] = field(default_factory=list)
    deleted_groups: list[str] = field(default_factory=list)
    # Resource types whose delete Azure takes its time over (a Container Apps environment can
    # take 20 minutes): the resource stays, ScheduledForDelete, and its operation never ends.
    slow_deletes: set[str] = field(default_factory=set)
    # Deletes Azure refuses (409) this many more times, by lower-case resource type: a
    # dependency still there, or a resource still being created.
    refused_deletes: dict[str, int] = field(default_factory=dict)
    # Polls of those operations' status.
    operation_polls: int = 0
    # AKS: DNS labels already taken, the node size refused to the subscription, its free
    # vCPUs, and Run Command: requests, results by ID, polls before a result and its outcome.
    dns_taken: set[str] = field(default_factory=set)
    vm_restricted: bool = False
    vcpus_free: int = 100
    run_commands: list[dict[str, Any]] = field(default_factory=list)
    # Writes a resize makes, in order: (method, path, body).
    writes: list[tuple[str, str, Any]] = field(default_factory=list)
    command_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    run_command_polls: int = 1
    run_command_exit: int = 0
    # The LiteLLM proxy running in the gateway's Container App (and on AKS clusters).
    litellm: FakeLiteLLM = field(default_factory=FakeLiteLLM)

    def __post_init__(self) -> None:
        self.add_gateway("gw")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # -- seeding ------------------------------------------------------------------------

    def add_gateway(
        self,
        name: str,
        *,
        group: str = "rg",
        subscription: str = "sub",
        fqdn: str = "gw.example.azurecontainerapps.io",
        master_key: str = "sk-master-0123456789",
        principal: str | None = GATEWAY_PRINCIPAL,
        managed: bool = True,
        profile: str = "dev",
        state: str = "Succeeded",
        image: str = "ghcr.io/berriai/litellm:v1.106.4",
        parameters: dict[str, Any] | None = None,
    ) -> str:
        """A LiteLLM Container App; with a deployment's ``parameters``, sized as they say (else
        by ``profile``, as a gateway deployed before the sizing plan)."""
        app_id = f"/subscriptions/{subscription}/resourceGroups/{group}{_CONTAINER_APPS}/{name}"
        identity_id = (
            f"/subscriptions/{subscription}/resourceGroups/{group}"
            f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-{name}"
        )
        insights = (
            f"/subscriptions/{subscription}/resourceGroups/{group}"
            f"/providers/Microsoft.Insights/components/appi-{name}"
        )
        tags = (
            {
                "managed-by": "ai-gateway-pool",
                "ai-gateway": name,
                "ai-gateway-app-insights": insights,
                "ai-gateway-profile": profile,
            }
            if managed
            else {}
        )
        production = profile == "production"
        cpu, memory = (2.0, "4Gi") if production else (1.0, "2Gi")
        replicas = (2, 10) if production else (1, 1)
        if parameters is not None:
            cpu = float(parameters["replicaCpu"])
            memory = str(parameters["replicaMemory"])
            replicas = (int(parameters["minReplicas"]), int(parameters["maxReplicas"]))
            if managed:
                tags |= {
                    "ai-gateway-postgres": parameters["postgresSku"],
                    "ai-gateway-redis": parameters["redisSku"],
                    "ai-gateway-sizing": parameters["sizing"],
                }
        self.container_apps[app_id.casefold()] = {
            "id": app_id,
            "name": name,
            "type": "Microsoft.App/containerApps",
            "location": "East US 2",
            "tags": tags,
            "identity": (
                {
                    "type": "UserAssigned",
                    "userAssignedIdentities": {
                        identity_id: {"principalId": principal, "clientId": f"{name}-client"}
                    },
                }
                if principal
                else {"type": "None"}
            ),
            "properties": {
                "provisioningState": state,
                "configuration": {"ingress": {"fqdn": fqdn, "targetPort": 4000}},
                "template": {
                    "containers": [
                        {
                            "name": "litellm",
                            "image": image,
                            "resources": {"cpu": cpu, "memory": memory},
                        },
                        {
                            "name": "otel-collector",
                            "image": "otel/opentelemetry-collector-contrib:0.120.0",
                            "resources": {"cpu": 0.25, "memory": "0.5Gi"},
                        },
                    ],
                    "scale": {"minReplicas": replicas[0], "maxReplicas": replicas[1]},
                },
            },
            "systemData": {"createdAt": "2026-10-25T10:00:00Z"},
        }
        self.container_app_secrets[app_id.casefold()] = {"litellm-master-key": master_key}
        if managed:
            base = f"/subscriptions/{subscription}/resourceGroups/{group}/providers"
            for kind, resource in (
                ("Microsoft.App/managedEnvironments", f"cae-{name}"),
                ("Microsoft.DBforPostgreSQL/flexibleServers", f"psql-{name}-x1"),
                ("Microsoft.ManagedIdentity/userAssignedIdentities", f"id-{name}"),
                ("Microsoft.Insights/components", f"appi-{name}"),
                ("Microsoft.OperationalInsights/workspaces", f"log-{name}"),
            ):
                resource_id = f"{base}/{kind}/{resource}"
                self.resources[resource_id.casefold()] = {
                    "id": resource_id,
                    "name": resource,
                    "type": kind,
                    "tags": {"managed-by": "ai-gateway-pool", "ai-gateway": name},
                }
        return app_id

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
                    # What Azure enforces for 100 units of an older chat model.
                    "rateLimits": [
                        {"key": "request", "renewalPeriod": 10, "count": 100},
                        {"key": "token", "renewalPeriod": 60, "count": 100000},
                    ],
                },
            }
            for deployment, model, model_format in deployments
        ]
        return account_id

    def add_resource_group(self, name: str, subscription: str = SUBSCRIPTION) -> None:
        key = f"/subscriptions/{subscription}/resourceGroups/{name}".casefold()
        self.resource_groups[key] = {"name": name, "location": "eastus2"}

    def allow(self, caller: str, scope: str, *actions: str) -> None:
        """Gives ``caller`` these actions at ``scope``, on top of its earlier grants."""
        entries = self.permissions.setdefault((caller, scope.casefold()), [])
        entries.append(
            {"actions": list(actions), "notActions": [], "dataActions": [], "notDataActions": []}
        )

    def grant_openai_user(self, account_id: str, principal: str = GATEWAY_PRINCIPAL) -> None:
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

    def add_metric(
        self, resource: str, metric: str, points: list[tuple[str, float]], **dimensions: str
    ) -> None:
        """A metric series of ``resource``; ``dimensions`` are filtered or split on by name."""
        self.metric_data.setdefault((resource.casefold(), metric), []).append(
            {"dims": {k.casefold(): v for k, v in dimensions.items()}, "points": points}
        )

    # -- permissions -------------------------------------------------------------------

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

    # -- routing -----------------------------------------------------------------------

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
        if request.url.host == LOGS_HOST:
            return self._logs(request, caller, path)
        if "/operationStatuses/" in path:
            self.operation_polls += 1
            return httpx.Response(202, json={"status": "InProgress"})
        body = json.loads(request.content) if request.content else None
        if _AUTHZ in path:
            return self._authorization(request, caller, path)
        if path.endswith(_METRICS):
            return self._metrics(request, caller, path.removesuffix(_METRICS))
        if path.endswith(_COST_QUERY):
            return self._cost(request, caller, path.removesuffix(_COST_QUERY))
        if "/Microsoft.CognitiveServices/" in path:
            return self._cognitive(request, caller, path)
        if (answer := self.gateway_resource(request, caller, path, body)) is not None:
            return answer
        if _DEPLOYMENTS in path:
            return self._deployment(request, caller, path, body)
        if _CLUSTERS in path or "/Microsoft.Compute/" in path or "/Microsoft.Network/" in path:
            return self._aks(request, caller, path, body)
        if path.endswith("/databases/default/listKeys"):
            return httpx.Response(200, json={"primaryKey": "redis-key"})
        if _CONTAINER_APPS in path:
            return self._container_app(request, caller, path)
        if path.casefold() in self.resources:
            return self._tagged(request, caller, path)
        return self._root(request, caller, path, body)

    def gateway_resource(
        self, request: httpx.Request, caller: str, path: str, body: Any
    ) -> httpx.Response | None:
        """A product's own resources, before everything else; None for anything else."""
        return None

    def _root(self, request: httpx.Request, caller: str, path: str, body: Any) -> httpx.Response:
        """Tenants, subscriptions, resource groups, their resources and providers."""
        if path == "/tenants":
            return httpx.Response(200, json={"value": self.tenants})
        if path == "/subscriptions":
            return httpx.Response(200, json={"value": self.subscriptions})
        parts = path.strip("/").split("/")
        subscription = parts[1]
        if parts[2:] == ["resourcegroups"]:
            prefix = f"/subscriptions/{subscription}/".casefold()
            groups = [
                group for key, group in self.resource_groups.items() if key.startswith(prefix)
            ]
            return httpx.Response(200, json={"value": groups})
        if len(parts) == 4 and parts[2].casefold() == "resourcegroups":
            return self._resource_group(request, caller, path, parts[3], body)
        if len(parts) == 5 and parts[2].casefold() == "resourcegroups" and parts[4] == "resources":
            return self._group_resources(request, path.rpartition("/")[0])
        if len(parts) >= 4 and parts[2] == "providers" and parts[3] in PROVIDERS:
            return self._provider(request, subscription, parts[3], parts[4:])
        return _error(400, f"unhandled {request.method} {path}")

    def _provider(
        self, request: httpx.Request, subscription: str, provider: str, rest: list[str]
    ) -> httpx.Response:
        states = self.providers.setdefault(subscription, {})
        if rest == ["register"]:
            states[provider] = "Registering"
            return httpx.Response(200, json={"registrationState": "Registering"})
        if provider == "Microsoft.DBforPostgreSQL" and rest[:1] == ["locations"]:
            region = rest[1]
            if rest[2:] == ["resourceType", "flexibleServers", "usages"]:
                usages = [
                    {
                        "name": {"value": family, "localizedValue": family},
                        "limit": self.postgres_vcores_free.get(region, 100),
                        "currentValue": 0,
                        "unit": "Count",
                    }
                    for family in ("standardBSFamily", "standardDDSv5Family")
                ]
                return httpx.Response(200, json={"value": usages})
            reason = self.postgres_restricted.get(region)
            skus = self.postgres_skus.get(
                region, ["Standard_B1ms", "Standard_B2s", "Standard_D2ds_v5"]
            )
            burstable = [sku for sku in skus if sku.startswith("Standard_B")]
            capability = {
                "name": "FlexibleServerCapabilities",
                "restricted": "Enabled" if reason else "Disabled",
                "reason": reason,
                "supportedServerVersions": (
                    [] if reason else [{"name": v} for v in ("14", "15", "16", "17")]
                ),
                "supportedServerEditions": [
                    {
                        "name": "Burstable",
                        "supportedServerSkus": [{"name": sku} for sku in burstable],
                    },
                    {
                        "name": "GeneralPurpose",
                        "supportedServerSkus": [
                            {"name": sku} for sku in skus if sku not in burstable
                        ],
                    },
                ],
            }
            return httpx.Response(200, json={"value": [capability]})
        if provider == "Microsoft.App" and rest[:1] == ["locations"] and rest[2:] == ["usages"]:
            usage = {
                "name": {"value": "ManagedEnvironmentCount", "localizedValue": "Environments"},
                "limit": self.environments_free.get(rest[1], 10),
                "currentValue": 0,
                "unit": "Count",
            }
            return httpx.Response(200, json={"value": [usage]})
        state = states.get(provider, "Registered")
        if state == "Registering":
            self.register_polls -= 1
            if self.register_polls < 0:
                state = states[provider] = "Registered"
        answer: dict[str, Any] = {"namespace": provider, "registrationState": state}
        types = [
            {"resourceType": kind.partition("/")[2], "locations": locations}
            for kind, locations in self.offered.items()
            if kind.partition("/")[0] == provider
        ]
        if provider == "Microsoft.App":
            types += [
                {"resourceType": "managedEnvironments", "locations": self.aca_locations},
                {"resourceType": "containerApps", "locations": self.aca_locations},
            ]
        if types:
            answer["resourceTypes"] = types
        return httpx.Response(200, json=answer)

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
        if request.method == "DELETE":
            self.resource_groups.pop(key, None)
            self.deleted_groups.append(key)
            return httpx.Response(202)
        group = self.resource_groups.get(key)
        return httpx.Response(200, json=group) if group else _error(404, "ResourceGroupNotFound")

    def _group_resources(self, request: httpx.Request, group: str) -> httpx.Response:
        """``resources`` of a group, filtered by one tag (``tagName eq .. and tagValue eq ..``)."""
        query = request.url.params.get("$filter", "")
        name = query.split("tagName eq '")[1].split("'")[0] if "tagName" in query else None
        value = query.split("tagValue eq '")[1].split("'")[0] if "tagValue" in query else None
        prefix = f"{group}/".casefold()
        items = [
            {k: v for k, v in item.items() if k != "properties"}
            for key, item in [*self.container_apps.items(), *self.resources.items()]
            if key.startswith(prefix)
            and (name is None or (item.get("tags") or {}).get(name) == value)
        ]
        return httpx.Response(200, json={"value": items})

    def _aks(self, request: httpx.Request, caller: str, path: str, body: Any) -> httpx.Response:
        """AKS clusters, Run Command, and the Compute and Network reads before deploying one."""
        if "/CheckDnsNameAvailability" in path:
            label = request.url.params["domainNameLabel"]
            return httpx.Response(200, json={"available": label not in self.dns_taken})
        if path.endswith("/providers/Microsoft.Compute/skus"):
            restrictions = (
                [{"type": "Location", "reasonCode": "NotAvailableForSubscription"}]
                if self.vm_restricted
                else []
            )
            sku = {
                "resourceType": "virtualMachines",
                "name": "Standard_D4ds_v5",
                "locationInfo": [{"location": "x", "zones": ["3", "1", "2"]}],
                "restrictions": restrictions,
            }
            return httpx.Response(200, json={"value": [sku]})
        if path.endswith("/usages"):
            usage = {
                "name": {"value": "standardDDSv5Family", "localizedValue": "DDSv5 vCPUs"},
                "limit": self.vcpus_free,
                "currentValue": 0,
            }
            return httpx.Response(200, json={"value": [usage]})
        if path.endswith(_CLUSTERS):
            prefix = path.removesuffix(_CLUSTERS).casefold() + "/"
            clusters = [
                item
                for key, item in self.resources.items()
                if key.startswith(prefix) and item["type"] == AKS_CLUSTER_TYPE
            ]
            return httpx.Response(200, json={"value": clusters})
        cluster, _, rest = path.partition(f"{_CLUSTERS}/")
        name, _, action = rest.partition("/")
        cluster_id = f"{cluster}{_CLUSTERS}/{name}"
        if action == "runCommand":
            if not self._may(
                caller, cluster_id, "Microsoft.ContainerService/managedClusters/runcommand/action"
            ):
                return _error(403, "AuthorizationFailed")
            command = f"cmd{len(self.run_commands) + 1}"
            self.run_commands.append({"cluster": cluster_id, "id": command, **body})
            self.command_results[command] = {
                "polls": self.run_command_polls,
                "exitCode": self.run_command_exit,
            }
            location = f"https://management.azure.com{cluster_id}/commandResults/{command}"
            return httpx.Response(202, headers={"Location": location})
        if action.startswith("agentPools/"):
            item = self.resources.get(cluster_id.casefold())
            if item is None:
                return _error(404, "ResourceNotFound")
            pools = item["properties"]["agentPoolProfiles"]
            pool_name = action.removeprefix("agentPools/")
            index = next((i for i, p in enumerate(pools) if p.get("name") == pool_name), None)
            if index is None:
                return _error(404, "ResourceNotFound")
            if request.method == "PUT":
                if not self._may(
                    caller,
                    cluster_id,
                    "Microsoft.ContainerService/managedClusters/agentPools/write",
                ):
                    return _error(403, "AuthorizationFailed")
                self.writes.append(("PUT", path, body))
                pools[index] = {
                    "name": pool_name,
                    **{k: v for k, v in body["properties"].items() if v is not None},
                }
                return httpx.Response(202)
            return httpx.Response(
                200,
                json={
                    "name": pool_name,
                    "properties": {k: v for k, v in pools[index].items() if k != "name"},
                },
            )
        if not action and request.method == "PATCH" and cluster_id.casefold() in self.resources:
            self.writes.append(("PATCH", path, body))
            self.resources[cluster_id.casefold()]["tags"] = body["tags"]
            return httpx.Response(200, json=self.resources[cluster_id.casefold()])
        if action.startswith("commandResults/"):
            result = self.command_results.get(action.removeprefix("commandResults/"))
            if result is None:
                return _error(404, "NotFound")
            if result["polls"] > 0:
                result["polls"] -= 1
                return httpx.Response(202)
            failed = result["exitCode"] != 0
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "provisioningState": "Failed" if failed else "Succeeded",
                        "exitCode": result["exitCode"],
                        "logs": "==> cert-manager\nError: timed out" if failed else "==> done",
                    }
                },
            )
        if path.casefold() in self.resources:
            return self._tagged(request, caller, path)
        if request.method == "GET":
            return _error(404, "ResourceNotFound")
        return _error(400, f"unhandled {request.method} {path}")

    def _tagged(self, request: httpx.Request, caller: str, path: str) -> httpx.Response:
        if request.method == "PATCH":
            if not self._may(caller, path, "*/write"):
                return _error(403, "AuthorizationFailed")
            body = json.loads(request.content)
            self.writes.append(("PATCH", path, body))
            self.resources[path.casefold()].update(body)
            return httpx.Response(202)
        if request.method == "DELETE":
            if not self._may(caller, path, "*/delete"):
                return _error(403, "AuthorizationFailed")
            item = self.resources.get(path.casefold())
            kind = str((item or {}).get("type") or "").casefold()
            if self.refused_deletes.get(kind, 0) > 0:
                self.refused_deletes[kind] -= 1
                return _error(409, "Conflict")
            if item is not None and str(item["type"]).casefold() in self.slow_deletes:
                item["properties"] = {"provisioningState": "ScheduledForDelete"}
                status = f"https://management.azure.com{path}/operationStatuses/1"
                return httpx.Response(202, headers={"Azure-AsyncOperation": status})
            self.resources.pop(path.casefold(), None)
            return httpx.Response(202)
        item = self.resources.get(path.casefold())
        return httpx.Response(200, json=item) if item else _error(404, "ResourceNotFound")

    def _container_app(self, request: httpx.Request, caller: str, path: str) -> httpx.Response:
        scope, _, rest = path.partition(f"{_CONTAINER_APPS}")
        if not rest:
            # Every Container App of a subscription.
            prefix = f"{scope}/".casefold()
            matching = [app for key, app in self.container_apps.items() if key.startswith(prefix)]
            return httpx.Response(200, json={"value": matching})
        name, _, action = rest.strip("/").partition("/")
        app_id = f"{scope}{_CONTAINER_APPS}/{name}"
        app = self.container_apps.get(app_id.casefold())
        if action == "listSecrets":
            if not self._may(caller, app_id, "Microsoft.App/containerApps/listSecrets/action"):
                return _error(403, "AuthorizationFailed")
            if app is None:
                return _error(404, "ResourceNotFound")
            secrets = self.container_app_secrets.get(app_id.casefold(), {})
            return httpx.Response(
                200, json={"value": [{"name": k, "value": v} for k, v in secrets.items()]}
            )
        if action:
            return _error(400, f"unhandled {request.method} {path}")
        if request.method == "PATCH":
            if not self._may(caller, app_id, "Microsoft.App/containerApps/write"):
                return _error(403, "AuthorizationFailed")
            if app is None:
                return _error(404, "ResourceNotFound")
            body = json.loads(request.content)
            self.writes.append(("PATCH", path, body))
            app["properties"]["template"] = body["properties"]["template"]
            app["tags"] = body["tags"]
            return httpx.Response(202)
        if request.method == "DELETE":
            if not self._may(caller, app_id, "Microsoft.App/containerApps/delete"):
                return _error(403, "AuthorizationFailed")
            self.container_apps.pop(app_id.casefold(), None)
            return httpx.Response(202)
        if not self._may(caller, app_id, "Microsoft.App/containerApps/read"):
            return _error(403, "AuthorizationFailed")
        return httpx.Response(200, json=app) if app else _error(404, "ResourceNotFound")

    def _deployment(
        self, request: httpx.Request, caller: str, path: str, body: Any
    ) -> httpx.Response:
        if path.endswith("/validate"):
            return self._validate(request, caller, path.removesuffix("/validate"), body)
        if path.endswith("/cancel"):
            record = self.arm_deployments.get(path.removesuffix("/cancel").casefold())
            if record is None or record["properties"]["provisioningState"] != "Running":
                return _error(409, "DeploymentCannotBeCancelled")
            record["properties"]["provisioningState"] = "Canceled"
            self.canceled.append(path.removesuffix("/cancel").casefold())
            return httpx.Response(204)
        if "/validationResults/" in path:
            # The long-running validation's outcome.
            return self._validation_result()
        deployment_id, _, rest = path.partition("/operations")
        key = deployment_id.casefold()
        if path.endswith("/operations"):
            record = self.arm_deployments.get(key)
            if record is None:
                return _error(404, "DeploymentNotFound")
            return httpx.Response(200, json={"value": record["operations"]})
        if request.method == "GET":
            record = self.arm_deployments.get(key)
            if record is None:
                return _error(404, "DeploymentNotFound")
            return httpx.Response(200, json={k: v for k, v in record.items() if k != "operations"})
        if request.method == "DELETE":
            if not self._may(caller, deployment_id, "Microsoft.Resources/deployments/delete"):
                return _error(403, "AuthorizationFailed")
            return httpx.Response(200 if self.arm_deployments.pop(key, None) else 204)
        if request.method != "PUT":
            return _error(405, "MethodNotAllowed")
        if not self._may(caller, deployment_id, "Microsoft.Resources/deployments/write"):
            return _error(403, "AuthorizationFailed")
        group = deployment_id.split(_DEPLOYMENTS)[0]
        if group.casefold() not in self.resource_groups:
            return _error(404, "ResourceGroupNotFound")
        parameters = {k: v["value"] for k, v in body["properties"]["parameters"].items()}
        state = "Failed" if self.deployment_failure else "Running"
        self.arm_deployments[key] = {
            "id": deployment_id,
            "name": deployment_id.rpartition("/")[2],
            "properties": {
                "provisioningState": state,
                "timestamp": "2026-10-26T09:00:00Z",
                **(
                    {
                        "error": {
                            "code": "DeploymentFailed",
                            "details": [{"code": "Conflict", "message": self.deployment_failure}],
                        }
                    }
                    if self.deployment_failure
                    else {}
                ),
            },
            "parameters": parameters,
            "template": body["properties"]["template"],
            "tags": body.get("tags"),
            "operations": [
                {
                    "properties": {
                        "provisioningState": "Succeeded",
                        "targetResource": {
                            "resourceType": "Microsoft.ManagedIdentity/userAssignedIdentities",
                            "resourceName": f"id-{parameters['name']}",
                        },
                    }
                },
                {
                    "properties": {
                        "provisioningState": "Failed" if self.deployment_failure else "Running",
                        "targetResource": {
                            "resourceType": "Microsoft.DBforPostgreSQL/flexibleServers",
                            "resourceName": f"psql-{parameters['name']}",
                        },
                        **(
                            {
                                "statusMessage": {
                                    "error": {
                                        "code": "Conflict",
                                        "message": self.deployment_failure,
                                    }
                                }
                            }
                            if self.deployment_failure
                            else {}
                        ),
                    }
                },
                # ARM lists a resource again when it retries it: shown once.
                {
                    "properties": {
                        "provisioningState": "Succeeded",
                        "targetResource": {
                            "resourceType": "Microsoft.ManagedIdentity/userAssignedIdentities",
                            "resourceName": f"id-{parameters['name']}",
                        },
                    }
                },
                # An operation without a target (the deployment itself) is not a step.
                {"properties": {"provisioningState": "Running"}},
            ],
        }
        return httpx.Response(201, json={"id": deployment_id})

    def _validate(
        self, request: httpx.Request, caller: str, deployment_id: str, body: Any
    ) -> httpx.Response:
        if not self._may(caller, deployment_id, "Microsoft.Resources/deployments/validate/action"):
            return _error(403, "AuthorizationFailed")
        group = deployment_id.split(_DEPLOYMENTS)[0]
        if group.casefold() not in self.resource_groups:
            return _error(404, "ResourceGroupNotFound")
        self.validations.append(body)
        if self.validation_async:
            location = f"https://management.azure.com{deployment_id}/validationResults/1"
            return httpx.Response(202, headers={"Location": location, "Retry-After": "1"})
        return self._validation_result()

    def _validation_result(self) -> httpx.Response:
        if not self.validation_errors:
            return httpx.Response(200, json={"properties": {"provisioningState": "Succeeded"}})
        error = {
            "code": "InvalidTemplateDeployment",
            "message": "The template deployment is not valid. See inner errors for details.",
            "details": [
                {
                    "code": "PreflightValidationCheckFailed",
                    "message": "Preflight validation failed.",
                    "details": [
                        {"code": code, "message": message}
                        for code, message in self.validation_errors
                    ],
                }
            ],
        }
        return httpx.Response(400, json={"error": error})

    def fail_resource(
        self,
        name: str,
        *,
        group: str = "rg-new",
        subscription: str = SUBSCRIPTION,
        message: str = "Insufficient capacity in this region.",
        created: tuple[str, ...] = ("Microsoft.ManagedIdentity/userAssignedIdentities",),
    ) -> list[str]:
        """The database of a running gateway deployment fails while the deployment still runs;
        ``created`` are the resource types it had already created (tagged). Returns their IDs."""
        key = (
            f"/subscriptions/{subscription}/resourceGroups/{group}{_DEPLOYMENTS}ai-gateway-{name}"
        ).casefold()
        record = self.arm_deployments[key]
        for operation in record["operations"]:
            target = operation["properties"].get("targetResource") or {}
            if target.get("resourceType") == "Microsoft.DBforPostgreSQL/flexibleServers":
                operation["properties"]["provisioningState"] = "Failed"
                operation["properties"]["statusMessage"] = {
                    "error": {"code": "ResourceDeploymentFailure", "message": message}
                }
        base = f"/subscriptions/{subscription}/resourceGroups/{group}/providers"
        prefixes = {
            "Microsoft.ManagedIdentity/userAssignedIdentities": "id",
            "Microsoft.OperationalInsights/workspaces": "log",
            "Microsoft.Insights/components": "appi",
            "Microsoft.App/managedEnvironments": "cae",
            "Microsoft.DBforPostgreSQL/flexibleServers": "psql",
        }
        ids = []
        for kind in created:
            resource = f"{prefixes[kind]}-{name}"
            resource_id = f"{base}/{kind}/{resource}"
            self.resources[resource_id.casefold()] = {
                "id": resource_id,
                "name": resource,
                "type": kind,
                "location": record["parameters"]["location"],
                "tags": {"managed-by": "ai-gateway-pool", "ai-gateway": name},
                "properties": {"provisioningState": "Succeeded"},
            }
            ids.append(resource_id)
        return ids

    def add_aks_gateway(
        self,
        name: str,
        *,
        group: str = "rg-new",
        subscription: str = SUBSCRIPTION,
        location: str = "swedencentral",
        profile: str = "dev",
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """An AKS cluster this app deployed LiteLLM on, with the resources next to it; returns
        its deployment's outputs. With a deployment's ``parameters``, sized as they say."""
        base = f"/subscriptions/{subscription}/resourceGroups/{group}/providers"
        tags = {"managed-by": "ai-gateway-pool", "ai-gateway": name}
        production = profile == "production"
        pool: dict[str, Any] = (
            {"name": "system", "count": 3, "minCount": 3, "maxCount": 6}
            if production
            else {"name": "system", "count": 1}
        )
        cluster_tags: dict[str, Any] = {"ai-gateway-profile": profile}
        tier = "Standard" if production else "Free"
        redis = production
        if parameters is not None:
            low, high = int(parameters["nodeMin"]), int(parameters["nodeMax"])
            pool = {"name": "system", "count": low, "vmSize": parameters["nodeVmSize"]}
            if high > low:
                pool |= {"minCount": low, "maxCount": high}
            cluster_tags = {
                "ai-gateway-profile": parameters["profile"],
                "ai-gateway-postgres": parameters["postgresSku"],
                "ai-gateway-redis": parameters["redisSku"],
                "ai-gateway-install": parameters["install"],
                "ai-gateway-sizing": parameters["sizing"],
            }
            tier = parameters["clusterTier"]
            redis = bool(parameters["redisSku"])
        cluster_id = f"{base}/{AKS_CLUSTER_TYPE}/{name}"
        insights_id = f"{base}/Microsoft.Insights/components/appi-{name}"
        resources: list[tuple[str, str, dict[str, Any]]] = [
            (
                AKS_CLUSTER_TYPE,
                name,
                {
                    "provisioningState": "Succeeded",
                    "oidcIssuerProfile": {"issuerURL": "https://oidc"},
                    "agentPoolProfiles": [pool],
                },
            ),
            ("Microsoft.DBforPostgreSQL/flexibleServers", f"psql-{name}-x1", {}),
            (
                "Microsoft.ManagedIdentity/userAssignedIdentities",
                f"id-{name}",
                {"principalId": f"{name}-mi", "clientId": f"{name}-client"},
            ),
            (
                "Microsoft.Insights/components",
                f"appi-{name}",
                {"ConnectionString": f"InstrumentationKey={name}"},
            ),
            ("Microsoft.OperationalInsights/workspaces", f"log-{name}", {}),
        ]
        if redis:
            resources.append(("Microsoft.Cache/redisEnterprise", f"redis-{name}-x1", {}))
        for kind, resource, properties in resources:
            resource_id = f"{base}/{kind}/{resource}"
            cluster = kind == AKS_CLUSTER_TYPE
            extra = {"ai-gateway-app-insights": insights_id, **cluster_tags} if cluster else {}
            self.resources[resource_id.casefold()] = {
                "id": resource_id,
                "name": resource,
                "type": kind,
                "location": location,
                "tags": {**tags, **extra},
                "properties": properties,
                **({"sku": {"name": "Base", "tier": tier}} if cluster else {}),
            }
        return {
            "clusterId": cluster_id,
            "fqdn": f"{name}.{location}.cloudapp.azure.com",
            "identityClientId": f"{name}-client",
            "principalId": f"{name}-mi",
            "appInsightsId": insights_id,
            "postgresHost": f"psql-{name}-x1.postgres.database.azure.com",
            "redisHost": f"redis-{name}-x1.{location}.redis.azure.net" if redis else "",
        }

    def finish_deployment(
        self, name: str, *, group: str = "rg-new", subscription: str = SUBSCRIPTION
    ) -> None:
        """Completes a running gateway deployment: its Container App (or AKS cluster) and
        resources appear."""
        key = (
            f"/subscriptions/{subscription}/resourceGroups/{group}{_DEPLOYMENTS}ai-gateway-{name}"
        ).casefold()
        record = self.arm_deployments[key]
        record["properties"]["provisioningState"] = "Succeeded"
        for operation in record["operations"]:
            operation["properties"]["provisioningState"] = "Succeeded"
        if (record.get("tags") or {}).get("ai-gateway-platform") == "aks":
            outputs = self.add_aks_gateway(
                name,
                group=group,
                subscription=subscription,
                location=record["parameters"]["location"],
                profile=record["parameters"]["profile"],
                parameters=record["parameters"],
            )
            record["properties"]["outputs"] = {
                key: {"type": "String", "value": value} for key, value in outputs.items()
            }
            return
        self.add_gateway(
            name,
            group=group,
            subscription=subscription,
            fqdn=f"{name}.example.azurecontainerapps.io",
            master_key=record["parameters"]["masterKey"],
            profile=record["parameters"]["profile"],
            parameters=record["parameters"],
        )

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
                and not any(item is hidden for hidden in self.hidden_assignments)
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
            if self.role_assignment_lag:
                self.hidden_assignments.append(item)
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
        """Cost Management's query: one ActualCost total for the resources with one tag
        (LiteLLM), or with these resource IDs (APIM)."""
        if request.method != "POST":
            return _error(405, "MethodNotAllowed")
        if caller in self.cost_forbidden:
            return _error(403, "RBACAccessDenied")
        if self.cost_error is not None:
            return _error(*self.cost_error)
        body = json.loads(request.content)
        assert body["type"] == "ActualCost" and body["timeframe"] == "MonthToDate", body
        filter_ = body["dataset"]["filter"]
        if "tags" in filter_:
            assert filter_["tags"]["name"] == "ai-gateway", filter_
            keys = filter_["tags"]["values"]
        else:
            keys = filter_["dimensions"]["values"]
            assert all(resource.startswith(scope.casefold()) for resource in keys)
        found = [self.costs[value] for value in keys if value in self.costs]
        rows = [[sum(amount for amount, _ in found), found[0][1]]] if found else []
        columns = [{"name": "Cost", "type": "Number"}, {"name": "Currency", "type": "String"}]
        return httpx.Response(200, json={"properties": {"columns": columns, "rows": rows}})

    def _prices(self, request: httpx.Request) -> httpx.Response:
        """The public Retail Prices API: no sign-in, filtered by service and region, in USD."""
        if self.prices_down:
            return _error(503, "ServiceUnavailable")
        assert "Authorization" not in request.headers  # Never send an ARM token there.
        query = request.url.params.get("$filter", "")
        region = next((r for r in self.retail_prices if f"'{r}'" in query), None)
        service = query.split("serviceName eq '")[1].split("'")[0]
        items = [
            {"currencyCode": "USD", "serviceName": service, "unitOfMeasure": "1 Hour", **item}
            for item in self.retail_prices.get(region or "", {}).get(service, [])
        ]
        return httpx.Response(200, json={"Items": items, "NextPageLink": None})

    def _logs(self, request: httpx.Request, caller: str, path: str) -> httpx.Response:
        """Log Analytics' resource-centric query: rows chosen by a word in the query."""
        if caller in self.logs_forbidden:
            return httpx.Response(
                403, json={"error": {"code": "InsufficientAccessError", "message": "no access"}}
            )
        body = json.loads(request.content)
        self.log_queries.append(
            {"resource": path.removeprefix("/v1").removesuffix("/query"), **body}
        )
        # The last step names the result's shape; quoted names (['time']) match unquoted.
        last = body["query"].split("|")[-1].replace("['", "").replace("']", "")
        rows = next((rows for word, rows in self.log_rows.items() if word in last), [])
        columns = sorted({name for row in rows for name in row})
        return httpx.Response(
            200,
            json={
                "tables": [
                    {
                        "name": "PrimaryResult",
                        "columns": [{"name": name, "type": "dynamic"} for name in columns],
                        "rows": [[row.get(name) for name in columns] for row in rows],
                    }
                ]
            },
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
