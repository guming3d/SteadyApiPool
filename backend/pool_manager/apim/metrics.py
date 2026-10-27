"""The gateway's half of a pool's dashboard, from APIM's platform metrics.

APIM's metrics have no backend dimension, so the pool's own numbers are those of the APIs bound
to it (``Requests``, ``Duration`` and ``BackendDuration`` filtered by ``ApiId``): what its clients
saw. Reading them needs ``Microsoft.Insights/metrics/read`` on the APIM service.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from ..arm import ArmClient, ArmError
from ..config import Settings
from ..metrics import (
    GatewayReading,
    MonitoredPool,
    PoolMonitor,
    Window,
    code_kind,
    error_text,
    query,
    quote,
    weighted,
)
from ..models import GatewayMetrics, LatencyPoint, TrafficPoint
from .service import ApimService

logger = logging.getLogger(__name__)


class ApimMonitor(PoolMonitor):
    def __init__(self, settings: Settings, arm: ArmClient, service: ApimService) -> None:
        super().__init__(settings, arm, service)
        self._service = service

    def pool(self, pool_id: str) -> tuple[MonitoredPool, list[str]]:
        pool = self._service.get_pool(pool_id)
        api_ids = sorted(
            api.id
            for api in self._service.list_apis()
            if api.binding and api.binding.pool_id.casefold() == pool.id.casefold()
        )
        return pool, api_ids

    def gateway(self, pool: MonitoredPool, names: list[str], window: Window) -> GatewayReading:
        if not names:
            return GatewayReading(None)  # No API is bound, so no client reaches the pool yet.
        try:
            return GatewayReading(self._gateway(names, window.timespan, window.interval))
        except ArmError as error:
            logger.info("Gateway metrics for pool %s: %s", pool.id, error.message)
            return GatewayReading(None, error_text(error, self._settings.apim_name))

    def _gateway(self, api_ids: list[str], window: str, interval: str) -> GatewayMetrics:
        apim = self._arm.resource_id
        counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        stamps_seen: set[str] = set()
        api_requests: dict[tuple[str, str], int] = defaultdict(int)
        latency: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(
            lambda: {"Duration": [], "BackendDuration": []}
        )
        for api_id in api_ids:
            api_filter = f"ApiId eq '{quote(api_id)}'"
            for series in query(
                self._arm,
                apim,
                ["Requests"],
                "Total",
                window,
                interval,
                f"{api_filter} and GatewayResponseCode eq '*'",
            ):
                kind = code_kind(series.dimensions.get("gatewayresponsecode", ""))
                for stamp, value in series.points:
                    count = round(value or 0)
                    counts[stamp][kind] += count
                    stamps_seen.add(stamp)
                    api_requests[(api_id, stamp)] += count
            # Unsplit, so every point of the window is present; the time axis comes from here.
            for series in query(
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
                value = weighted(latency[stamp][metric])
                weight = sum(api_requests.get((api, stamp), 0) for api in api_ids)
                if value is not None and weight:
                    pairs.append((value, float(weight)))
            return weighted(pairs)

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
                    overall_ms=weighted(latency[stamp]["Duration"]),
                    backend_ms=weighted(latency[stamp]["BackendDuration"]),
                )
                for stamp in stamps
            ],
        )
