"""Fixtures for the live Azure journeys (docs/testing.md). Opt-in: nothing runs, and nothing is
billed, unless E2E_LIVE=1.

Each test module is one user's journey on one kind of gateway, in order: its own app process (a
first run, no gateway chosen), its own resource group ``e2e-<journey>-<run>`` and its own state.
A journey's steps are the methods of a class marked ``incremental``: once one fails, the rest are
skipped, since each builds on the one before. Teardown deletes the resource group whatever
happened (unless E2E_KEEP=1); scripts/e2e-azure-janitor.py deletes any a run couldn't.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from harness import Api, AppProcess, AzureLogin, Env, az, create_group, delete_group

_failed: dict[str, str] = {}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("E2E_LIVE") == "1":
        return
    skip = pytest.mark.skip(
        reason="live Azure tests: set E2E_LIVE=1 (they create billable resources)"
    )
    for item in items:
        item.add_marker(skip)


def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> None:
    # A step's own failure, or its fixtures' (setup): either way the next steps can't build on it.
    failed = call.excinfo is not None and not call.excinfo.errisinstance(pytest.skip.Exception)
    if "incremental" in item.keywords and failed and call.when != "teardown":
        parent = item.parent.nodeid if item.parent else item.nodeid
        _failed.setdefault(parent, item.name)


def pytest_runtest_setup(item: pytest.Item) -> None:
    if "incremental" in item.keywords and item.parent is not None:
        earlier = _failed.get(item.parent.nodeid)
        if earlier is not None:
            pytest.skip(f"an earlier step failed: {earlier}")


@pytest.fixture(scope="session")
def env() -> Env:
    return Env.load()


@pytest.fixture(scope="session")
def login(env: Env) -> AzureLogin:
    login = AzureLogin(env)
    account = login.check()
    print(f"\nSigned in to {account.get('name')} as {account.get('user', {}).get('name')}")
    return login


@pytest.fixture(scope="session")
def foundry(env: Env, login: AzureLogin) -> list[dict[str, str]]:
    """The long-lived Foundry deployments every journey pools (they must exist already)."""
    refs = []
    for account in env.foundry_accounts:
        # This command takes no --ids: the group and the account from the ARM ID.
        parts = account.strip("/").split("/")
        found = az(
            "cognitiveservices",
            "account",
            "deployment",
            "show",
            "--resource-group",
            parts[3],
            "--name",
            parts[-1],
            "--deployment-name",
            env.foundry_deployment,
            check=False,
        )
        if not found:
            raise AssertionError(
                f"{env.foundry_deployment} not found in {account}: run scripts/e2e-azure-setup.py"
            )
        refs.append({"account_id": account, "deployment": env.foundry_deployment})
    return refs


class Journey:
    """One journey's app, resource group and what its steps found out (``state``)."""

    def __init__(self, env: Env, login: AzureLogin, name: str) -> None:
        self.env = env
        self.name = name
        self.group = f"e2e-{name}-{env.run_id}"
        self.gateway = f"e2e{name.replace('-', '')[:6]}{env.run_id}"[:24]
        self.app = AppProcess(env)
        self.api = Api(self.app, login)
        self.state: dict[str, Any] = {}


def journey(name: str) -> Any:
    """A module-scoped fixture for the journey ``name``."""

    @pytest.fixture(scope="module")
    def fixture(env: Env, login: AzureLogin) -> Iterator[Journey]:
        trip = Journey(env, login, name)
        create_group(env, trip.group)
        trip.app.start()
        try:
            yield trip
        finally:
            trip.app.stop()
            if env.keep:
                print(f"\nE2E_KEEP=1: left {trip.group} and the app's folder {trip.app.folder}")
            else:
                for undo in reversed(trip.state.get("teardown", [])):
                    undo()
                delete_group(trip.group)

    return fixture
