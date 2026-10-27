"""ARM request bodies and readback parsing for APIM backends (Single and Pool backends, and
their circuit breakers)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Literal

from ..models import (
    PoolMemberView,
)
from .models import (
    BackendView,
    CircuitBreaker,
    FailureStatusCodeRange,
    MemberBackendWrite,
    PoolView,
    PoolWrite,
)

MANAGED_MARKER = "Managed by APIM Backend Pool Manager."
_MI_MARKER = "auth=managed_identity"
_CIRCUIT_RULE_NAME = "deployment-fault-isolation"
_NAMED_VALUE = re.compile(r"^\{\{([A-Za-z0-9._-]+)\}\}$")
_BEARER_NAMED_VALUE = re.compile(r"^Bearer \{\{([A-Za-z0-9._-]+)\}\}$")


def is_protected(resource_id: str, protected_prefixes: list[str]) -> bool:
    return any(resource_id.casefold().startswith(prefix) for prefix in protected_prefixes)


def is_managed(properties: Mapping[str, Any]) -> bool:
    return str(properties.get("description") or "").startswith(MANAGED_MARKER)


def session_cookie_name(pool_id: str) -> str:
    # Stable per pool: a hash of the pool ID, so the cookie name never changes for a pool.
    return f"PoolAffinity-{hashlib.sha256(pool_id.casefold().encode()).hexdigest()[:16]}"


def credential_named_value_id(backend_id: str) -> str:
    candidate = f"{backend_id}-credential"
    if len(candidate) <= 80:
        return candidate
    digest = hashlib.sha256(backend_id.encode()).hexdigest()[:8]
    return f"{backend_id[:60]}-{digest}-credential"


def circuit_breaker_properties(breaker: CircuitBreaker) -> dict[str, Any]:
    return {
        "rules": [
            {
                "name": _CIRCUIT_RULE_NAME,
                "failureCondition": {
                    "count": breaker.failure_count,
                    "interval": f"PT{breaker.interval_seconds}S",
                    "statusCodeRanges": [
                        {"min": item.minimum, "max": item.maximum}
                        for item in breaker.status_code_ranges
                    ],
                    "errorReasons": list(breaker.error_reasons),
                },
                "tripDuration": f"PT{breaker.trip_duration_seconds}S",
                "acceptRetryAfter": breaker.accept_retry_after,
            }
        ]
    }


def single_backend_properties(
    write: MemberBackendWrite, backend_id: str, named_value_id: str | None
) -> dict[str, Any]:
    description = MANAGED_MARKER
    if write.auth.type == "managed_identity":
        description = f"{MANAGED_MARKER} {_MI_MARKER}"
    properties: dict[str, Any] = {
        "title": write.title or backend_id,
        "description": description,
        "type": "Single",
        "protocol": "http",
        "url": str(write.url).rstrip("/"),
        "tls": {"validateCertificateChain": True, "validateCertificateName": True},
    }
    if write.auth.type == "api_key" and named_value_id:
        properties["credentials"] = {
            "header": {write.auth.header_name: [f"{{{{{named_value_id}}}}}"]}
        }
    elif write.auth.type == "bearer" and named_value_id:
        properties["credentials"] = {
            "header": {"Authorization": [f"Bearer {{{{{named_value_id}}}}}"]}
        }
    if write.circuit_breaker is not None:
        properties["circuitBreaker"] = circuit_breaker_properties(write.circuit_breaker)
    return properties


_API_FOR_POOL = f"{MANAGED_MARKER} Endpoint for backend pool "


def api_properties(display_name: str, path: str, pool_id: str) -> dict[str, Any]:
    """An HTTP API whose backend is chosen by the pool binding, so it needs no service URL.

    Clients authenticate with an APIM subscription key, as on any new API.
    """
    return {
        "displayName": display_name,
        "description": f"{_API_FOR_POOL}{pool_id}.",
        "path": path,
        "protocols": ["https"],
        "subscriptionRequired": True,
    }


def api_pool_id(properties: Mapping[str, Any]) -> str | None:
    """The pool an API was created for by ``api_properties``; ``None`` for any other API."""
    description = str(properties.get("description") or "")
    if not description.startswith(_API_FOR_POOL) or not description.endswith("."):
        return None
    return description[len(_API_FOR_POOL) : -1] or None


# One POST wildcard covers every Azure OpenAI per-deployment call (/chat/completions,
# /embeddings, ...) behind the member URLs, and nothing else.
WILDCARD_OPERATION_ID = "post-wildcard"
WILDCARD_OPERATION = {"displayName": "POST /*", "method": "POST", "urlTemplate": "/*"}


def pool_properties(
    write: PoolWrite,
    pool_id: str,
    resource_id_base: str,
    existing: Mapping[str, Any] | None,
) -> dict[str, Any]:
    pool: dict[str, Any] = {
        "services": [
            {
                "id": f"{resource_id_base}/backends/{member.backend_id}",
                # APIM priority is 1-based; the API keeps a 0-based priority.
                "priority": member.priority + 1,
                "weight": member.weight,
            }
            for member in write.members
        ]
    }
    if write.session_affinity:
        pool["sessionAffinity"] = {
            "sessionId": {"source": "cookie", "name": session_cookie_name(pool_id)}
        }
    previous = existing or {}
    # New pools and pools this app created keep the ownership marker; adopted pools
    # keep whatever description their author chose unless the operator replaces it.
    owned = existing is None or is_managed(previous)
    if write.description is None:
        description = str(previous.get("description") or "") or MANAGED_MARKER
    elif owned:
        description = f"{MANAGED_MARKER} {write.description}".rstrip()
    else:
        description = write.description
    properties: dict[str, Any] = {
        "title": write.title or previous.get("title") or pool_id,
        "type": "Pool",
        "pool": pool,
    }
    if description:  # APIM rejects an empty description.
        properties["description"] = description[:2000]
    return properties


def _duration_seconds(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        value,
    )
    if match is None or not any(match.groupdict().values()):
        return None
    parts = {name: int(raw or 0) for name, raw in match.groupdict().items()}
    return (
        parts["days"] * 86_400 + parts["hours"] * 3_600 + parts["minutes"] * 60 + parts["seconds"]
    )


def parse_circuit_breaker(value: object) -> CircuitBreaker | None:
    """Returns the first rule when it fits the editable model, otherwise ``None``."""
    if not isinstance(value, Mapping):
        return None
    rules = value.get("rules")
    if not isinstance(rules, list) or not rules or not isinstance(rules[0], Mapping):
        return None
    rule = rules[0]
    condition = rule.get("failureCondition") or {}
    interval = _duration_seconds(condition.get("interval"))
    trip = _duration_seconds(rule.get("tripDuration"))
    if interval is None or trip is None or not isinstance(condition.get("count"), int):
        return None
    try:
        return CircuitBreaker(
            failure_count=condition["count"],
            interval_seconds=interval,
            trip_duration_seconds=trip,
            accept_retry_after=bool(rule.get("acceptRetryAfter", False)),
            status_code_ranges=[
                FailureStatusCodeRange(minimum=int(item["min"]), maximum=int(item["max"]))
                for item in condition.get("statusCodeRanges") or []
            ],
            error_reasons=list(condition.get("errorReasons") or []),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _auth(properties: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    """Returns ``(auth_type, header_name, named_value)``."""
    credentials = properties.get("credentials")
    if not isinstance(credentials, Mapping):
        credentials = {}
    headers = credentials.get("header") or {}
    # Query, certificate or authorization credentials are shown but not editable here.
    other = [key for key, value in credentials.items() if value and key != "header"]
    if not headers and not other:
        described = _MI_MARKER in str(properties.get("description") or "")
        return ("managed_identity" if described else "none"), None, None
    if other or len(headers) != 1:
        return "custom", None, None
    name, values = next(iter(headers.items()))
    value = values[0] if isinstance(values, list) and len(values) == 1 else None
    if not isinstance(value, str):
        return "custom", name, None
    if name.casefold() == "authorization" and (match := _BEARER_NAMED_VALUE.fullmatch(value)):
        return "bearer", name, match.group(1)
    if match := _NAMED_VALUE.fullmatch(value):
        return "api_key", name, match.group(1)
    return "custom", name, None


def parse_backend(
    body: Mapping[str, Any], etag: str | None, protected_prefixes: list[str]
) -> BackendView:
    backend_id = str(body.get("name") or "")
    properties = body.get("properties") or {}
    kind: Literal["Single", "Pool"] = "Pool" if properties.get("type") == "Pool" else "Single"
    auth_type, header_name, named_value = (
        _auth(properties) if kind == "Single" else ("none", None, None)
    )
    return BackendView(
        id=backend_id,
        title=properties.get("title"),
        description=properties.get("description"),
        type=kind,
        url=properties.get("url"),
        auth_type=auth_type,  # type: ignore[arg-type]
        header_name=header_name,
        named_value=named_value,
        circuit_breaker=parse_circuit_breaker(properties.get("circuitBreaker")),
        has_circuit_breaker=bool((properties.get("circuitBreaker") or {}).get("rules")),
        managed=is_managed(properties),
        protected=is_protected(backend_id, protected_prefixes),
        etag=etag,
    )


def parse_pool(
    body: Mapping[str, Any], etag: str | None, protected_prefixes: list[str]
) -> PoolView:
    pool_id = str(body.get("name") or "")
    properties = body.get("properties") or {}
    pool = properties.get("pool") or {}
    affinity = (pool.get("sessionAffinity") or {}).get("sessionId") or {}
    members = [
        PoolMemberView(
            backend_id=str(item.get("id", "")).rsplit("/", 1)[-1],
            # APIM may return null priority/weight; treat as top priority, weight 1.
            priority=max(int(item.get("priority") or 1) - 1, 0),
            weight=int(item["weight"]) if item.get("weight") is not None else 1,
        )
        for item in pool.get("services") or []
        if isinstance(item, Mapping)
    ]
    return PoolView(
        id=pool_id,
        title=properties.get("title"),
        description=properties.get("description"),
        members=members,
        session_affinity=bool(affinity),
        session_cookie_name=affinity.get("name"),
        managed=is_managed(properties),
        protected=is_protected(pool_id, protected_prefixes),
        etag=etag,
    )
