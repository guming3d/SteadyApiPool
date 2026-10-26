"""Foundry deployments discovered across the configured subscriptions, grouped by model.
Select some and open a new pool with them, or deploy another model into a Foundry account."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable

from ..base import tui
from ..client import ApiError, Json
from ..logic import suggest_pool_id
from ..screens.deploy_model import DeployModelScreen
from ..screens.pool_editor import PoolEditorScreen


def _key(deployment: Json) -> str:
    return f"{deployment['account_id']}/deployments/{deployment['deployment']}"


class DeploymentsView(Vertical):
    BINDINGS = [
        Binding("space", "pick", "Select"),
        Binding("c", "create_pool", "Import + create pool"),
        Binding("d", "deploy", "Deploy a model"),
        Binding("r", "reload", "Refresh"),
        Binding("R", "rescan", "Rescan Azure"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._deployments: list[Json] = []
        self._selected: set[str] = set()

    def compose(self) -> ComposeResult:
        pane = Vertical(
            DataTable(id="deployment-list", cursor_type="row"),
            classes="pane grow",
            id="deployments-pane",
        )
        pane.border_title = "Foundry deployments"
        yield pane

    def on_mount(self) -> None:
        self.query_one("#deployment-list", DataTable).add_columns(
            " ",
            "Model",
            "Version",
            "Deployment",
            "Account",
            "Region",
            "SKU",
            "Capacity",
            "Member backend",
        )

    def focus_default(self) -> None:
        self.query_one("#deployment-list", DataTable).focus()

    def load(self) -> None:
        self.load_deployments()

    def action_reload(self) -> None:
        self.load_deployments()

    def action_rescan(self) -> None:
        self.load_deployments(refresh=True)

    @work(exclusive=True, group="deployments")
    async def load_deployments(self, refresh: bool = False) -> None:
        pane = self.query_one("#deployments-pane")
        pane.border_subtitle = "scanning…"
        try:
            deployments = await tui(self).api.deployments(refresh=refresh)
        except ApiError as error:
            pane.border_subtitle = ""
            tui(self).show_error(error, "Cannot list deployments")
            return
        self._deployments = sorted(
            deployments,
            key=lambda item: (
                item["model_name"].casefold(),
                item["account_name"],
                item["deployment"],
            ),
        )
        self._selected &= {_key(item) for item in self._deployments}
        self.render_table()

    def render_table(self) -> None:
        table = self.query_one("#deployment-list", DataTable)
        cursor = table.cursor_row
        table.clear()
        previous = None
        for item in self._deployments:
            model = item["model_name"] if item["model_name"] != previous else ""
            previous = item["model_name"]
            table.add_row(
                Text("●", style="#7aa2f7") if _key(item) in self._selected else " ",
                Text(model, style="bold #7dcfff"),
                item["model_version"],
                item["deployment"],
                item["account_name"],
                item["location"],
                item["sku"],
                "" if item["capacity"] is None else str(item["capacity"]),
                Text(item["backend_id"], style="#9ece6a")
                if item.get("backend_id")
                else Text("—", style="#737aa2"),
                key=_key(item),
            )
        if self._deployments:
            table.move_cursor(row=min(max(cursor, 0), len(self._deployments) - 1))
        count = len(self._selected)
        self.query_one("#deployments-pane").border_subtitle = (
            f"{count} selected · c create pool" if count else f"{len(self._deployments)} · d deploy"
        )

    @on(DataTable.RowSelected, "#deployment-list")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_pick()

    def action_pick(self) -> None:
        table = self.query_one("#deployment-list", DataTable)
        if not self._deployments:
            return
        key = _key(self._deployments[min(max(table.cursor_row, 0), len(self._deployments) - 1)])
        self._selected ^= {key}
        self.render_table()

    def _chosen(self) -> list[Json]:
        if self._selected:
            return [item for item in self._deployments if _key(item) in self._selected]
        table = self.query_one("#deployment-list", DataTable)
        return (
            [self._deployments[table.cursor_row]]
            if self._deployments and table.cursor_row >= 0
            else []
        )

    def action_create_pool(self) -> None:
        # Importing on its own would leave backends no pool uses, so it always opens a pool.
        if tui(self).can_write():
            self.import_selected(create_pool=True)

    def action_deploy(self) -> None:
        taken: dict[str, list[str]] = {}
        for item in self._deployments:
            taken.setdefault(item["account_id"].casefold(), []).append(item["deployment"])

        def done(name: str | None) -> None:
            if name:
                self.notify(f"Deployed {name}; you can now create a pool with it.")
                self.load_deployments(refresh=True)

        self.app.push_screen(DeployModelScreen(taken), done)

    @work(exclusive=True, group="import")
    async def import_selected(self, create_pool: bool) -> None:
        chosen = self._chosen()
        if not chosen:
            return
        models = {item["model_name"].casefold() for item in chosen}
        if create_pool and len(models) > 1:
            self.notify(
                "A pool's members must serve the same model; select one model.", severity="warning"
            )
            return
        try:
            result = await tui(self).api.import_deployments(
                [
                    {"account_id": item["account_id"], "deployment": item["deployment"]}
                    for item in chosen
                ]
            )
        except ApiError as error:
            tui(self).show_error(error, "Cannot import")
            return
        created = len(result["created"])
        self.notify(
            f"{created} member backend{'s' if created != 1 else ''} created, "
            f"{len(result['backend_ids']) - created} already present."
        )
        self._selected.clear()
        self.load_deployments()
        if create_pool:
            model = chosen[0]["model_name"]
            prefix = (tui(self).context.get("capabilities") or {}).get("id_prefix", "pool-")

            def done(pool_id: str | None) -> None:
                if pool_id:
                    tui(self).action_view("pools")
                    tui(self).query_one("#pools").load(pool_id)  # type: ignore[attr-defined]

            self.app.push_screen(
                PoolEditorScreen(
                    members=result["backend_ids"],
                    pool_id=suggest_pool_id(model, prefix),
                    title=model,
                ),
                done,
            )
