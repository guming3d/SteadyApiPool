"""Create or edit a pool: mode, members (primary, weights), session affinity, the pool's circuit
breaker, and for a new pool the API clients will call. Publishing is ETag-conditional; the
backend enforces the rest (and writes the breaker to the members this app manages)."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select, Static

from ..base import tui
from ..client import ApiError, Json
from ..logic import (
    API_PATH_PATTERN,
    ID_PATTERN,
    MODES,
    Member,
    Mode,
    add_member,
    append_members,
    change_weight,
    deployment_mode,
    draft_issues,
    members_for_mode,
    members_to_update,
    percent,
    pool_breaker,
    primary_first,
    read_only_members,
    remove_member,
    role,
    set_primary,
    suggest_api_path,
    traffic_shares,
)
from .common import PickScreen

MODE_LABELS = {
    "failover": "Failover: the primary takes all traffic, the others stand by",
    "balanced": "Balanced: every member takes an equal share",
    "weighted": "Weighted: members share traffic by weight",
}


class PoolEditorScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("a", "add", "Add member"),
        Binding("x", "remove", "Remove"),
        Binding("p", "primary", "Make primary"),
        Binding("plus,equals_sign", "weight(1)", "Weight +"),
        Binding("minus", "weight(-1)", "Weight −"),
        Binding("ctrl+s", "save", "Publish"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        pool: Json | None = None,
        *,
        members: list[str] | None = None,
        pool_id: str = "",
        title: str = "",
    ) -> None:
        super().__init__()
        self._pool = pool
        self._creating = pool is None
        if pool is not None:
            self._members = primary_first([Member.from_json(item) for item in pool["members"]])
        else:
            self._members = append_members([], members or [])[0]
        self._mode: Mode = deployment_mode(self._members)
        self._pool_id = pool["id"] if pool else pool_id
        self._title = (pool.get("title") if pool else title) or ""
        self._backends: dict[str, Json] = {}
        self._models: dict[str, Json] = {}
        self._path_edited = False
        # The pool's breaker as its published members have it; None until the backends load.
        self._breaker: dict[str, Any] | None = None
        self._breaker_uniform = True

    def compose(self) -> ComposeResult:
        capabilities = tui(self).context.get("capabilities") or {}
        # Scrolls on short terminals instead of cutting off the bottom rows.
        with VerticalScroll(classes="dialog wide") as dialog:
            dialog.border_title = "New pool" if self._creating else f"Edit {self._pool_id}"
            dialog.border_subtitle = (
                "a add · x remove · p primary · +/- weight · ctrl+s publish · esc cancel"
            )
            with Horizontal(classes="row"):
                with Vertical():
                    yield Label("Pool ID")
                    yield Input(
                        self._pool_id,
                        id="pool-id",
                        disabled=not self._creating,
                        placeholder="pool-gpt-4o",
                    )
                with Vertical():
                    yield Label("Title")
                    yield Input(self._title, id="pool-title", placeholder="gpt-4o")
            yield Label("Mode")
            yield Select(
                [(MODE_LABELS[mode], mode) for mode in MODES],
                value=self._mode if self._mode != "custom" else Select.NULL,
                allow_blank=self._mode == "custom",
                prompt="Custom priorities and weights (pick a mode to normalise)",
                id="mode",
            )
            yield Label("Members")
            yield DataTable(id="editor-members", cursor_type="row", zebra_stripes=False)
            yield Static("", id="model-check")
            yield Checkbox(
                "Session affinity (cookie)",
                value=bool(self._pool and self._pool.get("session_affinity")),
                id="affinity",
                disabled=not capabilities.get("session_affinity_supported", True),
            )
            yield Label("Circuit breaker, written to every member this app manages on publish")
            with Horizontal(classes="row"):
                with Vertical():
                    yield Label("Failure count")
                    yield Input("1", id="breaker-count", type="integer")
                with Vertical():
                    yield Label("Failure interval (s)")
                    yield Input("60", id="breaker-interval", type="integer")
                with Vertical():
                    yield Label("Trip duration (s)")
                    yield Input("60", id="breaker-trip", type="integer")
                yield Checkbox("Use Retry-After", value=True, id="breaker-retry-after")
            yield Static("", id="breaker-note")
            if self._creating:
                yield Checkbox(
                    "Create an API for this pool so clients get an endpoint",
                    value=True,
                    id="with-api",
                )
                yield Label("API path")
                yield Input(self._suggested_path(), id="api-path")
            yield Static("", id="editor-error", classes="error-text")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Publish to APIM", id="save", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#editor-members", DataTable)
        table.add_columns("#", "Member", "Role", "Weight", "Share", "Model")
        if self._creating:
            self.query_one("#pool-id", Input).focus()
        else:
            table.focus()
        self.load_backends()
        self.render_members()

    def _suggested_path(self) -> str:
        prefix = (tui(self).context.get("capabilities") or {}).get("id_prefix", "")
        return suggest_api_path(self._title, self._pool_id, prefix)

    # -- data -------------------------------------------------------------------------

    @work(exclusive=True, group="editor-backends")
    async def load_backends(self) -> None:
        try:
            backends = await tui(self).api.backends()
        except ApiError as error:
            tui(self).show_error(error, "Cannot list backends")
            return
        self._backends = {backend["id"].casefold(): backend for backend in backends}
        if self._breaker is None:
            published = (
                [m["backend_id"] for m in self._pool["members"]]
                if self._pool
                else [member.backend_id for member in self._members]
            )
            self._breaker, self._breaker_uniform = pool_breaker(published, self._backends)
            self.query_one("#breaker-count", Input).value = str(self._breaker["failure_count"])
            self.query_one("#breaker-interval", Input).value = str(
                self._breaker["interval_seconds"]
            )
            self.query_one("#breaker-trip", Input).value = str(
                self._breaker["trip_duration_seconds"]
            )
            self.query_one("#breaker-retry-after", Checkbox).value = bool(
                self._breaker["accept_retry_after"]
            )
        self.render_members()

    @work(exclusive=True, group="editor-models")
    async def check_models(self) -> None:
        line = self.query_one("#model-check", Static)
        if not self._members:
            line.update("")
            return
        line.update(Text("Checking which model each member serves…", style="#737aa2"))
        try:
            result = await tui(self).api.check_models(
                [member.backend_id for member in self._members]
            )
        except ApiError as error:
            line.update(Text(error.message, style="#f7768e"))
            return
        self._models = {item["backend_id"].casefold(): item for item in result["members"]}
        self.render_members(check=False)
        if result["ok"]:
            names = ", ".join(result["model_names"]) or "no Foundry deployments"
            line.update(Text(f"✓ Same model: {names}", style="#9ece6a"))
        else:
            line.update(Text(f"✗ {result['message']}", style="#f7768e"))

    def render_members(self, check: bool = True) -> None:
        table = self.query_one("#editor-members", DataTable)
        cursor = table.cursor_row
        table.clear()
        shares = traffic_shares(self._members)
        for index, member in enumerate(self._members):
            model = self._models.get(member.backend_id.casefold()) or {}
            missing = self._backends and member.backend_id.casefold() not in self._backends
            name = Text(member.backend_id, style="#f7768e" if missing else "")
            table.add_row(
                str(index + 1),
                name,
                role(self._members, index),
                str(member.weight),
                percent(shares[index], 0),
                model.get("model_name") or ("?" if model.get("problem") else "—"),
                key=member.backend_id,
            )
        if self._members:
            table.move_cursor(row=min(cursor, len(self._members) - 1))
        self.render_breaker_note()
        if check:
            self.check_models()

    def _breaker_rule(self) -> dict[str, Any] | None:
        """The rule as edited; None while the backends load or a number is invalid."""
        if self._breaker is None:
            return None
        try:
            numbers = {
                key: int(self.query_one(f"#{field}", Input).value)
                for key, field in (
                    ("failure_count", "breaker-count"),
                    ("interval_seconds", "breaker-interval"),
                    ("trip_duration_seconds", "breaker-trip"),
                )
            }
        except ValueError:
            return None
        if any(value < 1 for value in numbers.values()):
            return None
        retry_after = self.query_one("#breaker-retry-after", Checkbox).value
        return {**self._breaker, **numbers, "accept_retry_after": retry_after}

    def render_breaker_note(self) -> None:
        note = self.query_one("#breaker-note", Static)
        rule = self._breaker_rule()
        if self._breaker is None or rule is None:
            note.update("")
            return
        ids = [member.backend_id for member in self._members]
        parts = []
        updates = members_to_update(ids, self._backends, rule)
        if updates:
            parts.append(f"Publishing writes it to {len(updates)} member(s).")
        if not self._breaker_uniform:
            parts.append("The members' rules differ today; publishing gives them all this one.")
        external = read_only_members(ids, self._backends)
        if external:
            parts.append(f"{', '.join(external)} weren't created by this app and keep their own.")
        note.update(Text(" ".join(parts), style="#737aa2"))

    def _highlighted(self) -> str | None:
        table = self.query_one("#editor-members", DataTable)
        if not self._members or table.cursor_row < 0:
            return None
        return self._members[min(table.cursor_row, len(self._members) - 1)].backend_id

    # -- editing ----------------------------------------------------------------------

    @on(Select.Changed, "#mode")
    def mode_changed(self, event: Select.Changed) -> None:
        if event.value is Select.NULL or event.value == self._mode:
            return
        self._mode = event.value  # type: ignore[assignment]
        if self._mode != "custom":
            self._members = members_for_mode(self._members, self._mode)
        self.render_members(check=False)

    @on(Input.Changed, "#pool-id, #pool-title")
    def naming_changed(self, event: Input.Changed) -> None:
        if event.input.id == "pool-id":
            self._pool_id = event.value.strip()
        else:
            self._title = event.value.strip()
        if self._creating and not self._path_edited:
            self.query_one("#api-path", Input).value = self._suggested_path()

    @on(Input.Changed, "#breaker-count, #breaker-interval, #breaker-trip")
    @on(Checkbox.Changed, "#breaker-retry-after")
    def breaker_changed(self) -> None:
        self.render_breaker_note()

    @on(Input.Changed, "#api-path")
    def path_changed(self, event: Input.Changed) -> None:
        if event.value != self._suggested_path():
            self._path_edited = True

    def action_add(self) -> None:
        present = {member.backend_id.casefold() for member in self._members}
        options = [
            (backend["id"], f"{backend['id']}  {backend.get('url') or ''}")
            for backend in sorted(self._backends.values(), key=lambda item: item["id"])
            if backend["type"] == "Single" and backend["id"].casefold() not in present
        ]

        def added(backend_id: str | None) -> None:
            if backend_id:
                self._members = add_member(self._members, backend_id, self._mode)
                self.render_members()

        self.app.push_screen(
            PickScreen("Add a member", options, "Member backends that aren't in this pool yet."),
            added,
        )

    def action_remove(self) -> None:
        backend_id = self._highlighted()
        if backend_id:
            self._members = remove_member(self._members, backend_id, self._mode)
            self.render_members()

    def action_primary(self) -> None:
        backend_id = self._highlighted()
        if backend_id:
            self._members = set_primary(self._members, backend_id, self._mode)
            self.render_members(check=False)
            self.query_one("#editor-members", DataTable).move_cursor(row=0)

    def action_weight(self, delta: int) -> None:
        backend_id = self._highlighted()
        if not backend_id:
            return
        if self._mode != "weighted":
            self.notify("Switch the mode to Weighted to change weights.", severity="warning")
            return
        member = next(item for item in self._members if item.backend_id == backend_id)
        self._members = change_weight(self._members, backend_id, member.weight + delta)
        self.render_members(check=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    # -- publish ----------------------------------------------------------------------

    def _problems(self) -> list[str]:
        problems = draft_issues(self._members, self._mode)
        if self._creating and not ID_PATTERN.fullmatch(self._pool_id):
            problems.insert(0, "the pool ID uses letters, digits and hyphens (max 80)")
        if self._creating and self.query_one("#with-api", Checkbox).value:
            path = self.query_one("#api-path", Input).value.strip().strip("/")
            if not API_PATH_PATTERN.fullmatch(path):
                problems.append("the API path uses letters, digits and . _ ~ - separated by /")
        if self._breaker is not None and self._breaker_rule() is None:
            problems.append("the circuit breaker takes whole numbers of 1 or more")
        return problems

    def action_save(self) -> None:
        problems = self._problems()
        error = self.query_one("#editor-error", Static)
        if problems:
            error.update("Can't publish yet: " + "; ".join(problems) + ".")
            return
        error.update("")
        self.publish()

    @work(exclusive=True, group="editor-save")
    async def publish(self) -> None:
        api = tui(self).api
        body: dict[str, Any] = {
            "title": self._title or None,
            # None keeps the stored description (with its ownership marker), as the web UI does.
            "description": None,
            "members": [member.to_json() for member in self._members],
            "session_affinity": self.query_one("#affinity", Checkbox).value,
            # The pool's rule for its managed members; None (backends not loaded) leaves them.
            "circuit_breaker": self._breaker_rule(),
            "etag": (self._pool or {}).get("etag"),
        }
        self.query_one("#save", Button).disabled = True
        try:
            saved = await api.save_pool(self._pool_id, body)
            if self._creating and self.query_one("#with-api", Checkbox).value:
                path = self.query_one("#api-path", Input).value.strip().strip("/")
                try:
                    await api.create_pool_api(saved["id"], {"path": path})
                except ApiError as error:
                    # The pool exists; the API can be retried from the pool view (a).
                    tui(self).show_error(error, "Pool saved, but its API wasn't created")
        except ApiError as error:
            self.query_one("#editor-error", Static).update(error.message)
            self.query_one("#save", Button).disabled = False
            return
        self.notify(f"Published {saved['id']}", severity="information")
        self.dismiss(str(saved["id"]))
