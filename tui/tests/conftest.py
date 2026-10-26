"""The TUI tests drive the real backend app, wired to the backend's in-memory APIM
(backend/tests/fake_apim.py), through httpx.ASGITransport: every keypress goes through the
same routes, auth and ETag checks as the browser."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pool_manager.api_test import ApiTester
from pool_manager.app import app as backend_app
from pool_manager.app import get_monitor, get_service, get_targets, get_tester
from pool_manager.arm import ArmClient
from pool_manager.metrics import PoolMonitor
from pool_manager.target import TargetChooser
from tests.fake_apim import SERVICE, FakeApim, FakeCredential
from tests.test_api_test import API, _pool_api
from tests.test_app import _client, _settings
from textual.pilot import Pilot
from textual.worker import WorkerCancelled

from pool_tui.app import PoolManagerApp
from pool_tui.client import ApiClient

T1 = "2026-10-24T06:00:00Z"


def gateway(request: httpx.Request) -> httpx.Response:
    """The APIM gateway: any key the fake issued works, anything else is APIM's own 401."""
    if not request.headers.get("Ocp-Apim-Subscription-Key", "").startswith("primary-"):
        message = "Access denied due to missing subscription key."
        return httpx.Response(401, json={"statusCode": 401, "message": message})
    return httpx.Response(
        200, json={"model": "gpt-4o", "choices": []}, headers={"x-ms-region": "East US 2"}
    )


def wire(apim: FakeApim, tmp_path: Path, **settings: Any) -> None:
    config = _settings(**settings)
    _client(apim, config)
    service = backend_app.dependency_overrides[get_service]()
    arm = ArmClient(config, FakeCredential(), apim.transport(), sleep=lambda _: None)
    tester = ApiTester(config, service, httpx.MockTransport(gateway))
    monitor = PoolMonitor(config, arm, service)
    chooser = TargetChooser(
        config,
        FakeCredential(),
        transport=apim.transport(),
        sleep=lambda _: None,
        state_file=tmp_path / "state.json",
    )
    backend_app.dependency_overrides[get_tester] = lambda: tester
    backend_app.dependency_overrides[get_monitor] = lambda: monitor
    backend_app.dependency_overrides[get_targets] = lambda: chooser


@pytest.fixture
def apim() -> FakeApim:
    return FakeApim()


@pytest.fixture
def seeded(apim: FakeApim, tmp_path: Path) -> Iterator[FakeApim]:
    """A pool (pool-gpt-4o, two Foundry members) bound to an API, plus an hour of metrics."""
    wire(apim, tmp_path)
    from fastapi.testclient import TestClient

    _pool_api(TestClient(backend_app), apim)
    apim.add_metric(SERVICE, "Requests", [(T1, 40)], ApiId=API, GatewayResponseCode="200")
    apim.add_metric(SERVICE, "Requests", [(T1, 2)], ApiId=API, GatewayResponseCode="429")
    apim.add_metric(SERVICE, "Duration", [(T1, 1500)], ApiId=API)
    apim.add_metric(SERVICE, "BackendDuration", [(T1, 1400)], ApiId=API)
    yield apim
    backend_app.dependency_overrides.clear()


def tui_client() -> ApiClient:
    return ApiClient("http://tui.test", transport=httpx.ASGITransport(app=backend_app))


async def settle(pilot: Pilot[None], rounds: int = 4) -> None:
    """Let workers (which often start more workers) and the resulting renders finish."""
    for _ in range(rounds):
        # An exclusive worker superseded by a newer one (a rescan replacing a scan) is fine.
        with contextlib.suppress(WorkerCancelled):
            await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def run(
    scenario: Callable[[Pilot[None]], Awaitable[None]], size: tuple[int, int] = (160, 50)
) -> PoolManagerApp:
    """Runs the TUI headless against the wired backend and plays ``scenario``."""
    app = PoolManagerApp(tui_client())

    async def main() -> None:
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            await scenario(pilot)
            # A last keypress may start a reload; let it finish before the widgets go away.
            await settle(pilot)

    asyncio.run(main())
    return app
