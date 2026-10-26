"""The TUI end to end: keypresses against the real backend and its in-memory APIM."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import run, settle, wire
from tests.fake_apim import SUBSCRIPTION, TENANT, FakeApim
from tests.test_api_test import API
from textual.pilot import Pilot
from textual.widgets import Checkbox, DataTable, Input, OptionList, Select, Static, TextArea

from pool_tui.screens import deploy_model
from pool_tui.screens.deploy_model import DeployModelScreen
from pool_tui.screens.pool_editor import PoolEditorScreen
from pool_tui.screens.try_api import TryApiScreen


def text(pilot: Pilot[None], selector: str) -> str:
    return str(pilot.app.screen.query_one(selector, Static).content)


def cells(table: DataTable[object]) -> list[list[str]]:
    return [[str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]


def test_opens_on_the_pools_of_the_chosen_service(seeded: FakeApim) -> None:
    async def scenario(pilot: Pilot[None]) -> None:
        app = pilot.app
        assert cells(app.query_one("#pool-list", DataTable))[0] == ["gpt-4o", "balanced", "2"]
        summary = text(pilot, "#pool-summary")
        assert "Balanced · 2 members" in summary and "https://apim.azure-api.net/gpt-4o" in summary
        members = cells(app.query_one("#pool-members", DataTable))
        assert [row[1] for row in members] == ["pool-aoai-east-gpt-4o", "pool-aoai-west-gpt-4o-2"]
        assert [row[4] for row in members] == ["50%", "50%"]
        assert cells(app.query_one("#pool-apis", DataTable))[0][:2] == [
            "gpt-4o",
            "https://apim.azure-api.net/gpt-4o",
        ]
        tiles = text(pilot, "#monitor-tiles")
        assert "Requests 42" in tiles and "429 to clients 2" in tiles
        assert "apim" in str(app.query_one("#topbar", Static).content)

    run(scenario)


def test_number_keys_switch_views(seeded: FakeApim) -> None:
    async def scenario(pilot: Pilot[None]) -> None:
        app = pilot.app
        await pilot.press("2")
        await settle(pilot)
        deployments = cells(app.query_one("#deployment-list", DataTable))
        assert {row[3] for row in deployments} >= {"gpt-4o", "gpt-4o-2"}
        await pilot.press("3")
        await settle(pilot)
        checks = cells(app.query_one("#setup-checks", DataTable))
        assert any("Managing apim" in row[2] for row in checks)
        await pilot.press("1")
        assert app.active_view == "pools"

    run(scenario)


def test_creating_a_pool_with_its_api(seeded: FakeApim) -> None:
    async def scenario(pilot: Pilot[None]) -> None:
        await pilot.press("n")
        await settle(pilot)
        editor = pilot.app.screen
        assert isinstance(editor, PoolEditorScreen)
        editor.query_one("#pool-id", Input).value = "pool-copy"
        editor.query_one("#pool-title", Input).value = "copy"
        editor.query_one("#editor-members", DataTable).focus()
        for _ in range(2):
            await pilot.press("a")
            await settle(pilot)
            await pilot.press("enter")  # The first backend not in the pool yet.
            await settle(pilot)
        assert "Same model: gpt-4o" in str(editor.query_one("#model-check", Static).content)
        assert editor.query_one("#api-path", Input).value == "copy"
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert not isinstance(pilot.app.screen, PoolEditorScreen)
        assert cells(pilot.app.query_one("#pool-list", DataTable))[0][0] == "copy"

    run(scenario)
    pool = seeded.backends["pool-copy"]["properties"]
    assert pool["type"] == "Pool" and len(pool["pool"]["services"]) == 2
    assert seeded.apis["pool-copy"]["properties"]["path"] == "copy"


def test_publishing_needs_two_members(seeded: FakeApim) -> None:
    async def scenario(pilot: Pilot[None]) -> None:
        await pilot.press("n")
        await settle(pilot)
        editor = pilot.app.screen
        editor.query_one("#pool-id", Input).value = "pool-lonely"
        editor.query_one("#editor-members", DataTable).focus()
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert "needs 2 to 30 members" in str(editor.query_one("#editor-error", Static).content)
        await pilot.press("escape")

    run(scenario)
    assert "pool-lonely" not in seeded.backends


def test_editing_promotes_a_primary_and_deleting_asks_first(seeded: FakeApim) -> None:
    async def scenario(pilot: Pilot[None]) -> None:
        # A second pool without an API, so it can be deleted.
        await pilot.press("n")
        await settle(pilot)
        editor = pilot.app.screen
        editor.query_one("#pool-id", Input).value = "pool-spare"
        editor.query_one("#with-api", Checkbox).value = False
        editor.query_one("#editor-members", DataTable).focus()
        for _ in range(2):
            await pilot.press("a", "enter")
            await settle(pilot)
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert pilot.app.query_one("#pool-list", DataTable).row_count == 2

        await pilot.press("e")
        await settle(pilot)
        editor = pilot.app.screen
        assert isinstance(editor, PoolEditorScreen)
        editor.query_one("#editor-members", DataTable).focus()
        await pilot.press("down", "p", "ctrl+s")
        await settle(pilot)

        await pilot.press("D")
        await settle(pilot)
        await pilot.press("n")  # Cancelled: still there.
        await settle(pilot)
        assert "pool-spare" in seeded.backends
        await pilot.press("D")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)

    run(scenario)
    assert "pool-spare" not in seeded.backends
    # Editing keeps the description as stored: one ownership marker, not two.
    assert all(
        backend["properties"].get("description", "").count("Managed by APIM Backend Pool Manager")
        <= 1
        for backend in seeded.backends.values()
    )


def test_trying_the_api_with_a_new_test_key(seeded: FakeApim) -> None:
    async def scenario(pilot: Pilot[None]) -> None:
        pilot.app.query_one("#pool-apis", DataTable).focus()
        await pilot.press("t")
        await settle(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, TryApiScreen)
        keys = screen.query_one("#keys", OptionList)
        assert "Create a test key" in str(keys.get_option_at_index(keys.option_count - 1).prompt)
        keys.focus()
        keys.highlighted = keys.option_count - 1
        await pilot.press("enter")
        await settle(pilot)
        assert screen.query_one("#key", Input).value == f"primary-pool-test-{API}"
        await pilot.press("ctrl+s")
        await settle(pilot)
        result = str(screen.query_one("#result", Static).content)
        assert "200" in result and "region East US 2" in result
        assert '"model": "gpt-4o"' in screen.query_one("#test-response", TextArea).text

    run(scenario)
    assert f"pool-test-{API}" in seeded.apim_subscriptions


def test_importing_deployments_into_a_new_pool(seeded: FakeApim) -> None:
    seeded.add_account("aoai-north", deployments=[("gpt-4o-3", "gpt-4o", "OpenAI")])

    async def scenario(pilot: Pilot[None]) -> None:
        await pilot.press("2")
        await settle(pilot)
        # Discovery caches its scan for 5 minutes; R rescans Azure.
        await pilot.press("R")
        await settle(pilot)
        table = pilot.app.query_one("#deployment-list", DataTable)
        rows = cells(table)
        target = next(index for index, row in enumerate(rows) if row[3] == "gpt-4o-3")
        table.move_cursor(row=target)
        await pilot.press("space", "c")
        await settle(pilot)
        editor = pilot.app.screen
        assert isinstance(editor, PoolEditorScreen)
        assert editor.query_one("#pool-id", Input).value == "pool-gpt-4o"
        assert [row[1] for row in cells(editor.query_one("#editor-members", DataTable))] == [
            "pool-aoai-north-gpt-4o-3"
        ]
        await pilot.press("escape")

    run(scenario)
    assert "pool-aoai-north-gpt-4o-3" in seeded.backends


def test_without_a_service_setup_comes_first(apim: FakeApim, tmp_path: Path) -> None:
    wire(apim, tmp_path, subscription_id="", resource_group="", apim_name="")
    apim.add_service("apim-new")

    async def scenario(pilot: Pilot[None]) -> None:
        assert pilot.app.active_view == "setup"
        assert any(
            "Choose the APIM service" in row[2]
            for row in cells(pilot.app.query_one("#setup-checks", DataTable))
        )
        await pilot.press("c")
        await settle(pilot)
        services = pilot.app.screen.query_one("#services", OptionList)
        assert services.option_count == 1
        services.focus()
        services.highlighted = 0
        await pilot.press("enter")
        await settle(pilot)

    try:
        run(scenario)
    finally:
        from pool_manager.app import app as backend_app

        backend_app.dependency_overrides.clear()
    saved = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert SUBSCRIPTION in saved and TENANT in saved and "apim-new" in saved


def test_deleting_and_restoring_a_service_from_choose_apim(apim: FakeApim, tmp_path: Path) -> None:
    from textual.widgets import Button

    from pool_tui.screens.target import DeleteServiceScreen

    wire(apim, tmp_path, subscription_id="", resource_group="", apim_name="")
    apim.add_service("apim-theirs")
    apim.add_service("apim-mine", tags={"managed-by": "apim-backend-pool"})

    async def scenario(pilot: Pilot[None]) -> None:
        await pilot.press("c")
        await settle(pilot)
        picker = pilot.app.screen
        services = picker.query_one("#services", OptionList)
        names = [str(services.get_option_at_index(i).prompt) for i in range(services.option_count)]
        mine = next(i for i, name in enumerate(names) if name.startswith("apim-mine"))
        theirs = next(i for i, name in enumerate(names) if name.startswith("apim-theirs"))
        assert "created here" in names[mine] and "created here" not in names[theirs]

        # A service someone else created can't be deleted here.
        services.focus()
        services.highlighted = theirs
        await pilot.press("d")
        await settle(pilot)
        assert pilot.app.screen is picker

        services.highlighted = mine
        await pilot.press("d")
        await settle(pilot)
        dialog = pilot.app.screen
        assert isinstance(dialog, DeleteServiceScreen)
        await pilot.press(*"apim-min")
        assert dialog.query_one("#delete", Button).disabled
        await pilot.press("e", "enter")
        await settle(pilot)
        assert pilot.app.screen is picker
        assert picker.query_one("#deleted", OptionList).option_count == 1

        deleted = picker.query_one("#deleted", OptionList)
        deleted.focus()
        deleted.highlighted = 0
        await pilot.press("enter")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)

    try:
        run(scenario)
    finally:
        from pool_manager.app import app as backend_app

        backend_app.dependency_overrides.clear()
    assert "apim-theirs" in {s["name"] for s in apim.services.values()}
    # Deleted, then restored from Recently deleted.
    assert "apim-mine" in {s["name"] for s in apim.services.values()}
    assert not apim.deleted_services
    assert sum(1 for r in apim.requests if r.method == "DELETE") == 1


def test_signing_in_with_a_code_and_out_again(apim: FakeApim, tmp_path: Path) -> None:
    import asyncio

    from tests.test_app import _client, _settings
    from tests.test_credentials import _credential, _Tool

    wire(apim, tmp_path)
    credential = _credential(tmp_path, [_Tool("azure_cli", False)])
    apim.callers.update({"app": "app"})
    _client(apim, _settings(), sign_in=credential, credential=credential)

    async def scenario(pilot: Pilot[None]) -> None:
        await pilot.press("3")
        await settle(pilot)
        table = pilot.app.query_one("#setup-checks", DataTable)
        assert cells(table)[0][2] == "Sign in to Azure"
        table.move_cursor(row=0)
        await pilot.pause()
        assert "Press d to sign in with a code" in text(pilot, "#setup-detail")

        await pilot.press("d")
        await asyncio.sleep(2.5)  # The view checks on the code every two seconds.
        await settle(pilot)
        assert cells(table)[0][2].startswith("Signed in")
        assert "device-code sign-in, kept after a restart" in text(pilot, "#setup-detail")

        await pilot.press("o")
        await settle(pilot)
        assert cells(table)[0][2] == "Sign in to Azure"

    try:
        run(scenario)
    finally:
        from pool_manager.app import app as backend_app

        backend_app.dependency_overrides.clear()
    assert credential.remembered is False


def test_the_pool_breaker_is_published_to_its_members(seeded: FakeApim) -> None:
    async def scenario(pilot: Pilot[None]) -> None:
        await pilot.press("e")
        await settle(pilot)
        editor = pilot.app.screen
        assert isinstance(editor, PoolEditorScreen)
        # It opens with the rule the members share: the default.
        assert editor.query_one("#breaker-trip", Input).value == "60"
        editor.query_one("#breaker-trip", Input).value = "120"
        await settle(pilot)
        note = str(editor.query_one("#breaker-note", Static).content)
        assert "writes it to 2 member(s)" in note
        await pilot.press("ctrl+s")
        await settle(pilot)

    run(scenario)
    services = seeded.backends["pool-gpt-4o"]["properties"]["pool"]["services"]
    for service in services:
        member = seeded.backends[service["id"].rsplit("/", 1)[-1]]
        rule = member["properties"]["circuitBreaker"]["rules"][0]
        assert rule["tripDuration"] == "PT120S"


def test_deploying_a_model_from_the_deployments_view(
    seeded: FakeApim, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_model, "POLL_SECONDS", 0)
    seeded.capacities[("eastus2", "gpt-4o", "2024-11-20")] = {"GlobalStandard": 450}

    async def scenario(pilot: Pilot[None]) -> None:
        await pilot.press("2")
        await settle(pilot)
        await pilot.press("d")
        await settle(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, DeployModelScreen)
        account = screen.query_one("#account", Select)
        east = next(value for _, value in account._options if "aoai-east" in str(value))
        account.value = east
        await settle(pilot)
        screen.query_one("#model", Select).value = "gpt-4o"
        await settle(pilot)
        # The default version, the first pay-per-token type, its default capacity, a free name.
        assert screen.query_one("#version", Select).value == "2024-11-20"
        assert screen.query_one("#sku", Select).value == "GlobalStandard"
        assert screen.query_one("#capacity", Input).value == "50"
        assert screen.query_one("#name", Input).value == "gpt-4o-2"
        assert "Quota left: 450K" in str(screen.query_one("#quota", Static).content)
        await pilot.press("ctrl+s")
        await settle(pilot, rounds=8)
        assert not isinstance(pilot.app.screen, DeployModelScreen)

    run(scenario)
    # Names only need to be unique per account: aoai-west has its own gpt-4o-2.
    east = next(key for key in seeded.deployments if key.endswith("/aoai-east"))
    created = [item for item in seeded.deployments[east] if item["name"] == "gpt-4o-2"]
    assert len(created) == 1
    assert created[0]["sku"] == {"name": "GlobalStandard", "capacity": 50}
    assert created[0]["properties"]["provisioningState"] == "Succeeded"
