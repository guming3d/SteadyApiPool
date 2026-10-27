"""The pool dashboard: the gateway's own view of a pool, and its Foundry members' metrics.

The gateway's view comes from LiteLLM's OpenTelemetry spans, which the gateway's collector sends
to Application Insights: one span per attempt on a member (``chat <model>``), with the member's
host and deployment, the outcome and the tokens. Spans of one client request share a trace
(``operation_Id``), so the requests clients saw, and those a failover saved, are counted from
them. LiteLLM's own spend logs are not used: they drop rows.

Each member that is an Azure OpenAI / Foundry deployment adds that deployment's metrics
(``ModelDeploymentName``). Those count every caller of the deployment, not only this gateway.

Everything here is read-only: Log Analytics read on the Application Insights component, and
``Microsoft.Insights/metrics/read`` on the member accounts. Results are cached for a minute per
pool and range.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

import httpx

from .arm import ArmClient, ArmError
from .config import Settings
from .errors import ServiceError
from .litellm.resources import member_target
from .litellm.service import PoolManagerService
from .models import (
    GatewayMetrics,
    LatencyPoint,
    MemberMetrics,
    MetricsRange,
    PoolMemberView,
    PoolMetrics,
    TrafficPoint,
)

logger = logging.getLogger(__name__)

METRICS_API_VERSION = "2023-10-01"
METRICS_READ = "Microsoft.Insights/metrics/read"
LOGS_URL = "https://api.loganalytics.azure.com/v1"
LOGS_SCOPE = "https://api.loganalytics.io/.default"
_LOGS_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# Span names LiteLLM gives model calls.
_MODEL_SPANS = ("chat ", "responses ", "embeddings ", "text_completion ", "completion ")
# Window and grain per range; about 60-170 points each, inside Azure Monitor's limits.
RANGES: dict[MetricsRange, tuple[timedelta, str]] = {
    "1h": (timedelta(hours=1), "PT1M"),
    "6h": (timedelta(hours=6), "PT5M"),
    "24h": (timedelta(hours=24), "PT15M"),
    "7d": (timedelta(days=7), "PT1H"),
}
_CACHE_SECONDS = 60.0
_WORKERS = 8
# Series returned when splitting by a dimension (response codes, deployments).
_TOP = "500"


@dataclass(frozen=True)
class Series:
    metric: str
    dimensions: dict[str, str]
    points: list[tuple[str, float | None]]


def _quote(value: str) -> str:
    return value.replace("'", "''")


def _query(
    arm: ArmClient,
    resource_id: str,
    metrics: Iterable[str],
    aggregation: str,
    window: str,
    interval: str,
    filter_: str | None = None,
) -> list[Series]:
    params = {
        "metricnames": ",".join(metrics),
        "aggregation": aggregation,
        "timespan": window,
        "interval": interval,
    }
    if filter_:
        params["$filter"] = filter_
        if "'*'" in filter_:
            params["top"] = _TOP
    result: list[Series] = []
    for metric in arm.list_arm(
        f"{resource_id}/providers/Microsoft.Insights/metrics",
        api_version=METRICS_API_VERSION,
        params=params,
    ):
        name = str((metric.get("name") or {}).get("value") or "")
        for series in metric.get("timeseries") or []:
            dimensions = {
                str((item.get("name") or {}).get("value") or "").casefold(): str(
                    item.get("value") or ""
                )
                for item in series.get("metadatavalues") or []
            }
            points = [
                (str(point.get("timeStamp") or ""), point.get(aggregation.casefold()))
                for point in series.get("data") or []
            ]
            result.append(Series(name, dimensions, points))
    return result


def _code_kind(code: str) -> str:
    """success, throttled, client_errors or server_errors for an HTTP status (or category)."""
    lowered = code.casefold()
    if not code.isdigit():
        if "server" in lowered:
            return "server_errors"
        if "client" in lowered:
            return "client_errors"
        return "success"
    status = int(code)
    if status == 429:
        return "throttled"
    if status >= 500:
        return "server_errors"
    # 0: the client went away before an answer.
    if status >= 400 or status == 0:
        return "client_errors"
    return "success"


def expected_shares(members: list[PoolMemberView]) -> list[float]:
    """Traffic each member gets while all are healthy: the highest priority (lowest number)
    shares it by weight, everyone else is standby."""
    if not members:
        return []
    top = min(member.priority for member in members)
    active = [member for member in members if member.priority == top]
    total = sum(max(member.weight, 0) for member in active)
    return [
        (max(member.weight, 0) / total if total else 1 / len(active))
        if member.priority == top
        else 0.0
        for member in members
    ]


def _kql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def spans_query(names: list[str], start: str, end: str) -> str:
    """The pool's attempts: LiteLLM's model-call spans for its model names."""
    listed = ", ".join(_kql_string(name.casefold()) for name in names)
    prefixes = " or ".join(f"name startswith {_kql_string(p)}" for p in _MODEL_SPANS)
    return f"""union isfuzzy=true dependencies, requests
