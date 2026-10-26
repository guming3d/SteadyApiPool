"""Setup: the same checks as the web wizard, with the fixes your own Azure rights allow."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from ..base import tui
from ..checks import FIXES, ICONS, STEP_OF_KIND, needs_attention, sign_in_lines, title
from ..client import ApiError, Json
from ..screens.target import TargetScreen

STATUS_STYLE = {
    "ok": "#9ece6a",
    "action": "#e0af68",
    "blocked": "#f7768e",
    "pending": "#7dcfff",
    "unknown": "#737aa2",
    "skipped": "#737aa2",
}


class SetupView(Vertical):
    BINDINGS = [
        Binding("enter", "fix", "Fix", priority=False),
        Binding("c", "choose_apim", "Choose APIM"),
        # For machines without a local browser: SSH, Linux servers, WSL, containers.
        Binding("d", "device_code", "Sign in with a code"),
        Binding("o", "sign_out", "Sign out"),
        Binding("r", "reload", "Check again"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._report: Json = {}
        # The device code being followed, shown under the sign-in check until it finishes.
        self._device: Json | None = None

    def compose(self) -> ComposeResult:
        checks = Vertical(
            DataTable(id="setup-checks", cursor_type="row"), classes="pane", id="setup-checks-pane"
        )
        checks.border_title = "Setup"
        detail = Vertical(Static("", id="setup-detail"), classes="pane", id="setup-detail-pane")
        detail.border_title = "Details"
        yield checks
        yield detail

    def on_mount(self) -> None:
        self.query_one("#setup-checks", DataTable).add_columns(" ", "Step", "Check", "Fix")

    def focus_default(self) -> None:
        self.query_one("#setup-checks", DataTable).focus()

    def action_reload(self) -> None:
        tui(self).load_context(refresh=True)

    def _apim(self) -> str:
        return ((tui(self).context.get("capabilities") or {}).get("apim_name")) or "APIM"

    def show_report(self, report: Json) -> None:
        self._report = report
        table = self.query_one("#setup-checks", DataTable)
        cursor = table.cursor_row
        table.clear()
        for check in report.get("checks", []):
            heading, _ = title(check, report, self._apim())
            status = check["status"]
            fix = FIXES.get(check.get("fix") or "", "")
            table.add_row(
                Text(ICONS.get(status, "?"), style=STATUS_STYLE.get(status, "")),
                STEP_OF_KIND.get(check["kind"], check["kind"]),
                heading,
                Text(fix, style="#7aa2f7") if fix and needs_attention(check) else "",
                key=check["id"],
            )
        count = sum(1 for check in report.get("checks", []) if needs_attention(check))
        pane = self.query_one("#setup-checks-pane")
        pane.border_subtitle = "all set" if report.get("ready") else f"{count} need attention"
        if report.get("checks"):
            table.move_cursor(row=min(max(cursor, 0), len(report["checks"]) - 1))
            self.show_detail(self._current())

    def _current(self) -> Json | None:
        checks = self._report.get("checks", [])
        table = self.query_one("#setup-checks", DataTable)
        return checks[min(max(table.cursor_row, 0), len(checks) - 1)] if checks else None

    @on(DataTable.RowHighlighted, "#setup-checks")
    def highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.show_detail(self._current())

    @on(DataTable.RowSelected, "#setup-checks")
    def selected(self, event: DataTable.RowSelected) -> None:
        self.action_fix()

    def show_detail(self, check: Json | None) -> None:
        detail = self.query_one("#setup-detail", Static)
        if check is None:
            detail.update("")
            return
        heading, hint = title(check, self._report, self._apim())
        text = Text(heading, style="bold")
        if check.get("subject"):
            text.append(f"\n{check['subject']}", style="#737aa2")
        if hint and check["status"] not in {"ok", "skipped"}:
            text.append(f"\n{hint}", style="#c0caf5")
        if check.get("detail") and check["status"] != "ok":
            text.append(f"\n{check['detail']}", style="#737aa2")
        if check.get("fix") and needs_attention(check):
            text.append(f"\nPress enter: {FIXES.get(check['fix'], check['fix'])}.", style="#7aa2f7")
        if check["kind"] == "sign_in":
            self._sign_in_detail(text, check)
        if check.get("missing") and needs_attention(check):
            text.append("\nMissing permissions:", style="#e0af68")
            for action in check["missing"]:
                text.append(f"\n  {action}", style="#737aa2")
        request = check.get("request")
        if request and check["status"] != "ok":
            text.append(
                "\nAsk an administrator"
                + ("" if check.get("fix") else " (you can't change this yourself)")
                + ":",
                style="#e0af68",
            )
            if request.get("role"):
                text.append(f"\n  {request['role']} for {request['assignee']}", style="#c0caf5")
                if request.get("scope"):
                    text.append(f"\n  on {request['scope']}", style="#737aa2")
            text.append(f"\n  {request['command']}", style="#7dcfff")
        detail.update(text)

    def _sign_in_detail(self, text: Text, check: Json) -> None:
        local = self._report.get("local_sign_in")
        if local is None:
            return
        for line in sign_in_lines(local):
            text.append(f"\n{line}", style="#737aa2")
        if needs_attention(check):
            text.append(
                "\nPress d to sign in with a code on another device instead (no browser here,"
                " e.g. over SSH).",
                style="#7aa2f7",
            )
        if local.get("source") in {"browser", "device_code"}:
            text.append("\nPress o to sign out of it.", style="#7aa2f7")
        if self._device:
            expires = time.strftime("%H:%M", time.localtime(self._device["expires_on"]))
            text.append(f"\n\nOpen {self._device['verification_uri']} and enter", style="#c0caf5")
            text.append(f"  {self._device['user_code']}  ", style="bold #1a1b26 on #7aa2f7")
            text.append(
                f"\nWaiting for the sign-in; the code expires at {expires}.", style="#737aa2"
            )

    def _local(self) -> bool:
        if self._report.get("mode") == "local":
            return True
        self.notify("Only the local app signs in itself.", severity="warning")
        return False

    def action_device_code(self) -> None:
        if self._local():
            self.follow_device_code()

    @work(exclusive=True, group="device-code")
    async def follow_device_code(self) -> None:
        api = tui(self).api
        try:
            self._device = await api.start_device_code()
            self.show_detail(self._current())
            self.notify(
                f"Open {self._device['verification_uri']} on any device and enter"
                f" {self._device['user_code']}.",
                timeout=30,
            )
            while True:
                await asyncio.sleep(2)
                status = await api.device_code_status()
                if status["state"] != "pending":
                    break
        except ApiError as error:
            tui(self).show_error(error, "The device-code sign-in didn't start")
            return
        finally:
            self._device = None
        if status["state"] == "done":
            self.notify("Signed in.")
            tui(self).load_context(refresh=True)
        else:
            self.notify(
                f"The device-code sign-in didn't finish: {status.get('error') or 'unknown error'}",
                severity="error",
                timeout=15,
            )
            self.show_detail(self._current())

    @work(exclusive=True, group="fix")
    async def action_sign_out(self) -> None:
        local = self._report.get("local_sign_in") or {}
        if not self._local():
            return
        if local.get("source") not in {"browser", "device_code"}:
            self.notify(
                "The app isn't using its own sign-in; sign out of the Azure CLI, azd or"
                " PowerShell there.",
                severity="warning",
            )
            return
        try:
            await tui(self).api.sign_out()
        except ApiError as error:
            tui(self).show_error(error, "Sign-out didn't work")
            return
        self.notify("Signed out of the app's own sign-in.")
        tui(self).load_context(refresh=True)

    def action_choose_apim(self) -> None:
        target = self._report.get("mode") == "local"
        if not target:
            self.notify(
                "The deployed app manages the APIM service set at deployment.", severity="warning"
            )
            return

        def done(changed: bool | None) -> None:
            if changed:
                tui(self).load_context(refresh=True)

        self.app.push_screen(TargetScreen(), done)

    def action_fix(self) -> None:
        check = self._current()
        if not check or not check.get("fix") or not needs_attention(check):
            return
        fix = check["fix"]
        if fix == "select_apim":
            self.action_choose_apim()
        elif fix in {"consent", "refresh_token"}:
            self.notify(
                "Open the web app in the browser to allow Azure access; the TUI can't.",
                severity="warning",
            )
        else:
            self.run_fix(check)

    @work(exclusive=True, group="fix")
    async def run_fix(self, check: Json) -> None:
        api = tui(self).api
        fix = check["fix"]
        try:
            if fix == "sign_in":
                self.notify("Complete the sign-in in the browser window that opened.")
                await api.sign_in()
            elif fix == "enable_apim_identity":
                await api.enable_apim_identity()
                self.notify("APIM is turning its managed identity on (a minute or two).")
            elif fix == "grant_foundry_access":
                await api.grant_foundry_access([check["subject"]])
                self.notify("Granted. Azure can take up to 10 minutes to apply a new role.")
        except ApiError as error:
            tui(self).show_error(error, "The fix didn't work")
            return
        tui(self).load_context(refresh=True)
