"""The terminal UI: the same pools, deployments and setup as the web UI, in a
keyboard-first Omarchy-style layout. It is only a client of the backend's ``/api``."""

from __future__ import annotations

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import ContentSwitcher, Footer, Static

from .client import ApiClient, ApiError, Json
from .logic import display_user
from .screens.common import HelpScreen
from .theme import OMARCHY
from .views.deployments import DeploymentsView
from .views.pools import PoolsView
from .views.setup import SetupView

VIEWS = (
    ("pools", "Pools"),
    ("deployments", "Deployments"),
    ("setup", "Setup"),
)


class TopBar(Static):
    """Views as workspaces on the left, the APIM service and user on the right."""

    def render_bar(self, active: str, context: Json) -> None:
        bar = Text(no_wrap=True, overflow="ellipsis")
        bar.append(" ◆ apim-backend-pool ", style="bold #7aa2f7")
        for index, (view, label) in enumerate(VIEWS, start=1):
            style = "bold #1a1b26 on #7aa2f7" if view == active else "#737aa2"
            bar.append(f" {index} {label} ", style=style)
            bar.append(" ")
        capabilities = context.get("capabilities") or {}
        report = context.get("setup") or {}
        me = context.get("me") or {}
        bar.append("   ")
        if capabilities:
            bar.append(f"{capabilities.get('apim_name')}", style="bold #c0caf5")
            bar.append(f" · {capabilities.get('resource_group')}", style="#737aa2")
        else:
            bar.append("no APIM service chosen", style="#e0af68")
        attention = sum(
            1 for check in report.get("checks", []) if check["status"] in {"action", "blocked"}
        )
        if report:
            bar.append("   setup ", style="#737aa2")
            bar.append(
                "✓" if report.get("ready") else f"! {attention}",
                style="#9ece6a" if report.get("ready") else "#e0af68",
            )
        if me:
            name = display_user(me, report.get("user"))
            bar.append(f"   {name or 'local (no sign-in)'}", style="#737aa2")
            if not me.get("can_write"):
                bar.append(" (read-only)", style="#e0af68")
        self.update(bar)


class PoolManagerApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "APIM Backend Pool Manager"
    BINDINGS = [
        Binding("1", "view('pools')", "Pools", show=False),
        Binding("2", "view('deployments')", "Deployments", show=False),
        Binding("3", "view('setup')", "Setup", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, api: ApiClient) -> None:
        super().__init__()
        # Registered before the stylesheet is parsed: it uses the theme's own variables.
        self.register_theme(OMARCHY)
        self.theme = OMARCHY.name
        self.api = api
        # Shared by every view: me, capabilities (None until an APIM service is readable), setup.
        self.context: Json = {}

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with ContentSwitcher(initial="pools", id="views"):
            yield PoolsView(id="pools")
            yield DeploymentsView(id="deployments")
            yield SetupView(id="setup")
        yield Footer()

    def on_mount(self) -> None:
        self.load_context(first=True)

    async def on_unmount(self) -> None:
        await self.api.aclose()

    @property
    def active_view(self) -> str:
        return self.query_one("#views", ContentSwitcher).current or "pools"

    def can_operate(self) -> bool:
        return bool((self.context.get("setup") or {}).get("can_operate"))

    def can_write(self) -> bool:
        return bool((self.context.get("me") or {}).get("can_write"))

    def show_error(self, error: Exception, title: str = "Error") -> None:
        message = error.message if isinstance(error, ApiError) else str(error)
        self.notify(message, title=title, severity="error", timeout=8)

    def refresh_bar(self) -> None:
        self.query_one(TopBar).render_bar(self.active_view, self.context)

    @work(exclusive=True, group="context")
    async def load_context(self, first: bool = False, refresh: bool = False) -> None:
        """Who we are, which APIM service, and what setup says; then reload the active view."""
        try:
            setup = await self.api.setup(refresh=refresh)
            me = await self.api.me()
            capabilities: Json | None = None
            if setup.get("can_operate"):
                capabilities = await self.api.capabilities()
        except ApiError as error:
            self.show_error(error, "Cannot load the app")
            return
        self.context = {"setup": setup, "me": me, "capabilities": capabilities}
        self.refresh_bar()
        self.query_one(SetupView).show_report(setup)
        if first and not setup.get("can_operate"):
            # Nothing else works until the APIM service is chosen and readable. The report is
            # already shown, so switch without loading it again (that would cancel this worker).
            self.show_view("setup")
            self.notify(
                "Finish setup first: the app can't use an APIM service yet.", severity="warning"
            )
            return
        self.reload_active()
        if any(check["status"] == "pending" for check in setup.get("checks", [])):
            # A new service or identity is provisioning; follow it.
            self.set_timer(5, lambda: self.load_context(refresh=True))

    def reload_active(self) -> None:
        # The setup view shows the report load_context just read; reloading it would loop.
        if self.active_view == "setup":
            return
        view = self.query_one(f"#{self.active_view}")
        loader = getattr(view, "load", None)
        if callable(loader):
            loader()

    def show_view(self, view: str) -> bool:
        """Switches to ``view``; False if it was already showing."""
        switcher = self.query_one("#views", ContentSwitcher)
        if switcher.current == view:
            return False
        switcher.current = view
        self.refresh_bar()
        focus = getattr(self.query_one(f"#{view}"), "focus_default", None)
        if callable(focus):
            focus()
        return True

    def action_view(self, view: str) -> None:
        if not self.show_view(view):
            return
        if view == "setup":
            self.load_context(refresh=True)
        elif self.can_operate():
            self.reload_active()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
