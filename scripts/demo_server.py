"""Runs the web UI against the tests' in-memory APIM (backend/tests/fake_apim.py), so the UI can
be tried, and the README's screenshots retaken, without Azure. Nothing here reaches Azure:
every ARM, gateway and price request goes to the fake, and the real credential factories in
deps.py refuse to build.

    cd frontend && npm run build && cd ..
    backend/.venv/Scripts/python scripts/demo_server.py     # bin/python on macOS and Linux

Open http://127.0.0.1:8010. It starts signed out, like a first run. Sign in, choose
apim-contoso-ai, and you are at a fresh service with Foundry deployments to pool.
``POST /demo/stage/<stage>`` jumps to a stage: sign-in, ready, or pool (a pool already in
use, with an hour of metrics).
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
from pool_manager.metrics import PoolMonitor  # noqa: E402
from pool_manager.service import PoolManagerService  # noqa: E402
from pool_manager.target import MANAGED_TAG, MANAGED_TAG_VALUE, TargetChooser  # noqa: E402
from tests import fake_apim  # noqa: E402
from tests.fake_apim import OPENAI_USER, SUBSCRIPTION, TENANT, FakeApim  # noqa: E402

GROUP = "rg-ai-gateway"
APIM_NAME = "apim-contoso-ai"
GATEWAY = f"https://{APIM_NAME}.azure-api.net"
# The fake serves one APIM service at fake_apim.SERVICE; give it a realistic ID.
fake_apim.SERVICE = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
    f"/providers/Microsoft.ApiManagement/service/{APIM_NAME}"
)
SERVICE = fake_apim.SERVICE
POOL = "pool-gpt-5-6-luna"
WRITE = {"X-Pool-Manager": "1"}
NO_TARGET = {"subscription_id": "", "resource_group": "", "apim_name": ""}
TARGET = {
    "subscription_id": SUBSCRIPTION,
    "resource_group": GROUP,
    "apim_name": APIM_NAME,
    "tenant_id": TENANT,
}
STAGES = ("sign-in", "ready", "pool")


def _jwt(**claims: Any) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    payload = {"aud": "https://management.azure.com/", "exp": int(time.time()) + 86_400, **claims}
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


class DemoApim(FakeApim):
    def _service(self, request: httpx.Request, caller: str) -> httpx.Response:
        response = super()._service(request, caller)
        if request.method != "GET":
            return response
        body = response.json()
        body.update(
            id=SERVICE,
            name=APIM_NAME,
            sku={"name": "BasicV2", "capacity": 1},
            tags={MANAGED_TAG: MANAGED_TAG_VALUE},
        )
        body["properties"]["gatewayUrl"] = GATEWAY
        return httpx.Response(200, json=body)


def gateway(request: httpx.Request) -> httpx.Response:
    """The APIM gateway: keys the fake issued work, anything else gets APIM's own 401."""
    if not request.headers.get("Ocp-Apim-Subscription-Key", "").startswith("primary-"):
        message = "Access denied due to missing subscription key."
        return httpx.Response(401, json={"statusCode": 401, "message": message})
    time.sleep(0.6)  # A model takes a moment to answer.
    answer = {
        "id": "chatcmpl-demo",
        "object": "chat.completion",
        "model": "gpt-5.6-luna",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello! How can I help you today?"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 10, "total_tokens": 19},
    }
    headers = {
        "x-ms-region": "Sweden Central",
        "apim-request-id": "5f2c9a41-7d3e-4b8a-9c1f-2e6d8b0a4c73",
        "x-ratelimit-remaining-tokens": "199981",
        "x-ratelimit-remaining-requests": "1999",
    }
    return httpx.Response(200, json=answer, headers=headers)


