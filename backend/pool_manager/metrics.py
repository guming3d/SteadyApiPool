"""The pool dashboard: the gateway's own view of a pool, and its Foundry members' metrics; and
the gateway dashboard: all of the gateway's traffic and the load on what it runs on.

``PoolMonitor`` collects both for a pool and a time range. The gateway's view is the product's
(``apim/metrics.py``: APIM's platform metrics of the APIs bound to the pool;
``litellm/metrics.py``: LiteLLM's OpenTelemetry spans in Application Insights). Each member that
is an Azure OpenAI / Foundry deployment adds that deployment's metrics
(``ModelDeploymentName``); those count every caller of the deployment, not only this gateway.

The gateway dashboard (``PoolMonitor.load``) is the product's traffic over every pool, and the
platform metrics of the gateway's resources (``LoadSpec`` per metric, read by ``resource_load``).

Everything here is read-only. Results are cached for a minute per pool (or the gateway) and range.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Protocol

from .arm import ArmClient, ArmError
from .config import Settings
from .models import (
    GatewayLoad,
    GatewayMetrics,
    LoadMetric,
    LoadPoint,
    LoadProblem,
    LoadSeries,
    LoadUnit,
    MemberMetrics,
    MetricsRange,
    ModelCheck,
    PoolMemberView,
    PoolMetrics,
)

logger = logging.getLogger(__name__)

METRICS_API_VERSION = "2023-10-01"
METRICS_READ = "Microsoft.Insights/metrics/read"
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


@dataclass(frozen=True)
class Window:
    """The time range a dashboard covers, and the grain of its charts (e.g. ``PT5M``)."""

    start: datetime
    end: datetime
    interval: str

    @property
    def start_text(self) -> str:
        return f"{self.start:%Y-%m-%dT%H:%M:%SZ}"

    @property
    def end_text(self) -> str:
        return f"{self.end:%Y-%m-%dT%H:%M:%SZ}"

    @property
    def timespan(self) -> str:
        """As Azure Monitor takes it: ``start/end``."""
        return f"{self.start_text}/{self.end_text}"


@dataclass
class GatewayReading:
    """What the gateway itself reports about a pool."""

    metrics: GatewayMetrics | None
    # Why ``metrics`` is missing, shown on the tab (a code the UI knows, or Azure's message).
    error: str | None = None
    # LiteLLM: the gateway's attempts on each member (by member ID, case-folded):
    # ``attempts``, ``failures`` and ``throttled``.
    attempts: dict[str, dict[str, int]] = field(default_factory=dict)


class MonitoredPool(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def members(self) -> list[PoolMemberView]: ...


class ModelChecks(Protocol):
    def check_models(self, backend_ids: list[str]) -> ModelCheck: ...


def quote(value: str) -> str:
    return value.replace("'", "''")


def query(
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


def code_kind(code: str) -> str:
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


def error_text(error: ArmError, what: str) -> str:
    if error.status in {401, 403}:
        return f"Cannot read metrics of {what}: {METRICS_READ} is needed (Monitoring Reader has it)"
    return f"Cannot read metrics of {what}: {error.message}"


def weighted(values: list[tuple[float, float]]) -> float | None:
    """Average of (value, weight) pairs; None without weight."""
    weight = sum(w for _, w in values)
    return sum(v * w for v, w in values) / weight if weight else None


@dataclass(frozen=True)
class LoadSpec:
    """One platform metric of a gateway's resource, read as a load series."""

    # Azure Monitor's name for it, e.g. ``CpuPercent_Gateway``.
    azure: str
    metric: LoadMetric
    unit: LoadUnit
    # Turns Azure's value into the series' unit, e.g. nanocores into a share of the replica's.
    scale: float = 1.0
    # Whether Azure's Maximum is the busiest moment (APIM's Capacity reads 0 below Premium).
    peaks: bool = True
    limit: float | None = None


def _by_stamp(found: list[Series], combine: Callable[[list[float]], float]) -> dict[str, float]:
    """One value per time bin over every series Azure returned (one, unsplit)."""
    values: dict[str, list[float]] = defaultdict(list)
    for series in found:
        for stamp, value in series.points:
            if value is not None:
                values[stamp].append(float(value))
    return {stamp: combine(present) for stamp, present in values.items()}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def resource_load(
    arm: ArmClient, resource_id: str, specs: Iterable[LoadSpec], window: Window
) -> tuple[list[LoadSeries], LoadProblem | None]:
    """The resource's load series, each metric on its own: a metric this tier or SKU doesn't
    emit (Azure answers 400, or only empty bins) is left out rather than failing the rest."""
    name = resource_id.rpartition("/")[2]
    result: list[LoadSeries] = []
    for spec in specs:

        def read(aggregation: str, spec: LoadSpec = spec) -> list[Series]:
            return query(
                arm, resource_id, [spec.azure], aggregation, window.timespan, window.interval
            )

        try:
            averages = _by_stamp(read("Average"), _mean)
            peaks = _by_stamp(read("Maximum"), max) if spec.peaks else {}
        except ArmError as error:
            if error.status in {401, 403}:
                return result, LoadProblem(resource=name, error="forbidden")
            if error.status == 400:
                logger.info("No %s for %s: %s", spec.azure, name, error.message)
                continue
            return result, LoadProblem(
                resource=name, error=f"Cannot read metrics of {name}: {error.message}"
            )
        if not averages:
            continue
        points = [
            LoadPoint(
                time=stamp,
                average=averages[stamp] * spec.scale if stamp in averages else None,
                peak=peaks[stamp] * spec.scale if stamp in peaks else None,
            )
            for stamp in sorted({*averages, *peaks})
        ]
        present = [point.average for point in points if point.average is not None]
        highs = [point.peak for point in points if point.peak is not None] or present
        result.append(
            LoadSeries(
                metric=spec.metric,
                unit=spec.unit,
                resource=name,
                average=_mean(present),
                peak=max(highs),
                latest=present[-1],
                limit=spec.limit,
                points=points,
            )
        )
    return result, None


