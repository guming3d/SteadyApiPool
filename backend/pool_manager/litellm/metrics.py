"""The gateway's half of a pool's dashboard, from LiteLLM's OpenTelemetry spans.

The gateway's collector sends them to Application Insights: one span per attempt on a member
(``chat <model>``), with the member's host and deployment, the outcome and the tokens. Spans of
one client request share a trace (``operation_Id``), so the requests clients saw, and those a
failover saved, are counted from them. LiteLLM's own spend logs are not used: they drop rows.

Reading them needs Log Analytics read on the Application Insights component.
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
from ..metrics import GatewayReading, MonitoredPool, PoolMonitor, Window, weighted
from ..models import GatewayMetrics, LatencyPoint, TrafficPoint
from .resources import member_target
from .service import LiteLLMService

logger = logging.getLogger(__name__)

LOGS_URL = "https://api.loganalytics.azure.com/v1"
LOGS_SCOPE = "https://api.loganalytics.io/.default"
_LOGS_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# Span names LiteLLM gives model calls.
_MODEL_SPANS = ("chat ", "responses ", "embeddings ", "text_completion ", "completion ")


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
            return weighted(
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
