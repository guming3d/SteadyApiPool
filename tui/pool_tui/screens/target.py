"""Choose the APIM service to manage (local only): tenant → subscription → service.

Creating a service is left to the web UI or Bicep; this picks an existing one, like the web
setup wizard's list, with whether your roles can manage each. Services this app created can be
deleted (d, with the name typed again); Azure keeps them 48 hours, and Recently deleted restores
them."""

from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..base import tui
from ..client import ApiError, Json
from .common import ConfirmScreen

MANAGED = "managed-by: apim-backend-pool"


class DeleteServiceScreen(ModalScreen[str | None]):
    """Shows what deleting removes; dismisses with the typed name once it matches."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, check: Json) -> None:
        super().__init__()
        self._check = check

    def compose(self) -> ComposeResult:
        check = self._check
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = f"Delete APIM service {check['name']}"
            dialog.border_subtitle = "type the name · enter delete · esc cancel"
            loss = Text(f"{check['name']}'s gateway stops at once", style="bold #f7768e")
            loss.append(
                f", and everything in it goes with it: {check['pools']} pools, "
                f"{check['members']} member backends, {check['apis']} APIs, and their "
                "subscriptions and keys. Clients calling it fail immediately.",
                style="#c0caf5",
            )
            if check["current"]:
                loss.append(
                    "\nThis is the service the app manages now; you'll choose another.",
                    style="#e0af68",
                )
            yield Static(loss)
            yield Static(
                "Azure keeps it for 48 hours: restore it from Recently deleted until then. "
                "Its name can't be reused until it's restored or purged.",
                classes="muted",
            )
            yield Label(f"Type {check['name']} to confirm")
            yield Input(placeholder=check["name"], id="confirm-name")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Delete service", id="delete", variant="error", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#confirm-name", Input).focus()

    def _matches(self) -> bool:
        typed = self.query_one("#confirm-name", Input).value.strip()
        return typed.casefold() == str(self._check["name"]).casefold()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#delete", Button).disabled = not self._matches()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._matches():
            self.dismiss(event.value.strip())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "delete" and self._matches():
            self.dismiss(self.query_one("#confirm-name", Input).value.strip())
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TargetScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("s", "sign_in", "Sign in to this tenant"),
        Binding("d", "delete", "Delete service"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._tenant: str | None = None
        self._subscription: str | None = None
        self._services: dict[str, Json] = {}
        self._deleted: dict[str, Json] = {}
        # The managed service was deleted, so the caller reloads setup either way.
        self._changed = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide") as dialog:
            dialog.border_title = "Choose the APIM service"
            dialog.border_subtitle = "enter select · d delete · s sign in to tenant · esc cancel"
            yield Static("", id="target-state", classes="muted")
            yield Label("Directory (tenant)")
            yield OptionList(id="tenants")
            yield Label("Subscription")
            yield OptionList(id="subscriptions")
            yield Label("APIM service")
            yield OptionList(id="services")
            yield Label(
                "Recently deleted (Azure keeps them 48 hours; enter restores)", id="deleted-label"
            )
            yield OptionList(id="deleted")

    def on_mount(self) -> None:
        self.query_one("#tenants", OptionList).focus()
        self._show_deleted(False)
        self.load_tenants()

    def _show_deleted(self, visible: bool) -> None:
        self.query_one("#deleted-label").display = visible
        self.query_one("#deleted", OptionList).display = visible

    def _state(self, message: str, style: str = "#737aa2") -> None:
        self.query_one("#target-state", Static).update(Text(message, style=style))

    @work(exclusive=True, group="tenants")
    async def load_tenants(self) -> None:
        self._state("Loading your tenants…")
        try:
            tenants = await tui(self).api.tenants()
        except ApiError as error:
            self._state(error.message, "#f7768e")
            return
        options = self.query_one("#tenants", OptionList)
        options.clear_options()
        options.add_options(
            Option(
                f"{t.get('display_name') or t['tenant_id']}  {t.get('default_domain') or ''}",
                id=t["tenant_id"],
            )
            for t in tenants
        )
        self._state("Choose a tenant.")
        if len(tenants) == 1:
            self._tenant = tenants[0]["tenant_id"]
            self.load_subscriptions()

    @on(OptionList.OptionSelected, "#tenants")
    def tenant_chosen(self, event: OptionList.OptionSelected) -> None:
        self._tenant = event.option.id
        self.load_subscriptions()

    @work(exclusive=True, group="subscriptions")
    async def load_subscriptions(self) -> None:
        options = self.query_one("#subscriptions", OptionList)
        options.clear_options()
        self.query_one("#services", OptionList).clear_options()
        self._state("Loading subscriptions…")
        try:
            subscriptions = await tui(self).api.subscriptions(self._tenant)
        except ApiError as error:
            hint = "  Press s to sign in to this tenant." if error.status == 401 else ""
            self._state(error.message + hint, "#e0af68")
            return
        options.add_options(
            Option(
                f"{item['display_name']}"
                + ("" if item["state"] == "Enabled" else f"  ({item['state']})"),
                id=item["subscription_id"],
                disabled=item["state"] != "Enabled",
            )
            for item in subscriptions
        )
        self._state(
            "Choose a subscription." if subscriptions else "No subscriptions in this tenant."
        )
        options.focus()
        enabled = [item for item in subscriptions if item["state"] == "Enabled"]
        if len(enabled) == 1:
            self._subscription = enabled[0]["subscription_id"]
            self.load_services()

    @on(OptionList.OptionSelected, "#subscriptions")
    def subscription_chosen(self, event: OptionList.OptionSelected) -> None:
        self._subscription = event.option.id
        self.load_services()

    @work(exclusive=True, group="services")
    async def load_services(self) -> None:
        if not self._subscription:
            return
        options = self.query_one("#services", OptionList)
        options.clear_options()
        self._state("Loading APIM services…")
        try:
            services = await tui(self).api.services(self._tenant, self._subscription)
        except ApiError as error:
            self._state(error.message, "#f7768e")
            return
        self._services = {service["id"]: service for service in services}
        for service in services:
            label = Text(service["name"], style="bold")
            label.append(
                f"  {service['resource_group']} · {service['location']} · {service['sku']}",
                style="#737aa2",
            )
            if service.get("provisioning_state") not in {None, "Succeeded"}:
                label.append(f" · {service['provisioning_state']}", style="#e0af68")
            if service.get("managed"):
                label.append("  created here", style="#bb9af7")
            if service.get("can_manage") is True:
                label.append("  ✓ you can manage it", style="#9ece6a")
            elif service.get("can_manage") is False:
                label.append("  ! missing permissions", style="#e0af68")
            options.add_option(Option(label, id=service["id"]))
        self._state(
            "Choose the service to manage."
            if services
            else "No APIM service in this subscription. "
            "Create one from the web UI or with azd (Bicep)."
        )
        options.focus()
        self.load_deleted()

    @work(exclusive=True, group="deleted")
    async def load_deleted(self) -> None:
        if not self._subscription:
            return
        try:
            deleted = await tui(self).api.deleted_services(self._tenant, self._subscription)
        except ApiError:
            # Listing deleted services is a convenience; the picker works without it.
            self._show_deleted(False)
            return
        self._deleted = {f"{item['location']}/{item['name']}": item for item in deleted}
        options = self.query_one("#deleted", OptionList)
        options.clear_options()
        for key, item in self._deleted.items():
            label = Text(item["name"], style="bold")
            label.append(
                f"  {item['resource_group']} · {item['display_location']}", style="#737aa2"
            )
            label.append(
                f" · restore until {item.get('scheduled_purge_date') or '?'}", style="#e0af68"
            )
            options.add_option(Option(label, id=key))
        self._show_deleted(bool(deleted))

    def _target(self, service: Json) -> Json:
        return {
            "tenant_id": self._tenant,
            "subscription_id": self._subscription,
            "resource_group": service["resource_group"],
            "apim_name": service["name"],
        }

    def action_delete(self) -> None:
        options = self.query_one("#services", OptionList)
        if options.highlighted is None or not self._subscription:
            return
        service = self._services.get(options.get_option_at_index(options.highlighted).id or "")
        if not service:
            return
        if not service.get("managed"):
            self.notify(
                f"Only services this app created ({MANAGED}) can be deleted here.",
                severity="warning",
            )
            return
        self.check_delete(service)

    @work(exclusive=True, group="delete")
    async def check_delete(self, service: Json) -> None:
        try:
            check = await tui(self).api.delete_check(self._target(service))
        except ApiError as error:
            self._state(error.message, "#f7768e")
            return

        def confirmed(typed: str | None) -> None:
            if typed:
                self.delete(service, check, typed)

        self.app.push_screen(DeleteServiceScreen(check), confirmed)

    @work(exclusive=True, group="delete")
    async def delete(self, service: Json, check: Json, typed: str) -> None:
        self._state(f"Deleting {service['name']}…", "#e0af68")
        try:
            await tui(self).api.delete_service({**self._target(service), "confirm_name": typed})
        except ApiError as error:
            self._state(error.message, "#f7768e")
            return
        self._changed = self._changed or bool(check["current"])
        self.notify(f"Deleted {service['name']}. Azure keeps it 48 hours; restore it below.")
        self.load_services()

    @on(OptionList.OptionSelected, "#deleted")
    def deleted_chosen(self, event: OptionList.OptionSelected) -> None:
        item = self._deleted.get(event.option.id or "")
        if not item:
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.restore(item)

        self.app.push_screen(
            ConfirmScreen(
                f"Restore {item['name']}",
                f"Restore {item['name']} into {item['resource_group']}, with everything it "
                "contained when it was deleted?",
                "Restore",
            ),
            confirmed,
        )

    @work(exclusive=True, group="restore")
    async def restore(self, item: Json) -> None:
        try:
            await tui(self).api.restore_service(
                {
                    "tenant_id": self._tenant,
                    "subscription_id": self._subscription,
                    "location": item["location"],
                    "name": item["name"],
                }
            )
        except ApiError as error:
            self._state(error.message, "#f7768e")
            return
        self.notify(f"{item['name']} is being restored.")
        self.load_services()

    @on(OptionList.OptionSelected, "#services")
    def service_chosen(self, event: OptionList.OptionSelected) -> None:
        service = self._services.get(event.option.id or "")
        if service and self._subscription:
            self.select(service)

    @work(exclusive=True, group="select")
    async def select(self, service: Json) -> None:
        try:
            await tui(self).api.select_target(
                {
                    "tenant_id": self._tenant,
                    "subscription_id": self._subscription,
                    "resource_group": service["resource_group"],
                    "apim_name": service["name"],
                }
            )
        except ApiError as error:
            self._state(error.message, "#f7768e")
            return
        self.notify(f"Now managing {service['name']}")
        self.dismiss(True)

    @work(exclusive=True, group="sign-in")
    async def action_sign_in(self) -> None:
        self._state("Complete the sign-in in the browser window that opened…", "#7dcfff")
        try:
            await tui(self).api.sign_in(self._tenant)
        except ApiError as error:
            self._state(error.message, "#f7768e")
            return
        self.load_subscriptions()

    def action_cancel(self) -> None:
        self.dismiss(self._changed)
