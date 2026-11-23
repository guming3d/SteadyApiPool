"""The gateway's half of a pool's dashboard, and the gateway dashboard, from AI Gateway's
OpenTelemetry signals.

A telemetry exporter sends them to Application Insights, whose Log Analytics workspace holds the
``OTelLogs`` and ``OTelSpans`` tables (the component's own Logs page doesn't resolve them). Each
attempt on a backend model writes one request log: ``gen_ai.request.model`` (the alias the
client sent), ``azure.ai_gateway.routing.attempt``, ``azure.ai_gateway.routing.backend`` (which
backend model served it) and ``http.response.status_code``. Attempts of one client request
share its trace and span ID, so requests, the failovers that saved some, and each member's
attempts are counted from them; the request span gives the duration and its ``backend`` child
the time the members took.

Without a telemetry exporter the gateway half is ``no_app_insights``. Reading the logs needs Log
Analytics read on the workspace. AI Gateway reports no platform metrics of its own load, so the
gateway dashboard has its traffic only.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..arm import ArmClient, ArmError
from ..config import Settings
from ..logs import KQL_BINS, LogsReader, kql_string, number, stamp
from ..metrics import GatewayReading, MonitoredPool, PoolMonitor, Window, weighted
from ..models import GatewayMetrics, LatencyPoint, LoadProblem, LoadSeries, TrafficPoint
from .resources import name_of
from .service import AiGatewayService

logger = logging.getLogger(__name__)

APP_INSIGHTS_API_VERSION = "2020-02-02"


def attempts_query(names: list[str] | None, start: str, end: str) -> str:
    """Every attempt on a backend model for these aliases (``None``: every alias)."""
    models = ""
    if names is not None:
        listed = ", ".join(kql_string(name.casefold()) for name in names)
        models = f"\n| where alias in ({listed})"
    return f"""OTelLogs
| where TimeGenerated between (datetime({start}) .. datetime({end}))
| where isnotempty(Attributes["gen_ai.request.model"])
| extend alias = tolower(tostring(Attributes["gen_ai.request.model"])){models}
| extend attempt = coalesce(toint(Attributes["azure.ai_gateway.routing.attempt"]), 1),
    backend = tolower(tostring(Attributes["azure.ai_gateway.routing.backend"])),
    status = toint(Attributes["http.response.status_code"])"""


def requests_query(names: list[str] | None, start: str, end: str, interval: str) -> str:
    """Client requests per time bin: the last attempt of each says how it ended. ``time`` is a
    KQL keyword, so the column is quoted."""
    return f"""let spans = OTelSpans
| where TimeGenerated between (datetime({start}) .. datetime({end}));
let requests = spans
| where Kind == "Server" and isnotempty(Attributes["http.route"])
| project TraceId, SpanId, DurationMs;
let backend_spans = spans
| where Name == "backend"
| summarize BackendMs = sum(DurationMs) by TraceId, SpanId = ParentSpanId;
{attempts_query(names, start, end)}
| summarize attempts = count(), arg_max(attempt, status), started = min(TimeGenerated)
    by TraceId, SpanId
| join kind=leftouter requests on TraceId, SpanId
| join kind=leftouter backend_spans on TraceId, SpanId
| extend outcome = case(status between (200 .. 399), "success", status == 429, "throttled",
    status between (400 .. 499), "client_errors", "server_errors")
| summarize success = countif(outcome == "success"), throttled = countif(outcome == "throttled"),
    client_errors = countif(outcome == "client_errors"),
    server_errors = countif(outcome == "server_errors"),
    failovers = countif(outcome == "success" and attempts > 1),
    duration_ms = avg(DurationMs), backend_ms = avg(BackendMs)
    by ['time'] = bin(started, {interval})
| order by ['time'] asc"""


def members_query(names: list[str], start: str, end: str) -> str:
    """The gateway's attempts on each backend model."""
    return f"""{attempts_query(names, start, end)}
| summarize attempts = count(), failures = countif(status >= 400 or isnull(status)),
    throttled = countif(status == 429)
    by backend"""


def _backend_key(value: str) -> str:
    """A routing backend as the logs name it (an ARM ID or a model name), for matching."""
    return name_of(value).casefold()