def _seed(apim: FakeApim) -> list[str]:
    apim.tenants = [{"tenantId": TENANT, "displayName": "Contoso", "defaultDomain": "contoso.com"}]
    apim.subscriptions = [
        {
            "subscriptionId": SUBSCRIPTION,
            "displayName": "Contoso AI Platform",
            "tenantId": TENANT,
            "state": "Enabled",
        }
    ]
    for group in (GROUP, "rg-ai", "rg-platform"):
        apim.add_resource_group(group)
    apim.add_service(
        APIM_NAME,
        group=GROUP,
        sku={"name": "BasicV2", "capacity": 1},
        tags={MANAGED_TAG: MANAGED_TAG_VALUE},
    )
    apim.add_service("apim-platform-prod", group="rg-platform")
    # Role assignments name the role in the account's own subscription.
    own = OPENAI_USER.replace("/subscriptions/sub/", f"/subscriptions/{SUBSCRIPTION}/")
    apim.role_definitions[own.casefold()] = {
        **apim.role_definitions[OPENAI_USER.casefold()],
        "id": own,
    }
    apim.costs[SERVICE.casefold()] = (61.37, "USD")
    accounts = [
        apim.add_account(
            "foundry-eastus2",
            location="eastus2",
            subscription=SUBSCRIPTION,
            deployments=[
                ("gpt-5.6-luna", "gpt-5.6-luna", "OpenAI"),
                ("gpt-6-astra", "gpt-6-astra", "OpenAI"),
                ("text-embedding-3-large", "text-embedding-3-large", "OpenAI"),
            ],
        ),
        apim.add_account(
            "foundry-swedencentral",
            location="swedencentral",
            subscription=SUBSCRIPTION,
            deployments=[
                ("gpt-5.6-luna", "gpt-5.6-luna", "OpenAI"),
                ("gpt-6-astra", "gpt-6-astra", "OpenAI"),
            ],
        ),
        apim.add_account(
            "aoai-westus3",
            kind="OpenAI",
            location="westus3",
            subscription=SUBSCRIPTION,
            deployments=[("gpt-5.6-luna-dz", "gpt-5.6-luna", "OpenAI")],
        ),
    ]
    for account in accounts:
        location = next(a["location"] for a in apim.accounts if a["id"] == account)
        for model in ("gpt-5.6-luna", "gpt-6-astra"):
            apim.capacities[(location, model, "2026-09-03")] = {
                "GlobalStandard": 450,
                "DataZoneStandard": 300,
                "Standard": 120,
            }
    return accounts


def _wave(minute: int, base: float, swing: float, phase: float = 0.0) -> float:
    return base + swing * math.sin(minute / 7 + phase) + swing / 3 * math.sin(minute / 2.3 + phase)