def _window(range_: MetricsRange) -> Window:
    span, interval = RANGES[range_]
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    return Window(end - span, end, interval)


class PoolMonitor:
    """Each product says which pool and names to report on (``pool``) and what its gateway
    saw (``gateway``); the Foundry half is the same for both. For the gateway dashboard it
    gives all its traffic (``traffic``) and its resources' load (``resources``)."""

    def __init__(self, settings: Settings, arm: ArmClient, service: ModelChecks) -> None:
        self._settings = settings
        self._arm = arm
        self._models = service
        self._lock = Lock()
        self._cache: dict[tuple[str, str], tuple[float, PoolMetrics]] = {}
        self._loads: dict[str, tuple[float, GatewayLoad]] = {}

    # -- what each product reports ----------------------------------------------------

    def pool(self, pool_id: str) -> tuple[MonitoredPool, list[str]]:
        """The pool, and the names its clients reach it by (APIs or model names)."""
        raise NotImplementedError

    def gateway(self, pool: MonitoredPool, names: list[str], window: Window) -> GatewayReading:
        """What the gateway saw of the pool's traffic; runs beside the Foundry reads."""
        raise NotImplementedError

    def traffic(self, window: Window) -> GatewayReading:
        """All of the gateway's traffic, every pool's and any other client's."""
        raise NotImplementedError

    def resources(self, window: Window) -> tuple[list[LoadSeries], list[LoadProblem]]:
        """The load on what the gateway runs on; runs beside ``traffic``."""
        raise NotImplementedError

    # -- the dashboard ----------------------------------------------------------------

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
        pool, names = self.pool(pool_id)
        window = _window(range_)
        interval = window.interval
        models = (
            self._models.check_models([member.backend_id for member in pool.members])
            if pool.members
            else None
        )
        deployments = (
            {member.backend_id: member.deployment_id for member in models.members} if models else {}
        )

        with ThreadPoolExecutor(max_workers=_WORKERS) as executor:
            gateway_future = executor.submit(self.gateway, pool, names, window)
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
            reading = gateway_future.result()

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
            seen = reading.attempts.get(member.backend_id.casefold()) if reading.metrics else None
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
                    gateway_attempts=seen.get("attempts") if seen else None,
                    gateway_failures=seen.get("failures") if seen else None,
                    gateway_throttled=seen.get("throttled") if seen else None,
                )
            )
        return PoolMetrics(
            pool_id=pool.id,
            range=range_,
            interval=interval,
            start=window.start_text,
            end=window.end_text,
            names=names,
            gateway=reading.metrics,
            gateway_error=reading.error,
            members=members,
        )

    # -- the gateway dashboard ------------------------------------------------------------

    def load(self, range_: MetricsRange, *, refresh: bool = False) -> GatewayLoad:
        with self._lock:
            cached = self._loads.get(range_)
        if cached and not refresh and time.monotonic() - cached[0] < _CACHE_SECONDS:
            return cached[1]
        window = _window(range_)
        with ThreadPoolExecutor(max_workers=2) as executor:
            traffic = executor.submit(self.traffic, window)
            series, problems = self.resources(window)
            reading = traffic.result()
        load = GatewayLoad(
            range=range_,
            interval=window.interval,
            start=window.start_text,
            end=window.end_text,
            traffic=reading.metrics,
            traffic_error=reading.error,
            series=series,
            problems=problems,
        )
        with self._lock:
            self._loads[range_] = (time.monotonic(), load)
        return load

    # -- Foundry -------------------------------------------------------------------------

    def _account(
        self, account_id: str, window: Window
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
            return query(
                self._arm, account_id, [metric], aggregation, window.timespan, "FULL", filter_
            )

        try:
            for series in by_deployment("AzureOpenAIRequests", "Total", split_status=True):
                deployment = series.dimensions.get("modeldeploymentname", "").casefold()
                count = round(sum(value or 0 for _, value in series.points))
                kind = code_kind(series.dimensions.get("statuscode", ""))
                entry = values[deployment]
                entry["requests"] = entry.get("requests", 0) + count
                entry.setdefault("throttled", 0)
                entry.setdefault("server_errors", 0)
                if kind in {"throttled", "server_errors"}:
                    entry[kind] += count
        except ArmError as error:
            return region, {}, error_text(error, name)

        # Optional: not every account or deployment type reports these.
        optional = (
            ("TokenTransaction", "Total", "tokens"),
            ("AzureOpenAITTLTInMS", "Average", "latency_ms"),
            ("AzureOpenAIProvisionedManagedUtilizationV2", "Maximum", "utilization_pct"),
        )
        for metric, aggregation, field_name in optional:
            try:
                for series in by_deployment(metric, aggregation):
                    deployment = series.dimensions.get("modeldeploymentname", "").casefold()
                    present = [value for _, value in series.points if value is not None]
                    if not present:
                        continue
                    if aggregation == "Total":
                        values[deployment][field_name] = round(sum(present))
                    elif aggregation == "Maximum":
                        values[deployment][field_name] = max(present)
                    else:
                        values[deployment][field_name] = sum(present) / len(present)
            except ArmError as error:
                logger.info("No %s for %s: %s", metric, account_id, error.message)
        return region, dict(values), None
