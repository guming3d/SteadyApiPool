"""KQL queries on Log Analytics, for the gateways whose traffic is in logs rather than platform
metrics: LiteLLM's OpenTelemetry spans in its Application Insights component, and AI Gateway's
OTelSpans and OTelLogs tables in the workspace behind its Application Insights.

Queries use the resource-centric API (``/v1<resource id>/query``) with a token for Log
Analytics from the same credential as ARM; reading needs Log Analytics read on the resource.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import httpx

from .arm import ArmClient, ArmError

LOGS_URL = "https://api.loganalytics.azure.com/v1"
LOGS_SCOPE = "https://api.loganalytics.io/.default"
_LOGS_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# Bin sizes per range for KQL (the same grain as the Azure Monitor charts).
KQL_BINS = {"PT1M": "1m", "PT5M": "5m", "PT15M": "15m", "PT1H": "1h"}


def kql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def innermost(error: object) -> str | None:
    """Log Analytics nests the useful part ("Query could not be parsed at ...") under
    ``innererror``; the top message is only "The request had some invalid properties"."""
    message = None
    while isinstance(error, dict):
        message = str(error.get("message") or "") or message
        error = error.get("innererror")
    return message


def rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    tables = body.get("tables") or []
    if not tables:
        return []
    columns = [str(column.get("name")) for column in tables[0].get("columns") or []]
    return [dict(zip(columns, row, strict=False)) for row in tables[0].get("rows") or []]


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def stamp(value: object) -> str:
    """KQL's ISO timestamps, in the same form as Azure Monitor's (no fraction)."""
    text = str(value or "")
    return text.split(".")[0].rstrip("Z") + "Z" if text else text


class LogsReader:
    """Runs KQL on one resource's logs (a component or a workspace)."""

    def __init__(self, arm: ArmClient, transport: httpx.BaseTransport | None = None) -> None:
        self._arm = arm
        self._http = httpx.Client(timeout=_LOGS_TIMEOUT, transport=transport)

    def query(self, resource_id: str, query: str, start: str, end: str) -> list[dict[str, Any]]:
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
                message = innermost(response.json().get("error")) or message
            raise ArmError(response.status_code, "LogsQueryFailed", message)
        body: dict[str, Any] = response.json()
        return rows(body)
