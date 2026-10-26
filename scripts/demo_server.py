"""Runs the web UI against the tests' in-memory Azure and LiteLLM gateway (backend/tests/
fake_azure.py, fake_litellm.py), so the UI can be tried, and the README's screenshots retaken,
without Azure. Nothing here reaches Azure: every ARM, gateway, log and price request goes to
the fakes, and the real credential factories in deps.py refuse to build.

    cd frontend && npm run build && cd ..
    backend/.venv/Scripts/python scripts/demo_server.py     # bin/python on macOS and Linux

Open http://127.0.0.1:8010. It starts signed out, like a first run. Sign in, then choose
ai-gateway-contoso, or deploy a new gateway (the deployment finishes after a few polls), and
you are at a gateway with Foundry deployments to pool. ``POST /demo/stage/<stage>`` jumps to a
stage: sign-in, ready, or pool (a pool already in use, with an hour of metrics).
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi import Response  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pool_manager import deps  # noqa: E402
from pool_manager.access import AccessChecker  # noqa: E402
from pool_manager.api_test import ApiTester  # noqa: E402
from pool_manager.app import (  # noqa: E402
    app,
    get_access,
    get_cost,
    get_discovery,
    get_monitor,
    get_service,
    get_settings,
    get_targets,
    get_tester,
)
from pool_manager.arm import ArmClient  # noqa: E402
from pool_manager.config import Settings  # noqa: E402
from pool_manager.cost import CostReader  # noqa: E402
from pool_manager.discovery import DeploymentDiscovery  # noqa: E402
from pool_manager.gateway import GatewayConnection  # noqa: E402
from pool_manager.metrics import PoolMonitor  # noqa: E402
from pool_manager.secret_store import SecretStore  # noqa: E402
from pool_manager.service import PoolManagerService  # noqa: E402
from pool_manager.target import TargetChooser  # noqa: E402
from tests.fake_azure import OPENAI_USER, SUBSCRIPTION, TENANT, FakeAzure  # noqa: E402
from tests.fake_litellm import FakeLiteLLM  # noqa: E402

GROUP = "rg-ai-gateway"
GATEWAY_NAME = "ai-gateway-contoso"
FQDN = f"{GATEWAY_NAME}.wittydune-1a2b3c.eastus2.azurecontainerapps.io"
POOL = "gpt-5-6-luna"
WRITE = {"X-Pool-Manager": "1"}
NO_TARGET = {"subscription_id": "", "resource_group": "", "gateway_name": ""}
TARGET = {
    "subscription_id": SUBSCRIPTION,
    "resource_group": GROUP,
    "gateway_name": GATEWAY_NAME,
    "tenant_id": TENANT,
}
STAGES = ("sign-in", "ready", "pool")
# Polls of a new gateway's deployment before it finishes.
DEPLOY_POLLS = 4


def _jwt(**claims: Any) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    payload = {
        "aud": "https://management.azure.com/",
        "exp": int(time.time()) + 86_400,
        **claims,
    }
    return f"{part({'alg': 'none'})}.{part(payload)}.sig"


TOKEN = _jwt(oid="demo-oid", tid=TENANT, upn="alex@contoso.com", name="Alex Wu")


class _Token:
    def __init__(self) -> None:
        self.token = TOKEN
        self.expires_on = int(time.time()) + 86_400


class DemoCredential:
    """Like LocalCredential on a machine with no Azure sign-in, until Sign in is clicked."""

    persistent = True

    def __init__(self, signed_in: bool) -> None:
        self.signed_in = signed_in

    @property
    def source(self) -> str | None:
        return "browser" if self.signed_in else None

    @property
    def remembered(self) -> bool:
        return self.signed_in

    def get_token(self, *_: str, **__: Any) -> _Token:
        if not self.signed_in:
            raise RuntimeError(
                "Not signed in to Azure. Sign in from the setup page (browser or device code), "
                "or run az login, azd auth login or Connect-AzAccount."
            )
        return _Token()

    def sign_in(self, tenant_id: str | None, scope: str) -> None:
        self.signed_in = True

    def sign_out(self) -> None:
        self.signed_in = False


class DemoAzure(FakeAzure):
    """A deployment started from the wizard finishes after a few status polls."""

    def __init__(self) -> None:
        super().__init__()
        self.polls: dict[str, int] = {}

    def _deployment(
        self, request: httpx.Request, caller: str, path: str, body: Any
    ) -> httpx.Response:
        key = path.removesuffix("/operations").casefold()
        record = self.arm_deployments.get(key)
        if (
            request.method == "GET"
            and record
            and record["properties"]["provisioningState"] == "Running"
        ):
            self.polls[key] = self.polls.get(key, 0) + 1
            if self.polls[key] >= DEPLOY_POLLS:
                parts = path.split("/")
                name = record["parameters"]["name"]
                self.finish_deployment(name, group=parts[4], subscription=parts[2])
                assert isinstance(self.litellm, DemoLiteLLM)
                self.litellm.other_keys.add(record["parameters"]["masterKey"])
        return super()._deployment(request, caller, path, body)


class DemoLiteLLM(FakeLiteLLM):
    """One proxy behind every gateway in the demo, so it takes each new gateway's master key."""

    def __init__(self) -> None:
        super().__init__()
        self.other_keys: set[str] = set()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization", "").removeprefix("Bearer ") in self.other_keys:
            request.headers["Authorization"] = f"Bearer {self.master_key}"
        return super()._handle(request)

    def _inference(self, request: httpx.Request, path: str, body: Any, auth: str) -> httpx.Response:
        time.sleep(0.6)  # A model takes a moment to answer.
        response = super()._inference(request, path, body, auth)
        if response.status_code != 200:
            return response
        answer = {
            "id": "chatcmpl-demo",
            "object": "chat.completion",
            "model": "gpt-5.6-luna",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello! How can I help you today?",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 10, "total_tokens": 19},
        }
        headers = {
            **{k: v for k, v in response.headers.items() if k.startswith("x-")},
            "x-litellm-version": "1.106.4",
            "x-litellm-response-duration-ms": "612.4",
            "x-ratelimit-remaining-tokens": "199981",
            "x-ratelimit-remaining-requests": "1999",
        }
        return httpx.Response(200, json=answer, headers=headers)


