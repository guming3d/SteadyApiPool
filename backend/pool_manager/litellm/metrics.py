"""The gateway's half of a pool's dashboard, and the gateway dashboard, from LiteLLM's
OpenTelemetry spans and its resources' platform metrics.

The gateway's collector sends them to Application Insights: one span per attempt on a member
(``chat <model>``), with the member's host and deployment, the outcome and the tokens. Spans of
one client request share a trace (``operation_Id``), so the requests clients saw, and those a
failover saved, are counted from them. LiteLLM's own spend logs are not used: they drop rows.

The gateway dashboard counts the spans of every model, and adds the load on what LiteLLM runs on:
the Container App's replicas and their CPU and memory against each replica's allocation, or the
AKS cluster's nodes, and the PostgreSQL server deployed with it (found by the gateway's tags).

Reading the spans needs Log Analytics read on the Application Insights component; the load,
``Microsoft.Insights/metrics/read`` on each resource and reading the resource group's resources.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..arm import ArmClient, ArmError
from ..config import Settings
from ..errors import ServiceError
from ..metrics import (
    GatewayReading,
    LoadSpec,
    MonitoredPool,
    PoolMonitor,
    Window,
    resource_load,
    weighted,
)
from ..models import GatewayMetrics, LatencyPoint, LoadProblem, LoadSeries, TrafficPoint
from .cleanup import tagged_resources
from .gateway import GatewayInfo, GatewayUnavailable
from .resources import member_target
from .service import LiteLLMService

logger = logging.getLogger(__name__)

LOGS_URL = "https://api.loganalytics.azure.com/v1"
LOGS_SCOPE = "https://api.loganalytics.io/.default"
_LOGS_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# Span names LiteLLM gives model calls.
_MODEL_SPANS = ("chat ", "responses ", "embeddings ", "text_completion ", "completion ")
_POSTGRES_TYPE = "microsoft.dbforpostgresql/flexibleservers"
# AKS: the nodes LiteLLM's pods run on (shared with the cluster's own pods).
NODE_SPECS = (
    LoadSpec("node_cpu_usage_percentage", "node_cpu", "percent"),
    LoadSpec("node_memory_working_set_percentage", "node_memory", "percent"),
)
POSTGRES_SPECS = (
    LoadSpec("cpu_percent", "db_cpu", "percent"),
    LoadSpec("memory_percent", "db_memory", "percent"),
    LoadSpec("active_connections", "db_connections", "count"),
    # Read-only at 95 %: storage grows by itself on gateways this app deploys, but not forever.
    LoadSpec("storage_percent", "db_storage", "percent"),
)


def _kql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def spans_query(names: list[str] | None, start: str, end: str) -> str:
    """The pool's attempts: LiteLLM's model-call spans for its model names (``None``: every
    model's, the whole gateway's)."""
    prefixes = " or ".join(f"name startswith {_kql_string(p)}" for p in _MODEL_SPANS)
    models = ""
    if names is not None:
        listed = ", ".join(_kql_string(name.casefold()) for name in names)
        models = f"\n| where model in ({listed})"
    return f"""union isfuzzy=true dependencies, requests
| where timestamp between (datetime({start}) .. datetime({end}))
| where {prefixes}
| extend model = tolower(tostring(customDimensions["gen_ai.request.model"])){models}
| extend host = tolower(tostring(customDimensions["server.address"])),
    target = tostring(customDimensions["litellm.provider.model"]),
    code = tostring(customDimensions["litellm.provider.error.code"]),
    tokens = toint(customDimensions["gen_ai.usage.total_tokens"])
| extend failed = success == false or isnotempty(tostring(customDimensions["error.type"]))"""


def requests_query(names: list[str] | None, start: str, end: str, interval: str) -> str:
    """Client requests per time bin, from the attempts of each trace. ``time`` is a KQL keyword,
    so the column is quoted."""
    # arg_max names its outputs after the columns it takes: the last attempt's code arrives as
    # last_code because it is called that before.
    return f"""{spans_query(names, start, end)}
| extend last_code = code
| summarize attempts = count(), ok = countif(not(failed)),
    arg_max(timestamp, last_code), started = min(timestamp),
    ended = max(timestamp + duration * 1ms), backend = avgif(duration, not(failed))
    by operation_Id
| extend outcome = case(ok > 0, "success", last_code == "429", "throttled",
    last_code startswith "4", "client_errors", "server_errors")
| summarize success = countif(outcome == "success"), throttled = countif(outcome == "throttled"),
    client_errors = countif(outcome == "client_errors"),
    server_errors = countif(outcome == "server_errors"),
    failovers = countif(ok > 0 and attempts > ok),
    duration_ms = avg((ended - started) / 1ms), backend_ms = avg(backend)
    by ['time'] = bin(started, {interval})
