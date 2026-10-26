"""Deploy an OpenAI model into an existing Foundry / Azure OpenAI account, like the web UI's
Deploy a model dialog. Runs with the user's own Azure rights; the backend checks every choice
again before it writes anything (backend/pool_manager/model_deploy.py)."""

from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from ..base import tui
from ..client import ApiError, Json
from ..logic import (
    DEPLOYMENT_NAME_PATTERN,
    capacity_limit,
    clamp_capacity,
    default_version,
    finished_state,
    suggest_deployment_name,
)

SKU_LABELS = {
    "GlobalStandard": "Global Standard: any Azure region, the highest quota",
    "DataZoneStandard": "Data Zone Standard: stays in the resource's data zone (US, EU)",
    "Standard": "Standard: only the resource's region",
}
UPGRADES = [
    ("When a new default version is available", "OnceNewDefaultVersionAvailable"),
    ("When the current version expires", "OnceCurrentVersionExpired"),
    ("Never", "NoAutoUpgrade"),
]
POLL_SECONDS = 4.0


class DeployModelScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("ctrl+s", "deploy", "Deploy"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, taken: dict[str, list[str]], *, poll_seconds: float | None = None) -> None:
        super().__init__()
        # Deployment names already in each account (casefolded ID), so the name is free.
        self._taken = taken
        # Read at run time, so tests can shorten it.
        self._poll = POLL_SECONDS if poll_seconds is None else poll_seconds
        self._models: list[Json] = []
        self._available: dict[str, int] = {}
        self._name_edited = False
        self._busy = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog wide") as dialog:
            dialog.border_title = "Deploy a model"
            dialog.border_subtitle = "ctrl+s deploy · esc cancel"
            yield Static(
                Text(
                    "OpenAI models, into a Foundry / Azure OpenAI account the app scans. "
                    "This uses your own Azure rights (e.g. Cognitive Services Contributor).",
                    style="#737aa2",
                )
            )
            yield Label("Account")
            yield Select[str]([], prompt="Loading accounts…", id="account")
            with Horizontal(classes="row"):
                with Vertical():
                    yield Label("Model")
                    yield Select[str]([], prompt="Pick an account first", id="model")
                with Vertical():
                    yield Label("Version")
                    yield Select[str]([], prompt="—", id="version")
            yield Label("Deployment type")
            yield Select[str]([], prompt="—", id="sku")
            with Horizontal(classes="row"):
                with Vertical():
                    yield Label("Capacity (thousand tokens per minute)")
                    yield Input("", id="capacity", type="integer")
                with Vertical():
                    yield Label("Deployment name")
                    yield Input("", id="name")
            yield Static("", id="quota")
            yield Label("Version upgrades")
            yield Select[str](UPGRADES, value=UPGRADES[0][1], allow_blank=False, id="upgrade")
            yield Static("", id="deploy-status")
            yield Static("", id="deploy-error", classes="error-text")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Deploy", id="deploy", variant="primary")

    def on_mount(self) -> None:
        self.load_accounts()

    # -- loading ----------------------------------------------------------------------

    @work(exclusive=True, group="deploy-accounts")
    async def load_accounts(self) -> None:
        try:
            accounts = await tui(self).api.foundry_accounts()
        except ApiError as error:
            self._error(error.message)
            return
        select = self.query_one("#account", Select)
        if not accounts:
            select.set_options([])
            self._error("No Foundry / Azure OpenAI account in the scanned subscriptions.")
            return
        select.set_options(
            [(f"{a['name']} · {a['location']} · {a['kind']}", a["id"]) for a in accounts]
        )
        select.value = accounts[0]["id"]

    @on(Select.Changed, "#account")
    def account_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.load_models(event.value)

    @work(exclusive=True, group="deploy-models")
    async def load_models(self, account_id: str) -> None:
        model = self.query_one("#model", Select)
        model.set_options([])
        try:
            self._models = await tui(self).api.foundry_models(account_id)
        except ApiError as error:
            self._error(error.message)
            return
        self._error("")
        names = list(dict.fromkeys(item["name"] for item in self._models))
        model.set_options([(name, name) for name in names])

    @on(Select.Changed, "#model")
    def model_changed(self, event: Select.Changed) -> None:
        if not isinstance(event.value, str):
            return
        versions = [item for item in self._models if item["name"] == event.value]
        version = self.query_one("#version", Select)
        version.set_options(
            [
                (
                    f"{item['version']}{' (default)' if item.get('is_default_version') else ''}"
                    f"{' · ' + item['lifecycle_status'] if item.get('lifecycle_status') else ''}",
                    item["version"],
                )
                for item in versions
            ]
        )
        preferred = default_version(self._models, event.value)
        if preferred:
            version.value = preferred["version"]
        if not self._name_edited:
            self.query_one("#name", Input).value = suggest_deployment_name(
                event.value, self._taken.get(self._account_id().casefold(), [])
            )

    @on(Select.Changed, "#version")
    def version_changed(self, event: Select.Changed) -> None:
        chosen = self._chosen()
        if chosen is None:
            return
        sku = self.query_one("#sku", Select)
        sku.set_options([(SKU_LABELS[item["name"]], item["name"]) for item in chosen["skus"]])
        if chosen["skus"]:
            sku.value = chosen["skus"][0]["name"]
        self.load_capacity(self._account_id(), chosen["name"], chosen["version"])

    @on(Select.Changed, "#sku")
    def sku_changed(self, event: Select.Changed) -> None:
        option = self._sku()
        if option is not None:
            self.query_one("#capacity", Input).value = str(
                clamp_capacity(
                    option["default_capacity"], option, self._available.get(option["name"])
                )
            )
        self._render_quota()

    @on(Input.Changed, "#name")
    def name_changed(self, event: Input.Changed) -> None:
        if self.query_one("#name", Input).has_focus:
            self._name_edited = True

    @work(exclusive=True, group="deploy-capacity")
    async def load_capacity(self, account_id: str, model: str, version: str) -> None:
        self._available = {}
        self.query_one("#quota", Static).update(Text("Reading the quota left…", style="#737aa2"))
        try:
            result = await tui(self).api.foundry_capacity(account_id, model, version)
        except ApiError as error:
            self.query_one("#quota", Static).update(Text(error.message, style="#e0af68"))
            return
        self._available = {key: int(value) for key, value in result["available"].items()}
        if result.get("error"):
            self.query_one("#quota", Static).update(
                Text(f"Couldn't read the quota left: {result['error']}", style="#e0af68")
            )
            return
        self._render_quota()

    # -- state ------------------------------------------------------------------------

    def _account_id(self) -> str:
        value = self.query_one("#account", Select).value
        return value if isinstance(value, str) else ""

    def _chosen(self) -> Json | None:
        model = self.query_one("#model", Select).value
        version = self.query_one("#version", Select).value
        return next(
            (item for item in self._models if item["name"] == model and item["version"] == version),
            None,
        )

    def _sku(self) -> Json | None:
        chosen = self._chosen()
        name = self.query_one("#sku", Select).value
        return next((item for item in (chosen or {}).get("skus", []) if item["name"] == name), None)

    def _render_quota(self) -> None:
        option = self._sku()
        if option is None:
            return
        available = self._available.get(option["name"])
        if available is None:
            line = f"From {option['min_capacity']} to {option['max_capacity']:,}"
        elif available < option["min_capacity"]:
            line = "No quota left for this type in this region; choose another or request quota."
        else:
            line = f"Quota left: {available:,}K"
        self.query_one("#quota", Static).update(Text(line, style="#737aa2"))

    def _error(self, message: str) -> None:
        self.query_one("#deploy-error", Static).update(message)

    def _problems(self, capacity: int | None) -> list[str]:
        chosen, option = self._chosen(), self._sku()
        if chosen is None or option is None:
            return ["pick a model, a version and a deployment type"]
        problems = []
        name = self.query_one("#name", Input).value.strip()
        if not DEPLOYMENT_NAME_PATTERN.fullmatch(name):
            problems.append("the name takes 2 to 64 letters, digits and . _ -")
        if name.casefold() in {
            item.casefold() for item in self._taken.get(self._account_id().casefold(), [])
        }:
            problems.append("this account already has a deployment with that name")
        limit = capacity_limit(option, self._available.get(option["name"]))
        if capacity is None or not option["min_capacity"] <= capacity <= limit:
            problems.append(f"capacity must be between {option['min_capacity']} and {limit}")
        return problems

    # -- deploying --------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "deploy":
            self.action_deploy()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        # Closing while Azure works is fine: the deployment carries on without us.
        self.dismiss(None)

    def action_deploy(self) -> None:
        if self._busy:
            return
        raw = self.query_one("#capacity", Input).value.strip()
        capacity = int(raw) if raw.isdigit() else None
        problems = self._problems(capacity)
        if problems:
            self._error("Can't deploy yet: " + "; ".join(problems) + ".")
            return
        self._error("")
        assert capacity is not None
        self.deploy(capacity)

    @work(exclusive=True, group="deploy")
    async def deploy(self, capacity: int) -> None:
        chosen, option = self._chosen(), self._sku()
        assert chosen is not None and option is not None
        api = tui(self).api
        name = self.query_one("#name", Input).value.strip()
        body: dict[str, Any] = {
            "account_id": self._account_id(),
            "deployment": name,
            "model_name": chosen["name"],
            "model_version": chosen["version"],
            "sku": option["name"],
            "capacity": capacity,
            "version_upgrade": self.query_one("#upgrade", Select).value,
        }
        self._busy = True
        self.query_one("#deploy", Button).disabled = True
        status_line = self.query_one("#deploy-status", Static)
        try:
            status = await api.create_foundry_deployment(body)
            while not finished_state(status["state"]):
                status_line.update(
                    Text(
                        f"Deploying {name} ({status['state']}); usually a minute or two…",
                        style="#7aa2f7",
                    )
                )
                await asyncio.sleep(self._poll)
                status = await api.foundry_deployment_status(body["account_id"], name)
        except ApiError as error:
            self._error(error.message)
            self._busy = False
            self.query_one("#deploy", Button).disabled = False
            status_line.update("")
            return
        if status["state"] != "Succeeded":
            self._error(f"The deployment failed: {status.get('error') or status['state']}")
            self._busy = False
            self.query_one("#deploy", Button).disabled = False
            status_line.update("")
            return
        self.dismiss(name)