def _pool_in_use(apim: FakeApim, accounts: list[str]) -> None:
    """A balanced pool over the three gpt-5.6-luna deployments, with its API and traffic."""
    client = TestClient(app)
    for account in accounts:
        apim.grant_openai_user(account)
    refs = [
        (accounts[0], "gpt-5.6-luna"),
        (accounts[1], "gpt-5.6-luna"),
        (accounts[2], "gpt-5.6-luna-dz"),
    ]
    imported = client.post(
        "/api/deployments/import",
        json={"deployments": [{"account_id": a, "deployment": d} for a, d in refs]},
        headers=WRITE,
    )
    imported.raise_for_status()
    members = [
        {"backend_id": backend_id, "priority": 0, "weight": 1}
        for backend_id in imported.json()["backend_ids"]
    ]
    body = {"title": "gpt-5.6-luna", "members": members}
    client.put(f"/api/pools/{POOL}", json=body, headers=WRITE).raise_for_status()
    created = client.post(f"/api/pools/{POOL}/api", json={"path": "gpt-5-6-luna"}, headers=WRITE)
    created.raise_for_status()

    end = datetime.now(UTC).replace(second=0, microsecond=0)
    stamps = [(f"{end - timedelta(minutes=60 - i):%Y-%m-%dT%H:%M:%SZ}", i) for i in range(61)]

    def series(base: float, swing: float, phase: float = 0.0) -> list[tuple[str, float]]:
        return [(t, round(max(0.0, _wave(i, base, swing, phase)))) for t, i in stamps]

    ok = series(150, 30)
    throttled = [(t, 4.0 if i % 9 == 4 else 1.0 if i % 4 == 0 else 0.0) for t, i in stamps]
    apim.add_metric(SERVICE, "Requests", ok, ApiId=POOL, GatewayResponseCode="200")
    apim.add_metric(SERVICE, "Requests", throttled, ApiId=POOL, GatewayResponseCode="429")
    apim.add_metric(SERVICE, "Duration", series(2100, 260, 1.0), ApiId=POOL)
    apim.add_metric(SERVICE, "BackendDuration", series(1950, 240, 1.0), ApiId=POOL)
    for index, (account, deployment) in enumerate(refs):
        share = (1.05, 1.0, 0.92)[index]
        dims = {"ModelDeploymentName": deployment}
        apim.add_metric(
            account, "AzureOpenAIRequests", series(52 * share, 11, index), StatusCode="200", **dims
        )
        if index == 1:  # Sweden is the busiest region: some 429s, retried on another member.
            apim.add_metric(
                account, "AzureOpenAIRequests", series(3, 3, 2.0), StatusCode="429", **dims
            )
        apim.add_metric(account, "TokenTransaction", series(61_000 * share, 12_000, index), **dims)
        apim.add_metric(
            account, "AzureOpenAITTLTInMS", series(1800 + 150 * index, 220, index), **dims
        )


def wire(stage: str) -> None:
    """Points every dependency at a fresh fake APIM, seeded for ``stage``."""
    apim = DemoApim()
    apim.callers[TOKEN] = "app"
    accounts = _seed(apim)
    credential = DemoCredential(signed_in=stage != "sign-in")
    base = Settings(_env_file=None, auth_mode="disabled", **NO_TARGET)  # type: ignore[call-arg]
    settings = base if stage == "sign-in" else base.with_saved_target(TARGET)
    arm = ArmClient(settings, credential, apim.transport(), sleep=lambda _: None)  # type: ignore[arg-type]
    service = PoolManagerService(settings, arm)
    discovery = DeploymentDiscovery(settings, arm, service)
    access = AccessChecker(
        settings,
        arm,
        service,
        lambda: [view.account_id for view in discovery.list()],
        credential,  # type: ignore[arg-type]
    )
    chooser = TargetChooser(
        settings,
        credential,  # type: ignore[arg-type]
        transport=apim.transport(),
        sleep=lambda _: None,
        state_file=Path(tempfile.mkdtemp()) / "state.json",
        # Choosing a service in the wizard moves the demo on to that service.
        on_change=lambda: wire("ready") if stage == "sign-in" else None,
    )
    overrides: dict[Any, Any] = {
        get_settings: settings,
        get_service: service,
        get_discovery: discovery,
        get_access: access,
        get_targets: chooser,
        get_cost: CostReader(settings, service, transport=apim.transport()),
        get_tester: ApiTester(settings, service, httpx.MockTransport(gateway)),
        get_monitor: PoolMonitor(settings, arm, service),
    }
    app.dependency_overrides.clear()
    for factory, value in overrides.items():
        app.dependency_overrides[factory] = _const(value)
    if stage == "pool":
        _pool_in_use(apim, accounts)


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
    # The app's own lifespan reads the real settings (backend/.env, the saved service).
    yield


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--stage", choices=STAGES, default="sign-in")
    args = parser.parse_args()
    deps.ArmClient = _refuse  # type: ignore[assignment,misc]
    deps.LocalCredential = _refuse  # type: ignore[assignment,misc]
    app.router.lifespan_context = _lifespan
    wire(args.stage)
    import uvicorn

    print(f"Demo UI (no Azure): http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