def _seed(azure: FakeAzure) -> list[str]:
    azure.tenants = [{"tenantId": TENANT, "displayName": "Contoso", "defaultDomain": "contoso.com"}]
    azure.subscriptions = [
        {
            "subscriptionId": SUBSCRIPTION,
            "displayName": "Contoso AI Platform",
            "tenantId": TENANT,
            "state": "Enabled",
        }
    ]
    for group in (GROUP, "rg-ai", "rg-platform"):
        azure.add_resource_group(group)
    azure.container_apps.clear()
    azure.resources.clear()
    azure.add_gateway(GATEWAY_NAME, group=GROUP, subscription=SUBSCRIPTION, fqdn=FQDN)
    # A LiteLLM someone runs by hand: listed, but not deletable here.
    azure.add_gateway(
        "litellm-platform",
        group="rg-platform",
        subscription=SUBSCRIPTION,
        managed=False,
        fqdn="litellm-platform.internal.contoso.com",
    )
    # Role assignments name the role in the account's own subscription.
    own = OPENAI_USER.replace("/subscriptions/sub/", f"/subscriptions/{SUBSCRIPTION}/")
    azure.role_definitions[own.casefold()] = {
        **azure.role_definitions[OPENAI_USER.casefold()],
        "id": own,
    }
    azure.costs[GATEWAY_NAME] = (38.42, "USD")
    accounts = [
        azure.add_account(
            "foundry-eastus2",
            location="eastus2",
            subscription=SUBSCRIPTION,
            deployments=[
                ("gpt-5.6-luna", "gpt-5.6-luna", "OpenAI"),
                ("gpt-6-astra", "gpt-6-astra", "OpenAI"),
                ("text-embedding-3-large", "text-embedding-3-large", "OpenAI"),
            ],
        ),
        azure.add_account(
            "foundry-swedencentral",
            location="swedencentral",
            subscription=SUBSCRIPTION,
            deployments=[
                ("gpt-5.6-luna", "gpt-5.6-luna", "OpenAI"),
                ("gpt-6-astra", "gpt-6-astra", "OpenAI"),
            ],
        ),
        azure.add_account(
            "aoai-westus3",
            kind="OpenAI",
            location="westus3",
            subscription=SUBSCRIPTION,
            deployments=[("gpt-5.6-luna-dz", "gpt-5.6-luna", "OpenAI")],
        ),
    ]
    for account in accounts:
        location = next(a["location"] for a in azure.accounts if a["id"] == account)
        for model in ("gpt-5.6-luna", "gpt-6-astra"):
            azure.capacities[(location, model, "2026-09-03")] = {
                "GlobalStandard": 450,
                "DataZoneStandard": 300,
                "Standard": 120,
            }
    return accounts


