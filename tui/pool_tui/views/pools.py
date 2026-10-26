"""Pools: the list on the left; summary, members, bound APIs and a compact monitor on the right."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Label, Static

from ..base import tui
from ..client import ApiError, Json
from ..logic import (
    API_PATH_PATTERN,
    COGNITIVE_SERVICES_RESOURCE,
    Member,
    availability_tone,
    bar,
    compact,
    deployment_mode,
    ms,
    percent,
    primary_first,
    role,
    share_tone,
    spark,
    suggest_api_path,
    throttle_tone,
    traffic_shares,
)
from ..screens.common import ConfirmScreen, PickScreen, PromptScreen
from ..screens.pool_editor import PoolEditorScreen
from ..screens.try_api import TryApiScreen

RANGES = ("1h", "6h", "24h", "7d")
TONE_STYLE = {"ok": "#9ece6a", "warn": "#e0af68", "bad": "#f7768e"}
TONE_ICON = {"ok": "", "warn": " ▲", "bad": " ●"}
# The app's default retry on 429, as in the web UI.
DEFAULT_BINDING = {
    "retry_on_429": True,
    "max_attempts_per_request": 2,
    "retry_interval_seconds": 1,
    "first_fast_retry": True,
    "backend_timeout_seconds": 120,
}


class TextSpark(Static):
    """A zero-based text sparkline that fits its width and labels its peak."""

    def __init__(self, unit: str, style: str, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._values: list[float] = []
        self._unit = unit
        self._style = style

    def set_values(self, values: list[float]) -> None:
        self._values = values
        self.draw()

    def on_resize(self) -> None:
        self.draw()

    def draw(self) -> None:
        if not any(self._values):
            self.update(Text("no data in this range", style="#737aa2"))
            return
        peak = max(self._values)
        label = f"  peak {ms(peak) if self._unit == 'ms' else compact(peak)}"
        line = Text(spark(self._values, max(10, self.size.width - len(label))), style=self._style)
        line.append(label, style="#737aa2")
        self.update(line)


def _pane(title: str, *children: Any, id: str, classes: str = "pane") -> Vertical:
    pane = Vertical(*children, id=id, classes=classes)
    pane.border_title = title
    return pane


class PoolsView(Horizontal):
    BINDINGS = [
        Binding("n", "new", "New pool"),
        Binding("e", "edit", "Edit"),
        Binding("D", "delete", "Delete"),
        Binding("a", "create_api", "Create API"),
        Binding("b", "bind_api", "Bind API"),
        Binding("u", "unbind_api", "Unbind"),
        Binding("t", "try_api", "Try API"),
        Binding("m", "range", "Range"),
        Binding("r", "reload", "Refresh"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pools: list[Json] = []
        self._pool: Json | None = None
        self._apis: list[Json] = []
        self._bound: list[Json] = []
        self._backends: dict[str, Json] = {}
        self._models: dict[str, Json] = {}
        self._model_name: str | None = None
        self._range = "1h"

    def compose(self) -> ComposeResult:
        yield _pane("Pools", DataTable(id="pool-list", cursor_type="row"), id="pool-list-pane")
        with VerticalScroll(id="pool-detail"):
            yield _pane(
                "Summary", Static("Choose a pool.", id="pool-summary"), id="pool-summary-pane"
            )
            yield _pane(
                "Members", DataTable(id="pool-members", cursor_type="row"), id="pool-members-pane"
            )
            yield _pane("APIs", DataTable(id="pool-apis", cursor_type="row"), id="pool-apis-pane")
            yield _pane(
                f"Monitor · {self._range}",
                Static("", id="monitor-tiles"),
                Horizontal(
                    Label("Requests"),
                    TextSpark("count", "#7aa2f7", id="requests-spark"),
                    classes="spark-row",
                ),
                Horizontal(
                    Label("Gateway latency"),
                    TextSpark("ms", "#bb9af7", id="latency-spark"),
                    classes="spark-row",
                ),
                DataTable(id="monitor-members", cursor_type="row"),
                Static("", id="monitor-note", classes="muted"),
                id="pool-monitor-pane",
            )

    def on_mount(self) -> None:
        self.query_one("#pool-list", DataTable).add_columns("Pool", "Mode", "Members")
        self.query_one("#pool-members", DataTable).add_columns(
            "#", "Member", "Role", "Weight", "Share", "Model", "Auth"
        )
        self.query_one("#pool-apis", DataTable).add_columns(
            "API", "Endpoint", "429 retry", "Identity"
        )
        self.query_one("#monitor-members", DataTable).add_columns(
            "Member",
            "Share (actual / expected)",
            "Requests",
            "429",
            "5xx",
            "Tokens",
            "Latency",
            "PTU",
        )
        # Platform metrics land every minute.
        self.set_interval(60, self._tick)

    def focus_default(self) -> None:
        self.query_one("#pool-list", DataTable).focus()

    def _tick(self) -> None:
        if self._pool and tui(self).active_view == "pools" and tui(self).can_operate():
            self.load_metrics()

    # -- loading ------------------------------------------------------------------------

    def load(self, select: str | None = None) -> None:
        self.load_pools(select)

    def action_reload(self) -> None:
        self.load_pools(self._pool["id"] if self._pool else None)

    @work(exclusive=True, group="pools")
    async def load_pools(self, select: str | None = None) -> None:
        api = tui(self).api
        try:
            pools, backends, apis = await api.pools(), await api.backends(), await api.apis()
        except ApiError as error:
            tui(self).show_error(error, "Cannot list pools")
            return
        self._pools = pools
        self._backends = {backend["id"].casefold(): backend for backend in backends}
        self._apis = apis
        table = self.query_one("#pool-list", DataTable)
        table.clear()
        for pool in pools:
            members = primary_first([Member.from_json(item) for item in pool["members"]])
            name = Text(pool.get("title") or pool["id"])
            if pool.get("protected"):
                name.append(" (read-only)", style="#737aa2")
            table.add_row(name, deployment_mode(members), str(len(members)), key=pool["id"])
        self.query_one("#pool-list-pane").border_subtitle = str(len(pools))
        if not pools:
            self._pool = None
            self.query_one("#pool-summary", Static).update(
                Text(
                    "No pools yet. Press n to create one, or import deployments (3).",
                    style="#737aa2",
                )
            )
            return
        ids = [pool["id"] for pool in pools]
        index = ids.index(select) if select in ids else min(table.cursor_row, len(ids) - 1)
        table.move_cursor(row=max(index, 0))
        self.load_pool(ids[max(index, 0)])

    @on(DataTable.RowHighlighted, "#pool-list")
    def pool_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Rebuilding the list highlights row 0 before the cursor moves back; skip stale events.
        if event.cursor_row != event.data_table.cursor_row:
            return
        pool_id = event.row_key.value if event.row_key else None
        if pool_id and (not self._pool or self._pool["id"] != pool_id):
            self.load_pool(pool_id)

    @work(exclusive=True, group="pool")
    async def load_pool(self, pool_id: str) -> None:
        api = tui(self).api
        try:
            pool = await api.pool(pool_id)
        except ApiError as error:
            tui(self).show_error(error, f"Cannot read {pool_id}")
            return
        self._pool = pool
        self._bound = [
            item
            for item in self._apis
            if (item.get("binding") or {}).get("pool_id", "").casefold() == pool_id.casefold()
        ]
        self._models = {}
        self.render_pool()
        self.load_metrics()
        try:
            check = await api.check_models([member["backend_id"] for member in pool["members"]])
        except ApiError:
            return
        self._models = {item["backend_id"].casefold(): item for item in check["members"]}
        self._model_name = check["model_names"][0] if check["model_names"] else None
        self.render_pool()

    # -- rendering ----------------------------------------------------------------------

    def _endpoint(self, path: str) -> str:
        gateway = (tui(self).context.get("capabilities") or {}).get(
            "gateway_url"
        ) or "https://<apim>.azure-api.net"
        return f"{gateway.rstrip('/')}/{path.strip('/')}"

    def render_pool(self) -> None:
        pool = self._pool
        if pool is None:
            return
        members = primary_first([Member.from_json(item) for item in pool["members"]])
        mode = deployment_mode(members)
        summary = Text()
        summary.append(pool.get("title") or pool["id"], style="bold")
        summary.append(f"  {pool['id']}", style="#737aa2")
        if pool.get("protected"):
            summary.append("  read-only: owned by another control plane", style="#e0af68")
        summary.append("\n")
        summary.append(f"{mode.capitalize()} · {len(members)} members", style="#c0caf5")
        if self._model_name:
            summary.append(f" · model {self._model_name}", style="#7dcfff")
        if pool.get("session_affinity"):
            summary.append(" · session affinity", style="#bb9af7")
        for item in self._bound:
            summary.append(f"\n{self._endpoint(item['path'])}", style="#7aa2f7")
        if not self._bound:
            summary.append(
                "\nNo API bound: the pool takes no traffic yet. Press a to create one.",
                style="#e0af68",
            )
        self.query_one("#pool-summary", Static).update(summary)

        table = self.query_one("#pool-members", DataTable)
        table.clear()
        shares = traffic_shares(members)
        for index, member in enumerate(members):
            backend = self._backends.get(member.backend_id.casefold()) or {}
            model = self._models.get(member.backend_id.casefold()) or {}
            breaker = "" if backend.get("has_circuit_breaker", True) else "  no breaker"
            table.add_row(
                str(index + 1),
                member.backend_id,
                role(members, index),
                str(member.weight),
                percent(shares[index], 0),
                model.get("model_name") or ("?" if model.get("problem") else "—"),
                f"{backend.get('auth_type', '?')}{breaker}",
                key=member.backend_id,
            )
        apis = self.query_one("#pool-apis", DataTable)
        apis.clear()
        for item in self._bound:
            binding = item.get("binding") or {}
            retry = (
                f"×{binding.get('max_attempts_per_request')}"
                if binding.get("retry_on_429")
                else "off"
            )
            apis.add_row(
                item["display_name"],
                self._endpoint(item["path"]),
                retry,
                "✓" if binding.get("managed_identity_resource") else "—",
                key=item["id"],
            )

    @work(exclusive=True, group="metrics")
    async def load_metrics(self, refresh: bool = False) -> None:
        if self._pool is None:
            return
        pane = self.query_one("#pool-monitor-pane")
        pane.border_title = f"Monitor · {self._range}"
        try:
            metrics = await tui(self).api.pool_metrics(self._pool["id"], self._range, refresh)
        except ApiError as error:
            self.query_one("#monitor-tiles", Static).update(Text(error.message, style="#f7768e"))
            return
        if self._pool is None or metrics["pool_id"].casefold() != self._pool["id"].casefold():
            return
        self.render_metrics(metrics)

    def render_metrics(self, metrics: Json) -> None:
        tiles = self.query_one("#monitor-tiles", Static)
        gateway = metrics.get("gateway")
        if metrics.get("gateway_error"):
            tiles.update(Text(metrics["gateway_error"], style="#e0af68"))
        elif not metrics.get("api_ids"):
            tiles.update(
                Text(
                    "No API is bound, so no client traffic. Member deployments below.",
                    style="#737aa2",
                )
            )
        elif gateway is not None:
            requests = gateway["requests"]
            line = Text()

            def tile(label: str, value: str, tone: str = "ok", sub: str = "") -> None:
                line.append(f"{label} ", style="#737aa2")
                line.append(value, style="bold #c0caf5")
                line.append(TONE_ICON[tone], style=TONE_STYLE[tone])
                if sub:
                    line.append(f" {sub}", style="#737aa2")
                line.append("    ")

            tile("Requests", compact(requests))
            tile(
                "Availability",
                percent(gateway["availability"], 2),
                availability_tone(gateway["availability"]),
                f"({gateway['server_errors']} 5xx)",
            )
            tile(
                "429 to clients",
                compact(gateway["throttled"]),
                throttle_tone(gateway["throttled"], requests),
                f"({percent(gateway['throttled'] / requests)})" if requests else "",
            )
            tile(
                "Latency",
                ms(gateway["duration_ms"]),
                sub=f"(backend {ms(gateway['backend_duration_ms'])})",
            )
            tiles.update(line)
        traffic = (gateway or {}).get("traffic") or []
        latency = (gateway or {}).get("latency") or []
        self.query_one("#requests-spark", TextSpark).set_values(
            [
                float(p["success"] + p["throttled"] + p["client_errors"] + p["server_errors"])
                for p in traffic
            ]
        )
        self.query_one("#latency-spark", TextSpark).set_values(
            [float(p["overall_ms"] or 0) for p in latency]
        )

        table = self.query_one("#monitor-members", DataTable)
        table.clear()
        members = metrics.get("members") or []
        total = sum(member.get("requests") or 0 for member in members)
        for member in members:
            if not member.get("deployment_id"):
                table.add_row(
                    member["backend_id"],
                    Text("not a Foundry deployment", style="#737aa2"),
                    *[""] * 6,
                )
                continue
            if member.get("error"):
                table.add_row(
                    member["backend_id"], Text(member["error"], style="#e0af68"), *[""] * 6
                )
                continue
            actual = (member["requests"] or 0) / total if total else None
            tone = share_tone(actual, member["expected_share"])
            share = Text(bar(actual), style="#7aa2f7")
            share.append(
                f" {percent(actual, 0)} / {percent(member['expected_share'], 0)}",
                style=TONE_STYLE[tone] if tone != "ok" else "#c0caf5",
            )
            share.append(TONE_ICON[tone], style=TONE_STYLE[tone])
            throttled_tone = throttle_tone(member["throttled"] or 0, member["requests"] or 0)
            throttled = Text(
                compact(member["throttled"]),
                style=TONE_STYLE[throttled_tone] if throttled_tone != "ok" else "",
            )
            table.add_row(
                member["backend_id"],
                share,
                compact(member["requests"]),
                throttled,
                compact(member["server_errors"]),
                compact(member["tokens"]),
                ms(member["latency_ms"]),
                "—"
                if member["utilization_pct"] is None
                else f"{round(member['utilization_pct'])}%",
            )
        self.query_one("#monitor-note", Static).update(
            "Member figures are the deployments' own Azure OpenAI metrics (all callers). "
            "Azure Monitor lags a few minutes; refreshes every minute. m changes the range."
        )

    # -- actions ------------------------------------------------------------------------

    def _writable(self) -> bool:
        if not tui(self).can_write():
            self.notify("You are read-only here.", severity="warning")
            return False
        return True

    def _open_editor(self, screen: PoolEditorScreen) -> None:
        def done(pool_id: str | None) -> None:
            if pool_id:
                self.load_pools(pool_id)

        self.app.push_screen(screen, done)

    def action_new(self) -> None:
        if self._writable():
            prefix = (tui(self).context.get("capabilities") or {}).get("id_prefix", "pool-")
            self._open_editor(PoolEditorScreen(pool_id=prefix))

    def action_edit(self) -> None:
        if self._pool and self._writable():
            if self._pool.get("protected"):
                self.notify("This pool is owned by another control plane.", severity="warning")
                return
            self._open_editor(PoolEditorScreen(self._pool))

    def action_delete(self) -> None:
        pool = self._pool
        if not pool or not self._writable():
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.delete_pool(pool)

        self.app.push_screen(
            ConfirmScreen(
                f"Delete {pool['id']}",
                "Delete this pool from APIM? Members this app created that no other pool "
                "uses are deleted with it. "
                "A pool still bound to an API can't be deleted.",
                "Delete",
            ),
            confirmed,
        )

    @work(group="pool-write")
    async def delete_pool(self, pool: Json) -> None:
        try:
            await tui(self).api.delete_pool(pool["id"], pool.get("etag"))
        except ApiError as error:
            tui(self).show_error(error, "Cannot delete the pool")
            return
        self.notify(f"Deleted {pool['id']}")
        self._pool = None
        self.load_pools()

    def action_create_api(self) -> None:
        pool = self._pool
        if not pool or not self._writable():
            return
        prefix = (tui(self).context.get("capabilities") or {}).get("id_prefix", "")

        def entered(path: str | None) -> None:
            if path:
                self.create_api(pool["id"], path.strip("/"))

        self.app.push_screen(
            PromptScreen(
                "Create an API",
                f"Path below {self._endpoint('')}",
                suggest_api_path(pool.get("title"), pool["id"], prefix),
                pattern=API_PATH_PATTERN,
                invalid="Letters, digits and . _ ~ - separated by /",
            ),
            entered,
        )

    @work(group="pool-write")
    async def create_api(self, pool_id: str, path: str) -> None:
        try:
            created = await tui(self).api.create_pool_api(pool_id, {"path": path})
        except ApiError as error:
            tui(self).show_error(error, "Cannot create the API")
            return
        self.notify(f"Clients can call {self._endpoint(created['path'])}")
        self.load_pools(pool_id)

    def action_bind_api(self) -> None:
        pool = self._pool
        if not pool or not self._writable():
            return
        options = [
            (item["id"], f"{item['display_name']}  /{item['path']}")
            for item in self._apis
            if not item.get("binding") and not item.get("foreign_backend_selection")
        ]

        def chosen(api_id: str | None) -> None:
            if api_id:
                self.bind_api(pool, api_id)

        self.app.push_screen(
            PickScreen(
                "Bind an API",
                options,
                "APIs without a pool binding. The original policy is kept "
                "and restored byte for byte on unbind.",
            ),
            chosen,
        )

    @work(group="pool-write")
    async def bind_api(self, pool: Json, api_id: str) -> None:
        managed_identity = any(
            (self._backends.get(member["backend_id"].casefold()) or {}).get("auth_type")
            == "managed_identity"
            for member in pool["members"]
        )
        target = next(item for item in self._apis if item["id"] == api_id)
        body = {
            "pool_id": pool["id"],
            **DEFAULT_BINDING,
            "managed_identity_resource": COGNITIVE_SERVICES_RESOURCE if managed_identity else None,
            "etag": target.get("etag"),
        }
        try:
            await tui(self).api.bind(api_id, body)
        except ApiError as error:
            tui(self).show_error(error, "Cannot bind the API")
            return
        self.notify(f"Bound {api_id} to {pool['id']}")
        self.load_pools(pool["id"])

    def _highlighted_api(self) -> Json | None:
        table = self.query_one("#pool-apis", DataTable)
        if not self._bound:
            return None
        return self._bound[min(max(table.cursor_row, 0), len(self._bound) - 1)]

    def action_unbind_api(self) -> None:
        target = self._highlighted_api()
        if not target or not self._writable():
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.unbind_api(target)

        self.app.push_screen(
            ConfirmScreen(
                f"Unbind {target['id']}",
                "Remove the pool from this API's policy? The original "
                "policy is restored and the API stops using the pool.",
                "Unbind",
            ),
            confirmed,
        )

    @work(group="pool-write")
    async def unbind_api(self, target: Json) -> None:
        try:
            await tui(self).api.unbind(target["id"], target.get("etag"))
        except ApiError as error:
            tui(self).show_error(error, "Cannot unbind the API")
            return
        self.notify(f"Unbound {target['id']}")
        self.load_pools(self._pool["id"] if self._pool else None)

    def action_try_api(self) -> None:
        target = self._highlighted_api()
        if not target:
            self.notify("Bind or create an API first (a / b).", severity="warning")
            return
        gateway = (tui(self).context.get("capabilities") or {}).get("gateway_url")
        self.app.push_screen(TryApiScreen(target, self._model_name or "<model>", gateway))

    def action_range(self) -> None:
        self._range = RANGES[(RANGES.index(self._range) + 1) % len(RANGES)]
        self.load_metrics()
