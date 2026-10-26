"""Try a pool's API: pick (or create) a subscription key, send a request through the backend,
and read what the answer means. The key lives in this dialog only."""

from __future__ import annotations

import json

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ..base import tui
from ..client import ApiError, Json
from ..logic import DIAGNOSES, default_test_body, diagnose, pretty

CREATE = "__create__"
SCOPES = {"api": "this API only", "product": "product", "all_apis": "all APIs"}
PORTAL_STEPS = (
    "Get a key in the Azure portal: open the APIM service → APIs → Subscriptions → "
    "+ Add subscription, set Scope to API and choose this API, then … → Show/hide keys and copy "
    "the primary key. Paste it below."
)


class TryApiScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("ctrl+s", "send", "Send"),
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, api: Json, model: str, gateway_url: str | None) -> None:
        super().__init__()
        self._api = api
        self._model = model
        base = (gateway_url or "https://<apim>.azure-api.net").rstrip("/")
        self._base = f"{base}/{api['path'].strip('/')}"

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide") as dialog:
            dialog.border_title = f"Try {self._api['display_name']}"
            dialog.border_subtitle = "ctrl+s send · esc close"
            yield Static(Text.assemble(("POST ", "bold #7aa2f7"), (f"{self._base}/…", "")))
            if self._api.get("subscription_required", True):
                yield Label(f"1  Subscription key ({self._api['subscription_key_header']})")
                yield Static(
                    "Looking for subscriptions whose key opens this API…",
                    id="keys-state",
                    classes="muted",
                )
                yield OptionList(id="keys")
                yield Input(placeholder="Paste a key, or pick one above", password=True, id="key")
            else:
                yield Static("This API needs no subscription key.", classes="muted")
            yield Label("2  Request")
            with Horizontal(classes="row"):
                yield Input("chat/completions", id="path")
                yield Input("2025-04-01-preview", id="api-version")
            yield TextArea(default_test_body(self._model), language="json", id="body")
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close")
                yield Button("Send request", id="send", variant="primary")
            yield Static("", id="result")
            yield TextArea("", read_only=True, id="test-response", language="json")

    def on_mount(self) -> None:
        self.query_one("#test-response", TextArea).display = False
        if self._api.get("subscription_required", True):
            self.query_one("#keys", OptionList).display = False
            self.load_keys()
        self.query_one("#send", Button).focus()

    @work(exclusive=True, group="keys")
    async def load_keys(self) -> None:
        state = self.query_one("#keys-state", Static)
        try:
            keys = await tui(self).api.api_keys(self._api["id"])
        except ApiError as error:
            # Without rights to read keys (or delegated access when deployed), use the portal.
            state.update(Text(f"{error.message}\n{PORTAL_STEPS}", style="#e0af68"))
            return
        options = [
            Option(
                f"{key['display_name']}  ({SCOPES.get(key['scope'], key['scope'])}"
                f"{': ' + key['product'] if key.get('product') else ''})",
                id=key["id"],
            )
            for key in keys
            if key["state"] == "active"
        ]
        if not any(key["managed"] for key in keys):
            options.append(
                Option(f"+ Create a test key for {self._api['display_name']}", id=CREATE)
            )
        option_list = self.query_one("#keys", OptionList)
        option_list.clear_options()
        option_list.add_options(options)
        option_list.display = True
        state.update("Pick a key (enter), or paste one. " + ("" if keys else PORTAL_STEPS))

    @on(OptionList.OptionSelected, "#keys")
    def key_chosen(self, event: OptionList.OptionSelected) -> None:
        self.read_key(event.option.id or "")

    @work(exclusive=True, group="key")
    async def read_key(self, key_id: str) -> None:
        api = tui(self).api
        try:
            if key_id == CREATE:
                key_id = (await api.create_api_key(self._api["id"]))["id"]
                self.load_keys()
            value = await api.read_api_key(self._api["id"], key_id)
        except ApiError as error:
            tui(self).show_error(error, "Cannot read the key")
            return
        self.query_one("#key", Input).value = value
        self.notify("Key ready; it is kept in this dialog only.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            self.action_send()
        else:
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_send(self) -> None:
        try:
            body = json.loads(self.query_one("#body", TextArea).text)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self.query_one("#result", Static).update(
                Text("The body must be a JSON object.", style="#f7768e")
            )
            return
        self.send(body)

    @work(exclusive=True, group="send")
    async def send(self, body: Json) -> None:
        result_line = self.query_one("#result", Static)
        response_area = self.query_one("#test-response", TextArea)
        key = self.query("#key").first(Input).value.strip() if self.query("#key") else ""
        result_line.update(Text("Waiting for the response…", style="#737aa2"))
        try:
            result = await tui(self).api.test_api(
                self._api["id"],
                {
                    "path": self.query_one("#path", Input).value.strip().strip("/")
                    or "chat/completions",
                    "api_version": self.query_one("#api-version", Input).value.strip() or None,
                    "subscription_key": key or None,
                    "body": body,
                },
            )
        except ApiError as error:
            result_line.update(Text(error.message, style="#f7768e"))
            return
        status = int(result["status"])
        outcome = diagnose(status, result["body"])
        line = Text.assemble(
            (f" {status} ", f"bold #1a1b26 on {'#9ece6a' if outcome == 'ok' else '#e0af68'}"),
            (f"  {result['elapsed_ms']} ms  ", "#737aa2"),
        )
        if outcome == "ok":
            region = result["headers"].get("x-ms-region")
            line.append(
                "The call succeeded" + (f" (region {region})" if region else "") + ".",
                style="#9ece6a",
            )
        elif outcome in DIAGNOSES:
            line.append(DIAGNOSES[outcome], style="#e0af68")
        if result.get("truncated"):
            line.append("  Only the first 64 KB are shown.", style="#737aa2")
        result_line.update(line)
        response_area.text = pretty(result["body"]) or "(empty response)"
        response_area.display = True