| where timestamp between (datetime({start}) .. datetime({end}))
| where {prefixes}
| extend model = tolower(tostring(customDimensions["gen_ai.request.model"]))
| where model in ({listed})
| extend host = tolower(tostring(customDimensions["server.address"])),
    target = tostring(customDimensions["litellm.provider.model"]),
    code = tostring(customDimensions["litellm.provider.error.code"]),
    tokens = toint(customDimensions["gen_ai.usage.total_tokens"])
| extend failed = success == false or isnotempty(tostring(customDimensions["error.type"]))"""


def requests_query(names: list[str], start: str, end: str, interval: str) -> str:
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


def _error_text(error: ArmError, what: str) -> str:
    if error.status in {401, 403}:
        return f"Cannot read metrics of {what}: {METRICS_READ} is needed (Monitoring Reader has it)"
    return f"Cannot read metrics of {what}: {error.message}"


def _weighted(values: list[tuple[float, float]]) -> float | None:
    """Average of (value, weight) pairs; None without weight."""
    weight = sum(w for _, w in values)
    return sum(v * w for v, w in values) / weight if weight else None


class PoolMonitor:
    def __init__(
        self,
        settings: Settings,
        arm: ArmClient,
        service: PoolManagerService,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._arm = arm
        self._service = service
        self._http = httpx.Client(timeout=_LOGS_TIMEOUT, transport=transport)
        self._lock = Lock()
        self._cache: dict[tuple[str, str], tuple[float, PoolMetrics]] = {}

    def get(self, pool_id: str, range_: MetricsRange, *, refresh: bool = False) -> PoolMetrics:
        key = (pool_id.casefold(), range_)
        with self._lock:
            cached = self._cache.get(key)
        if cached and not refresh and time.monotonic() - cached[0] < _CACHE_SECONDS:
            return cached[1]
        metrics = self._collect(pool_id, range_)
        with self._lock:
            self._cache[key] = (time.monotonic(), metrics)
        return metrics

    def _collect(self, pool_id: str, range_: MetricsRange) -> PoolMetrics:
        endpoint = self._service.endpoint(pool_id)
        pool = self._service.get_pool(endpoint.pool_id)
        names = endpoint.model_names
        span, interval = RANGES[range_]
        end = datetime.now(UTC).replace(second=0, microsecond=0)
        start = end - span
        window = f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}"
        models = (
            self._service.check_models([member.backend_id for member in pool.members])
            if pool.members
            else None
        )
        deployments = (
            {member.backend_id: member.deployment_id for member in models.members} if models else {}
        )
        app_insights = self._app_insights()
        bounds = (f"{start:%Y-%m-%dT%H:%M:%SZ}", f"{end:%Y-%m-%dT%H:%M:%SZ}")

        with ThreadPoolExecutor(max_workers=_WORKERS) as executor:
            gateway_future = (
                executor.submit(self._gateway, app_insights, names, *bounds, interval)
                if app_insights
                else None
            )
            accounts = sorted(
                {
                    deployment.rpartition("/deployments/")[0]
                    for deployment in deployments.values()
                    if deployment and "/deployments/" in deployment
                }
            )
            account_results = dict(
                zip(
                    accounts,
                    executor.map(lambda account: self._account(account, window), accounts),
                    strict=True,
                )
            )
            gateway: GatewayMetrics | None = None
            attempts: dict[tuple[str, str], dict[str, int]] = {}
            gateway_error: str | None = None if app_insights else "no_app_insights"
            if gateway_future is not None:
                try:
                    gateway, attempts = gateway_future.result()
                except ArmError as error:
                    # "forbidden" is shown with the role it needs; anything else as Azure said.
                    gateway_error = (
                        "forbidden"
                        if error.status in {401, 403}
                        else f"Cannot read the gateway's spans: {error.message}"
                    )
                    logger.info("Gateway metrics for pool %s: %s", pool_id, error.message)
        members_by_id = {view.id.casefold(): view for view in self._service.list_backends()}

        members: list[MemberMetrics] = []
        for member, share in zip(pool.members, expected_shares(pool.members), strict=True):
            deployment_id = deployments.get(member.backend_id)
            values: dict[str, Any] = {}
            region: str | None = None
            problem: str | None = None
            if deployment_id and "/deployments/" in deployment_id:
                account, _, name = deployment_id.rpartition("/deployments/")
                region, by_deployment, problem = account_results[account]
                if not problem:
                    # Metrics only list deployments that had traffic in the window, so a
                    # missing count is zero (even if another metric lists the deployment).
                    zero = {"requests": 0, "throttled": 0, "server_errors": 0, "tokens": 0}
                    values = {**zero, **by_deployment.get(name.casefold(), {})}
                if not values.get("requests"):
                    # Azure reports a 0 ms average for an idle deployment; no requests, no latency.
                    values = {**values, "latency_ms": None}
            seen = self._attempts(members_by_id.get(member.backend_id.casefold()), attempts)
            members.append(
                MemberMetrics(
                    backend_id=member.backend_id,
                    deployment_id=deployment_id,
                    region=region,
                    requests=values.get("requests"),
                    throttled=values.get("throttled"),
                    server_errors=values.get("server_errors"),
                    tokens=values.get("tokens"),
                    latency_ms=values.get("latency_ms"),
                    utilization_pct=values.get("utilization_pct"),
                    expected_share=share,
                    error=problem,
                    gateway_attempts=seen.get("attempts") if gateway else None,
                    gateway_failures=seen.get("failures") if gateway else None,
                    gateway_throttled=seen.get("throttled") if gateway else None,
                )
            )
        return PoolMetrics(
            pool_id=pool.id,
            range=range_,
            interval=interval,
            start=f"{start:%Y-%m-%dT%H:%M:%SZ}",
            end=f"{end:%Y-%m-%dT%H:%M:%SZ}",
            model_names=names,
            gateway=gateway,
            gateway_error=gateway_error,
            members=members,
        )

    # -- the gateway's spans -----------------------------------------------------------

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
        bins = self._logs(
            app_insights,
            requests_query(names, start, end, _KQL_BINS.get(interval, "5m")),
            start,
            end,
        )
        per_member = self._logs(app_insights, members_query(names, start, end), start, end)
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
            return _weighted(
                [
                    (value, weight)
                    for row, weight in zip(bins, counts, strict=True)
                    if (value := _number(row.get(column))) is not None and weight
                ]
            )

        gateway = GatewayMetrics(
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
        attempts = {
            (str(row.get("host") or ""), str(row.get("target") or "").casefold()): {
                "attempts": int(_number(row.get("attempts")) or 0),
                "failures": int(_number(row.get("failures")) or 0),
                "throttled": int(_number(row.get("throttled")) or 0),
            }
            for row in per_member
        }
        return gateway, attempts

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

    # -- Foundry -------------------------------------------------------------------------

    def _account(
        self, account_id: str, window: str
    ) -> tuple[str | None, dict[str, dict[str, Any]], str | None]:
        """(region, metrics by lower-case deployment name, error) for one account."""
        name = account_id.rpartition("/")[2]
        region: str | None = None
        try:
            found = self._arm.get_arm_json(
                account_id, api_version=self._settings.cognitive_api_version
            )
            region = str((found or {}).get("location") or "") or None
        except ArmError as error:
            logger.info("Cannot read account %s: %s", account_id, error.message)
        values: dict[str, dict[str, Any]] = defaultdict(dict)

        def by_deployment(
            metric: str, aggregation: str, split_status: bool = False
        ) -> list[Series]:
            filter_ = "ModelDeploymentName eq '*'"
            if split_status:
                filter_ += " and StatusCode eq '*'"
            return _query(self._arm, account_id, [metric], aggregation, window, "FULL", filter_)

        try:
            for series in by_deployment("AzureOpenAIRequests", "Total", split_status=True):
                deployment = series.dimensions.get("modeldeploymentname", "").casefold()
                count = round(sum(value or 0 for _, value in series.points))
                kind = _code_kind(series.dimensions.get("statuscode", ""))
                entry = values[deployment]
                entry["requests"] = entry.get("requests", 0) + count
                entry.setdefault("throttled", 0)
                entry.setdefault("server_errors", 0)
                if kind in {"throttled", "server_errors"}:
                    entry[kind] += count
        except ArmError as error:
            return region, {}, _error_text(error, name)

        # Optional: not every account or deployment type reports these.
        optional = (
            ("TokenTransaction", "Total", "tokens"),
            ("AzureOpenAITTLTInMS", "Average", "latency_ms"),
            ("AzureOpenAIProvisionedManagedUtilizationV2", "Maximum", "utilization_pct"),
        )
        for metric, aggregation, field in optional:
            try:
                for series in by_deployment(metric, aggregation):
                    deployment = series.dimensions.get("modeldeploymentname", "").casefold()
                    present = [value for _, value in series.points if value is not None]
                    if not present:
                        continue
                    if aggregation == "Total":
                        values[deployment][field] = round(sum(present))
                    elif aggregation == "Maximum":
                        values[deployment][field] = max(present)
                    else:
                        values[deployment][field] = sum(present) / len(present)
            except ArmError as error:
                logger.info("No %s for %s: %s", metric, account_id, error.message)
        return region, dict(values), None