def _wave(minute: int, base: float, swing: float, phase: float = 0.0) -> float:
    return base + swing * math.sin(minute / 7 + phase) + swing / 3 * math.sin(minute / 2.3 + phase)


def _pool_in_use(azure: FakeAzure, accounts: list[str]) -> None:
    """A failover pool over the three gpt-5.6-luna deployments, with an alias and traffic."""
    client = TestClient(app)
    for account in accounts:
        azure.grant_openai_user(account)
    refs = [
        (accounts[1], "gpt-5.6-luna"),
        (accounts[0], "gpt-5.6-luna"),
        (accounts[2], "gpt-5.6-luna-dz"),
    ]
    imported = client.post(
        "/api/deployments/import",
        json={"deployments": [{"account_id": a, "deployment": d} for a, d in refs]},
        headers=WRITE,
    )
    imported.raise_for_status()
    members = [
        {"backend_id": backend_id, "priority": 0 if index == 0 else 1, "weight": 1}
        for index, backend_id in enumerate(imported.json()["backend_ids"])
    ]
    body = {"title": "gpt-5.6-luna", "members": members}
    client.put(f"/api/pools/{POOL}", json=body, headers=WRITE).raise_for_status()
    client.post(f"/api/pools/{POOL}/aliases", json={"name": "gpt-5.6-luna"}, headers=WRITE)

    end = datetime.now(UTC).replace(second=0, microsecond=0)
    minutes = range(61)

    def value(i: int, base: float, swing: float, phase: float = 0.0) -> float:
        return round(max(0.0, _wave(i, base, swing, phase)))

    azure.log_rows["by time"] = [
        {
            "time": f"{end - timedelta(minutes=60 - i):%Y-%m-%dT%H:%M:%SZ}",
            "success": value(i, 150, 30),
            "throttled": 2 if i % 13 == 6 else 0,
            "client_errors": 1 if i % 17 == 3 else 0,
            "server_errors": 0,
            "failovers": 4 if i % 9 == 4 else 1 if i % 4 == 0 else 0,
            "duration_ms": value(i, 2100, 260, 1.0),
            "backend_ms": value(i, 1950, 240, 1.0),
        }
        for i in minutes
    ]
    azure.log_rows["by host, target"] = [
        {
            "host": "foundry-swedencentral.openai.azure.com",
            "target": "azure/gpt-5.6-luna",
            "attempts": 9310,
            "failures": 212,
            "throttled": 205,
        },
        {
            "host": "foundry-eastus2.openai.azure.com",
            "target": "azure/gpt-5.6-luna",
            "attempts": 207,
            "failures": 0,
            "throttled": 0,
        },
        {
            "host": "aoai-westus3.openai.azure.com",
            "target": "azure/gpt-5.6-luna-dz",
            "attempts": 5,
            "failures": 0,
            "throttled": 0,
        },
    ]
    stamps = [(f"{end - timedelta(minutes=60 - i):%Y-%m-%dT%H:%M:%SZ}", i) for i in minutes]

    def series(base: float, swing: float, phase: float = 0.0) -> list[tuple[str, float]]:
        return [(t, value(i, base, swing, phase)) for t, i in stamps]

    for index, (account, deployment) in enumerate(refs):
        share = (1.0, 0.05, 0.01)[index]
        dims = {"ModelDeploymentName": deployment}
        azure.add_metric(
            account,
            "AzureOpenAIRequests",
            series(150 * share, 30 * share, index),
            StatusCode="200",
            **dims,
        )
        if index == 0:  # Sweden takes the traffic and throttles now and then.
            azure.add_metric(
                account,
                "AzureOpenAIRequests",
                series(3, 3, 2.0),
                StatusCode="429",
                **dims,
            )
        azure.add_metric(
            account,
            "TokenTransaction",
            series(61_000 * share, 12_000 * share, index),
            **dims,
        )
        azure.add_metric(
            account,
            "AzureOpenAITTLTInMS",
            series(1800 + 150 * index, 220, index),
            **dims,
        )