| order by ['time'] asc"""


def members_query(names: list[str], start: str, end: str) -> str:
    """The gateway's attempts on each member (host and deployment)."""
    return f"""{spans_query(names, start, end)}
| summarize attempts = count(), failures = countif(failed), throttled = countif(code == "429")
    by host, target"""


# Bin sizes per range for KQL (the same grain as the Azure Monitor charts).
_KQL_BINS = {"PT1M": "1m", "PT5M": "5m", "PT15M": "15m", "PT1H": "1h"}


def _innermost(error: object) -> str | None:
    """Log Analytics nests the useful part ("Query could not be parsed at ...") under
    ``innererror``; the top message is only "The request had some invalid properties"."""
    message = None
    while isinstance(error, dict):
        message = str(error.get("message") or "") or message
        error = error.get("innererror")
    return message


def _rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    tables = body.get("tables") or []
    if not tables:
        return []
    columns = [str(column.get("name")) for column in tables[0].get("columns") or []]
    return [dict(zip(columns, row, strict=False)) for row in tables[0].get("rows") or []]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _stamp(value: object) -> str:
    """KQL's ISO timestamps, in the same form as Azure Monitor's (no fraction)."""
    text = str(value or "")
    return text.split(".")[0].rstrip("Z") + "Z" if text else text


