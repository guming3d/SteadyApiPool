"""The pool dashboard: Azure Monitor platform metrics for a pool and its Foundry members.

APIM's metrics have no backend dimension, so the pool's own numbers are those of the APIs bound
to it (``Requests``, ``Duration`` and ``BackendDuration`` filtered by ``ApiId``): what its clients
saw. Each member that is an Azure OpenAI / Foundry deployment adds that deployment's metrics
(``ModelDeploymentName``). Those count every caller of the deployment, not only this pool.

Everything here is read-only and needs ``Microsoft.Insights/metrics/read`` on the APIM service
and the member accounts. Results are cached for a minute per pool and range.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from .arm import ArmClient, ArmError
from .config import Settings
from .models import (
    GatewayMetrics,
    LatencyPoint,
    MemberMetrics,
    MetricsRange,
    PoolMemberView,
    PoolMetrics,
    TrafficPoint,
)
from .service import PoolManagerService

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
    # 0: the client went away before APIM answered.
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


def _error_text(error: ArmError, what: str) -> str:
    if error.status in {401, 403}:
        return f"Cannot read metrics of {what}: {METRICS_READ} is needed (Monitoring Reader has it)"
    return f"Cannot read metrics of {what}: {error.message}"


def _weighted(values: list[tuple[float, float]]) -> float | None:
    """Average of (value, weight) pairs; None without weight."""
    weight = sum(w for _, w in values)
    return sum(v * w for v, w in values) / weight if weight else None


class PoolMonitor:
    def __init__(self, settings: Settings, arm: ArmClient, service: PoolManagerService) -> None:
        self._settings = settings
        self._arm = arm
        self._service = service
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
        pool = self._service.get_pool(pool_id)
        api_ids = sorted(
            api.id
            for api in self._service.list_apis()
            if api.binding and api.binding.pool_id.casefold() == pool.id.casefold()
        )
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

        with ThreadPoolExecutor(max_workers=_WORKERS) as executor:
            gateway_future = (
                executor.submit(self._gateway, api_ids, window, interval) if api_ids else None
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
            gateway_error: str | None = None
            if gateway_future is not None:
                try:
                    gateway = gateway_future.result()
                except ArmError as error:
                    gateway_error = _error_text(error, self._settings.apim_name)
                    logger.info("Gateway metrics for pool %s: %s", pool_id, error.message)

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
                )
            )
        return PoolMetrics(
            pool_id=pool.id,
            range=range_,
            interval=interval,
            start=f"{start:%Y-%m-%dT%H:%M:%SZ}",
            end=f"{end:%Y-%m-%dT%H:%M:%SZ}",
            api_ids=api_ids,
            gateway=gateway,
            gateway_error=gateway_error,
            members=members,
        )

    # -- APIM ----------------------------------------------------------------------------

    def _gateway(self, api_ids: list[str], window: str, interval: str) -> GatewayMetrics:
        apim = self._arm.resource_id
        counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        stamps_seen: set[str] = set()
        api_requests: dict[tuple[str, str], int] = defaultdict(int)
        latency: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(
            lambda: {"Duration": [], "BackendDuration": []}
        )
        for api_id in api_ids:
            api_filter = f"ApiId eq '{_quote(api_id)}'"
            for series in _query(
                self._arm,
                apim,
                ["Requests"],
                "Total",
                window,
                interval,
                f"{api_filter} and GatewayResponseCode eq '*'",
            ):
                kind = _code_kind(series.dimensions.get("gatewayresponsecode", ""))
                for stamp, value in series.points:
                    count = round(value or 0)
                    counts[stamp][kind] += count
                    stamps_seen.add(stamp)
                    api_requests[(api_id, stamp)] += count
            # Unsplit, so every point of the window is present; the time axis comes from here.
            for series in _query(
                self._arm,
                apim,
                ["Duration", "BackendDuration"],
                "Average",
                window,
                interval,
                api_filter,
            ):
                for stamp, value in series.points:
                    stamps_seen.add(stamp)
                    if value is not None and series.metric in latency[stamp]:
                        # Weighted by requests, so a quiet API doesn't skew a busy one; a lone
                        # API keeps its own average.
                        weight = api_requests.get((api_id, stamp), 0) or (
                            1 if len(api_ids) == 1 else 0
                        )
                        latency[stamp][series.metric].append((float(value), float(weight)))

        stamps = sorted(stamps_seen)
        traffic = [
            TrafficPoint(
                time=stamp,
                success=counts[stamp]["success"],
                throttled=counts[stamp]["throttled"],
                client_errors=counts[stamp]["client_errors"],
                server_errors=counts[stamp]["server_errors"],
            )
            for stamp in stamps
        ]
        totals = {
            kind: sum(getattr(point, kind) for point in traffic)
            for kind in ("success", "throttled", "client_errors", "server_errors")
        }
        requests = sum(totals.values())

        def overall(metric: str) -> float | None:
            pairs: list[tuple[float, float]] = []
            for stamp in stamps:
                value = _weighted(latency[stamp][metric])
                weight = sum(api_requests.get((api, stamp), 0) for api in api_ids)
                if value is not None and weight:
                    pairs.append((value, float(weight)))
            return _weighted(pairs)

        return GatewayMetrics(
            requests=requests,
            success=totals["success"],
            throttled=totals["throttled"],
            client_errors=totals["client_errors"],
            server_errors=totals["server_errors"],
            availability=(requests - totals["server_errors"]) / requests if requests else None,
            duration_ms=overall("Duration"),
            backend_duration_ms=overall("BackendDuration"),
            traffic=traffic,
            latency=[
                LatencyPoint(
                    time=stamp,
                    overall_ms=_weighted(latency[stamp]["Duration"]),
                    backend_ms=_weighted(latency[stamp]["BackendDuration"]),
                )
                for stamp in stamps
            ],
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