def wire(stage: str) -> None:
    """Points every dependency at fresh fakes, seeded for ``stage``."""
    azure = DemoAzure()
    azure.litellm = DemoLiteLLM()
    azure.callers[TOKEN] = "app"
    accounts = _seed(azure)
    credential = DemoCredential(signed_in=stage != "sign-in")
    base = Settings(_env_file=None, auth_mode="disabled", **NO_TARGET)  # type: ignore[call-arg]
    settings = base if stage == "sign-in" else base.with_saved_target(TARGET)
    # Like deps._secrets: kept when the wizard switches gateways.
    secrets = SecretStore(Path(tempfile.mkdtemp()) / "keys.bin", persistent=False)
    _wire(azure, settings, credential, secrets)
    if stage == "pool":
        _pool_in_use(azure, accounts)


def _wire(
    azure: FakeAzure,
    settings: Settings,
    credential: DemoCredential,
    secrets: SecretStore,
) -> None:
    state_file = Path(tempfile.mkdtemp()) / "state.json"
    arm = ArmClient(settings, credential, azure.transport(), sleep=lambda _: None)  # type: ignore[arg-type]
    gateway = GatewayConnection(
        settings,
        arm,
        stored_key=secrets.get,
        transport=azure.litellm.transport(),
        sleep=lambda _: None,
    )
    service = PoolManagerService(settings, arm, gateway)
    discovery = DeploymentDiscovery(settings, arm, service)
    access = AccessChecker(
        settings,
        arm,
        service,
        lambda: [view.account_id for view in discovery.list()],
        credential,  # type: ignore[arg-type]
    )

    def changed() -> None:
        # Choosing, deploying or connecting a gateway moves the demo on to it, keeping the fakes;
        # deleting the current one leaves it with none, like deps.reset_target.
        from pool_manager.config import read_saved_target

        saved = read_saved_target(state_file)
        base = Settings(_env_file=None, auth_mode="disabled", **NO_TARGET)  # type: ignore[call-arg]
        _wire(azure, base.with_saved_target(saved) if saved else base, credential, secrets)

    chooser = TargetChooser(
        settings,
        credential,  # type: ignore[arg-type]
        transport=azure.transport(),
        gateway_transport=azure.litellm.transport(),
        sleep=lambda _: None,
        state_file=state_file,
        secrets=secrets,
        on_change=changed,
    )
    overrides: dict[Any, Any] = {
        get_settings: settings,
        get_service: service,
        get_discovery: discovery,
        get_access: access,
        get_targets: chooser,
        get_cost: CostReader(settings, service, transport=azure.transport()),
        get_tester: ApiTester(settings, service, transport=azure.litellm.transport()),
        get_monitor: PoolMonitor(settings, arm, service, transport=azure.transport()),
    }
    app.dependency_overrides.clear()
    for factory, value in overrides.items():
        app.dependency_overrides[factory] = _const(value)


def _const(value: Any) -> Any:
    # No parameters: FastAPI would read a dependency's parameters from the request.
    return lambda: value


def _refuse(*_: Any, **__: Any) -> Any:
    raise RuntimeError("The demo server reached a real Azure credential factory in deps.py")


@app.post("/demo/stage/{stage}", include_in_schema=False)
def demo_stage(stage: str) -> Response:
    if stage not in STAGES:
        return Response(status_code=404)
    wire(stage)
    return Response(status_code=204)


@asynccontextmanager
async def _lifespan(_: Any) -> AsyncIterator[None]:
    # The app's own lifespan reads the real settings (backend/.env, the saved gateway).
    yield


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--stage", choices=STAGES, default="sign-in")
    args = parser.parse_args()
    deps.ArmClient = _refuse  # type: ignore[assignment,misc]
    deps.LocalCredential = _refuse  # type: ignore[assignment,misc]
    deps.SecretStore = _refuse  # type: ignore[assignment,misc]
    app.router.lifespan_context = _lifespan
    wire(args.stage)
    import uvicorn

    print(f"Demo UI (no Azure): http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