class AiGatewayMonitor(PoolMonitor):
    def __init__(
        self,
        settings: Settings,
        arm: ArmClient,
        service: AiGatewayService,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(settings, arm, service)
        self._service = service
        self._reader = LogsReader(arm, transport)

    def pool(self, pool_id: str) -> tuple[MonitoredPool, list[str]]:
        pool = self._service.get_pool(pool_id)
        return pool, [pool.model_name]

    def _workspace(self) -> str | None:
        """The Log Analytics workspace behind the exporter's Application Insights component."""
        component = self._service.app_insights_id()
        if not component:
            return None
        try:
            found = self._arm.get_arm_json(component, api_version=APP_INSIGHTS_API_VERSION)
        except ArmError as error:
            logger.info("Cannot read %s: %s", component, error.message)
            return None
        workspace = str(((found or {}).get("properties") or {}).get("WorkspaceResourceId") or "")
        return workspace or None

    @staticmethod
    def _failure(error: ArmError) -> GatewayReading:
        # "forbidden" is shown with the role it needs; anything else as Azure said.
        return GatewayReading(
            None,
            "forbidden"
            if error.status in {401, 403}
            else f"Cannot read the gateway's telemetry: {error.message}",
        )

    def gateway(self, pool: MonitoredPool, names: list[str], window: Window) -> GatewayReading:
        workspace = self._workspace()
        if not workspace:
            return GatewayReading(None, "no_app_insights")
        try:
            metrics = self._requests(workspace, names, window)
            rows = self._reader.query(
                workspace,
                members_query(names, window.start_text, window.end_text),
                window.start_text,
                window.end_text,
            )
        except ArmError as error:
            logger.info("Gateway metrics for pool %s: %s", pool.id, error.message)
            return self._failure(error)
        by_backend: dict[str, dict[str, int]] = {}
        for row in rows:
            key = _backend_key(str(row.get("backend") or ""))
            entry = by_backend.setdefault(key, {"attempts": 0, "failures": 0, "throttled": 0})
            for field in entry:
                entry[field] += int(number(row.get(field)) or 0)
        models = {view.id.casefold(): view for view in self._service.list_backends()}
        attempts: dict[str, dict[str, int]] = {}
        for member in pool.members:
            view = models.get(member.backend_id.casefold())
            keys = {member.backend_id.casefold()}
            if view is not None:
                keys |= {_backend_key(view.resource_id), view.model.casefold()}
            found = next((by_backend[key] for key in keys if key in by_backend), None)
            attempts[member.backend_id.casefold()] = found or {
                "attempts": 0,
                "failures": 0,
                "throttled": 0,
            }
        return GatewayReading(metrics, attempts=attempts)

    def traffic(self, window: Window) -> GatewayReading:
        workspace = self._workspace()
        if not workspace:
            return GatewayReading(None, "no_app_insights")
        try:
            return GatewayReading(self._requests(workspace, None, window))
        except ArmError as error:
            logger.info("Gateway metrics: %s", error.message)
            return self._failure(error)

    def resources(self, window: Window) -> tuple[list[LoadSeries], list[LoadProblem]]:
        # A managed gateway: Azure scales it and reports no load of its own.
        return [], []

    def _requests(self, workspace: str, names: list[str] | None, window: Window) -> GatewayMetrics:
        bins = self._reader.query(
            workspace,
            requests_query(
                names, window.start_text, window.end_text, KQL_BINS.get(window.interval, "5m")
            ),
            window.start_text,
            window.end_text,
        )
        return _metrics(bins)


def _metrics(bins: list[dict[str, Any]]) -> GatewayMetrics:
    traffic = [
        TrafficPoint(
            time=stamp(row.get("time")),
            success=int(number(row.get("success")) or 0),
            throttled=int(number(row.get("throttled")) or 0),
            client_errors=int(number(row.get("client_errors")) or 0),
            server_errors=int(number(row.get("server_errors")) or 0),
        )
        for row in bins
    ]
    kinds = ("success", "throttled", "client_errors", "server_errors")
    totals = {kind: sum(getattr(point, kind) for point in traffic) for kind in kinds}
    requests = sum(totals.values())
    counts = [float(sum(getattr(point, kind) for kind in kinds)) for point in traffic]

    def overall(column: str) -> float | None:
        return weighted(
            [
                (value, weight)
                for row, weight in zip(bins, counts, strict=True)
                if (value := number(row.get(column))) is not None and weight
            ]
        )

    return GatewayMetrics(
        requests=requests,
        success=totals["success"],
        throttled=totals["throttled"],
        client_errors=totals["client_errors"],
        server_errors=totals["server_errors"],
        failovers=int(sum(number(row.get("failovers")) or 0 for row in bins)),
        availability=(requests - totals["server_errors"]) / requests if requests else None,
        duration_ms=overall("duration_ms"),
        backend_duration_ms=overall("backend_ms"),
        traffic=traffic,
        latency=[
            LatencyPoint(
                time=stamp(row.get("time")),
                overall_ms=number(row.get("duration_ms")),
                backend_ms=number(row.get("backend_ms")),
            )
            for row in bins
        ],
    )