class LiteLLMMonitor(PoolMonitor):
    def __init__(
        self,
        settings: Settings,
        arm: ArmClient,
        service: LiteLLMService,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(settings, arm, service)
        self._service = service
        self._http = httpx.Client(timeout=_LOGS_TIMEOUT, transport=transport)

    def pool(self, pool_id: str) -> tuple[MonitoredPool, list[str]]:
        endpoint = self._service.endpoint(pool_id)
        return self._service.get_pool(endpoint.pool_id), endpoint.model_names

    def gateway(self, pool: MonitoredPool, names: list[str], window: Window) -> GatewayReading:
        app_insights = self._app_insights()
        if not app_insights:
            return GatewayReading(None, "no_app_insights")
        try:
            gateway, attempts = self._gateway(
                app_insights, names, window.start_text, window.end_text, window.interval
            )
        except ArmError as error:
            logger.info("Gateway metrics for pool %s: %s", pool.id, error.message)
            # "forbidden" is shown with the role it needs; anything else as Azure said.
            return GatewayReading(
                None,
                "forbidden"
                if error.status in {401, 403}
                else f"Cannot read the gateway's spans: {error.message}",
            )
        members_by_id = {view.id.casefold(): view for view in self._service.list_backends()}
        return GatewayReading(
            gateway,
            attempts={
                member.backend_id.casefold(): self._attempts(
                    members_by_id.get(member.backend_id.casefold()), attempts
                )
                for member in pool.members
            },
        )

    def traffic(self, window: Window) -> GatewayReading:
        app_insights = self._app_insights()
        if not app_insights:
            return GatewayReading(None, "no_app_insights")
        try:
            return GatewayReading(
                self._requests(
                    app_insights, None, window.start_text, window.end_text, window.interval
                )
            )
        except ArmError as error:
            logger.info("Gateway metrics: %s", error.message)
            return GatewayReading(
                None,
                "forbidden"
                if error.status in {401, 403}
                else f"Cannot read the gateway's spans: {error.message}",
            )

    def resources(self, window: Window) -> tuple[list[LoadSeries], list[LoadProblem]]:
        settings = self._settings
        try:
            info = self._service.gateway.info()
        except GatewayUnavailable as error:
            return [], [LoadProblem(resource=settings.gateway_name, error=error.message)]
        found: list[tuple[str, tuple[LoadSpec, ...]]] = []
        # Where the template put the gateway's database: only gateways this app deployed.
        group: tuple[str, str] | None = None
        if info.kind == "aca" and info.resource_id:
            found.append((info.resource_id, self._replica_specs(info)))
            if info.managed:
                group = (settings.subscription_id, settings.resource_group)
        cluster = settings.gateway_cluster_id
        if cluster:
            found.append((cluster, NODE_SPECS))
            parts = cluster.split("/")
            if len(parts) > 4:
                group = (parts[2], parts[4])
        problems: list[LoadProblem] = []
        if group:
            try:
                found.extend(
                    (str(item["id"]), POSTGRES_SPECS)
                    for item in tagged_resources(self._arm, *group, settings.gateway_name)
                    if str(item.get("type") or "").casefold() == _POSTGRES_TYPE
                )
            except ArmError as error:
                logger.info("Cannot list the gateway's resources: %s", error.message)
                problems.append(
                    LoadProblem(
                        resource=group[1],
                        error="forbidden_resources"
                        if error.status in {401, 403}
                        else error.message,
                    )
                )
        series: list[LoadSeries] = []
        for resource_id, specs in found:
            read, problem = resource_load(self._arm, resource_id, specs, window)
            series.extend(read)
            if problem:
                problems.append(problem)
        return series, problems

    @staticmethod
    def _replica_specs(info: GatewayInfo) -> tuple[LoadSpec, ...]:
        """Replicas, and each replica's CPU and memory as a share of what it is given."""
        specs = [LoadSpec("Replicas", "replicas", "count", limit=info.max_replicas)]
        if info.cpu:
            specs.append(LoadSpec("UsageNanoCores", "cpu", "percent", scale=100 / (info.cpu * 1e9)))
        if info.memory_gib:
            specs.append(
                LoadSpec(
                    "WorkingSetBytes", "memory", "percent", scale=100 / (info.memory_gib * 2**30)
                )
            )
        return tuple(specs)

    def _app_insights(self) -> str | None:
        try:
            return self._service.capabilities().app_insights_id
        except ServiceError as error:
            logger.info("Cannot read the gateway's Application Insights: %s", error.message)
            return None

    def _logs(self, resource_id: str, query: str, start: str, end: str) -> list[dict[str, Any]]:
        """A KQL query on the component's logs (Log Analytics' resource-centric query API)."""
        token = self._arm.token_for(LOGS_SCOPE)
        try:
            response = self._http.post(
                f"{LOGS_URL}{resource_id}/query",
                json={"query": query, "timespan": f"{start}/{end}"},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as error:
            raise ArmError(502, "LogsUnreachable", str(error)) from error
        if response.status_code >= 400:
            message = response.text[:500]
            with suppress(ValueError):
                message = _innermost(response.json().get("error")) or message
            raise ArmError(response.status_code, "LogsQueryFailed", message)
        body: dict[str, Any] = response.json()
        return _rows(body)

    def _gateway(
        self, app_insights: str, names: list[str], start: str, end: str, interval: str
    ) -> tuple[GatewayMetrics, dict[tuple[str, str], dict[str, int]]]:
        gateway = self._requests(app_insights, names, start, end, interval)
        per_member = self._logs(app_insights, members_query(names, start, end), start, end)
        attempts = {
            (str(row.get("host") or ""), str(row.get("target") or "").casefold()): {
                "attempts": int(_number(row.get("attempts")) or 0),
                "failures": int(_number(row.get("failures")) or 0),
                "throttled": int(_number(row.get("throttled")) or 0),
            }
            for row in per_member
        }
        return gateway, attempts

    def _requests(
        self, app_insights: str, names: list[str] | None, start: str, end: str, interval: str
    ) -> GatewayMetrics:
        """Client requests to these model names (``None``: to every model)."""
        bins = self._logs(
            app_insights,
            requests_query(names, start, end, _KQL_BINS.get(interval, "5m")),
            start,
            end,
        )
        traffic = [
            TrafficPoint(
                time=_stamp(row.get("time")),
                success=int(_number(row.get("success")) or 0),
                throttled=int(_number(row.get("throttled")) or 0),
                client_errors=int(_number(row.get("client_errors")) or 0),
                server_errors=int(_number(row.get("server_errors")) or 0),
            )
            for row in bins
        ]
        totals = {
            kind: sum(getattr(point, kind) for point in traffic)
            for kind in ("success", "throttled", "client_errors", "server_errors")
        }
        requests = sum(totals.values())
        counts = [float(sum(getattr(point, kind) for kind in totals)) for point in traffic]

        def overall(column: str) -> float | None:
            return weighted(
                [
                    (value, weight)
                    for row, weight in zip(bins, counts, strict=True)
                    if (value := _number(row.get(column))) is not None and weight
                ]
            )

        return GatewayMetrics(
            requests=requests,
            success=totals["success"],
            throttled=totals["throttled"],
            client_errors=totals["client_errors"],
            server_errors=totals["server_errors"],
            failovers=int(sum(_number(row.get("failovers")) or 0 for row in bins)),
            availability=(requests - totals["server_errors"]) / requests if requests else None,
            duration_ms=overall("duration_ms"),
            backend_duration_ms=overall("backend_ms"),
            traffic=traffic,
            latency=[
                LatencyPoint(
                    time=_stamp(row.get("time")),
                    overall_ms=_number(row.get("duration_ms")),
                    backend_ms=_number(row.get("backend_ms")),
                )
                for row in bins
            ],
        )

    @staticmethod
    def _attempts(member: Any, attempts: dict[tuple[str, str], dict[str, int]]) -> dict[str, int]:
        """The gateway's attempts on a member, matched by host and provider/model."""
        if member is None:
            return {"attempts": 0, "failures": 0, "throttled": 0}
        try:
            provider, _, model = member_target(member.url, member.model)
        except ValueError:
            return {"attempts": 0, "failures": 0, "throttled": 0}
        host = (urlsplit(member.url).hostname or "").casefold()
        return attempts.get(
            (host, f"{provider}/{model}".casefold()), {"attempts": 0, "failures": 0, "throttled": 0}
        )
