"""Pure rules shared with the web UI, ported so the terminal and the browser agree.

- Pool modes: ``frontend/src/pool-modes.ts`` (failover / balanced / weighted from priority and
  weight, adding, removing and promoting members, validation, API path suggestions).
- Numbers and health: ``frontend/src/pool-metrics.ts``.
- Test-call diagnosis: ``frontend/src/api-test.ts``.
- Who the top bar names: ``frontend/src/identity.ts``.
- The pool's circuit breaker: ``frontend/src/pool-breaker.ts``.
- Deploying a model: ``frontend/src/deploy-model.ts``.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from typing import Any, Literal

Mode = Literal["failover", "balanced", "weighted", "custom"]
Tone = Literal["ok", "warn", "bad"]

MIN_MEMBERS = 2
MAX_MEMBERS = 30
MODES: tuple[Literal["failover", "balanced", "weighted"], ...] = (
    "failover",
    "balanced",
    "weighted",
)
# Same rules as RESOURCE_ID_PATTERN and API_PATH_PATTERN in backend/pool_manager/models.py.
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,79}$")
API_PATH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*(?:/[A-Za-z0-9._~-]+)*$")
# Audience of the token APIM's managed identity sends to Foundry members.
COGNITIVE_SERVICES_RESOURCE = "https://cognitiveservices.azure.com"


@dataclass(frozen=True)
class Member:
    backend_id: str
    priority: int
    weight: int

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> Member:
        return cls(str(value["backend_id"]), int(value["priority"]), int(value["weight"]))

    def to_json(self) -> dict[str, Any]:
        return {"backend_id": self.backend_id, "priority": self.priority, "weight": self.weight}


# -- pool modes ------------------------------------------------------------------------------


def primary_first(members: list[Member]) -> list[Member]:
    """Lowest priority first; stable, so equal priorities keep the order APIM returned."""
    return sorted(members, key=lambda member: member.priority)


def deployment_mode(members: list[Member]) -> Mode:
    if len(members) < 2:
        return "failover"
    primary, replicas = members[0], members[1:]
    equal_weights = len({member.weight for member in members}) == 1
    if len({member.priority for member in members}) == 1:
        return "balanced" if equal_weights else "weighted"
    if (
        equal_weights
        and all(member.priority > primary.priority for member in replicas)
        and len({member.priority for member in replicas}) == 1
    ):
        return "failover"
    return "custom"


def members_for_mode(
    members: list[Member], mode: Literal["failover", "balanced", "weighted"]
) -> list[Member]:
    primary = members[0].backend_id if members else ""
    custom_weights = len({member.weight for member in members}) > 1
    result = []
    for member in members:
        if mode == "weighted":
            weight = (
                max(1, member.weight)
                if custom_weights
                else (2 if member.backend_id == primary else 1)
            )
        else:
            weight = 1
        priority = 1 if mode == "failover" and member.backend_id != primary else 0
        result.append(replace(member, priority=priority, weight=weight))
    return result


def add_member(members: list[Member], backend_id: str, mode: Mode | None) -> list[Member]:
    next_mode = mode if mode in {"balanced", "weighted"} else "failover"
    priority = 0 if next_mode != "failover" or not members else 1
    return [*members, Member(backend_id, priority, 1)]


def append_members(members: list[Member], backend_ids: list[str]) -> tuple[list[Member], list[str]]:
    """Adds backends that aren't members yet, in the mode the pool is already in."""
    present = {member.backend_id.casefold() for member in members}
    mode = deployment_mode(members) if members else None
    added: list[str] = []
    for backend_id in backend_ids:
        if backend_id.casefold() in present:
            continue
        present.add(backend_id.casefold())
        members = add_member(members, backend_id, mode)
        added.append(backend_id)
    return members, added


def remove_member(members: list[Member], backend_id: str, mode: Mode | None) -> list[Member]:
    remaining = [member for member in members if member.backend_id != backend_id]
    # Removing the primary promotes the next member; re-derive priorities so failover holds.
    if mode and mode != "custom" and members and members[0].backend_id == backend_id:
        return members_for_mode(remaining, mode)
    return remaining


def set_primary(members: list[Member], backend_id: str, mode: Mode | None) -> list[Member]:
    chosen = next((member for member in members if member.backend_id == backend_id), None)
    if chosen is None:
        return members
    reordered = [chosen, *(member for member in members if member.backend_id != backend_id)]
    return members_for_mode(reordered, mode) if mode and mode != "custom" else reordered


def change_weight(members: list[Member], backend_id: str, weight: int) -> list[Member]:
    clamped = min(100, max(1, weight))
    return [
        replace(member, weight=clamped) if member.backend_id == backend_id else member
        for member in members
    ]


def traffic_shares(members: list[Member]) -> list[float]:
    """Traffic each member gets while all are healthy: the highest priority shares it by
    weight, the rest stand by (same as expected_shares in backend/pool_manager/metrics.py)."""
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


def role(members: list[Member], index: int) -> str:
    """Primary / standby / active, as the web UI labels members."""
    shares = traffic_shares(members)
    mode = deployment_mode(members)
    if mode == "failover":
        return "primary" if index == 0 else "standby"
    return "active" if shares[index] > 0 else "standby"


def draft_issues(members: list[Member], mode: Mode | None) -> list[str]:
    issues = []
    if not MIN_MEMBERS <= len(members) <= MAX_MEMBERS:
        issues.append(f"a pool needs {MIN_MEMBERS} to {MAX_MEMBERS} members")
    if mode == "weighted" and len({member.weight for member in members}) < 2:
        issues.append("weighted mode needs members with different weights")
    return issues


def slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9._~-]+", "-", value.strip().lower())
    text = re.sub(r"-{2,}", "-", text)
    return re.sub(r"^[^a-z0-9]+|-+$", "", text)


def suggest_api_path(title: str | None, pool_id: str, prefix: str) -> str:
    """A readable API path for a new pool: its title, else its ID without the app's prefix."""
    bare = (
        pool_id[len(prefix) :] if prefix and pool_id.lower().startswith(prefix.lower()) else pool_id
    )
    return slug(title or "") or slug(bare) or slug(pool_id)


def suggest_pool_id(model: str, prefix: str) -> str:
    candidate = re.sub(r"[^a-z0-9-]+", "-", f"{prefix}{model}".lower()).strip("-")
    return re.sub(r"-{2,}", "-", candidate)[:80]


# -- numbers -----------------------------------------------------------------------------------


def compact(value: float | None) -> str:
    """1,284 -> "1,284"; 12,900 -> "12.9K"; 4,200,000 -> "4.2M"."""
    if value is None:
        return "—"
    if abs(value) < 10_000:
        return f"{round(value):,}"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            scaled = value / limit
            text = str(round(scaled)) if scaled >= 100 else f"{scaled:.1f}".removesuffix(".0")
            return f"{text}{suffix}"
    return str(value)


def ms(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 1000:
        return f"{round(value)} ms"
    return f"{value / 1000:.{2 if value < 10_000 else 1}f} s"


def percent(value: float | None, digits: int = 1) -> str:
    """Never rounds to a false 100%."""
    if value is None:
        return "—"
    scaled = value * 100
    text = f"{scaled:.{digits}f}"
    if scaled < 100 and float(text) >= 100:
        text = f"{100 - 10**-digits:.{digits}f}"
    return f"{text.rstrip('0').rstrip('.') if '.' in text else text}%"


def availability_tone(availability: float | None) -> Tone:
    if availability is None:
        return "ok"
    return "ok" if availability >= 0.999 else "warn" if availability >= 0.99 else "bad"


def throttle_tone(throttled: int, requests: int) -> Tone:
    if not requests:
        return "ok"
    rate = throttled / requests
    return "ok" if rate < 0.001 else "warn" if rate < 0.05 else "bad"


def share_tone(actual: float | None, expected: float) -> Tone:
    if actual is None:
        return "ok"
    gap = abs(actual - expected)
    return "ok" if gap < 0.15 else "warn" if gap < 0.3 else "bad"


def bar(fraction: float | None, width: int = 10) -> str:
    """A text meter: ``███████░░░``."""
    if fraction is None:
        return "░" * width
    filled = max(0, min(width, math.floor(fraction * width + 0.5)))
    return "█" * filled + "░" * (width - filled)


# -- test calls --------------------------------------------------------------------------------

DIAGNOSES = {
    "key": "APIM refused the request: the subscription key is missing or invalid.",
    "member_access": "A member refused APIM's call. Usually APIM's managed identity lacks "
    "Cognitive Services OpenAI User on the Foundry account (a new role takes up to 10 minutes).",
    "not_found": "Not found: check the path (e.g. chat/completions) and the api-version.",
    "throttled": "Every member is throttling (429). Retry later, or add members to the pool.",
    "no_identity": "APIM answered 500. Usually APIM's managed identity is off, or a member URL "
    "is unreachable.",
    "unavailable": "No member is available (503): they may all have tripped their circuit breaker.",
    "timeout": "The request timed out: no member answered within the backend timeout.",
}


def diagnose(status: int, body: str) -> str:
    """ok, key, member_access, not_found, throttled, no_identity, unavailable, timeout, other.

    APIM itself answers a missing or wrong key with 401 and a message about the subscription
    key; any other 401/403 comes from a member."""
    lowered = body.lower()
    if 200 <= status < 300:
        return "ok"
    if status == 401 and "subscription key" in lowered:
        return "key"
    if status in {401, 403}:
        return "member_access"
    return {404: "not_found", 429: "throttled", 500: "no_identity", 503: "unavailable"}.get(
        status, "timeout" if status in {408, 504} else "other"
    )


def pretty(body: str) -> str:
    try:
        return json.dumps(json.loads(body), indent=2)
    except ValueError:
        return body


def default_test_body(model: str) -> str:
    return json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "Hello"}]}, indent=2
    )


SPARK_LEVELS = "▁▂▃▄▅▆▇█"


def spark(values: list[float], width: int) -> str:
    """A one-line sparkline scaled from zero to the peak (so a dip isn't drawn as no traffic),
    resampled to ``width`` columns by averaging. Intervals with nothing are blank."""
    if not values or width <= 0:
        return ""
    if len(values) > width:
        size = len(values) / width
        values = [
            sum(chunk) / len(chunk)
            for index in range(width)
            if (
                chunk := values[
                    round(index * size) : max(round((index + 1) * size), round(index * size) + 1)
                ]
            )
        ]
    peak = max(values)
    if peak <= 0:
        return " " * len(values)
    top = len(SPARK_LEVELS) - 1
    return "".join(
        " " if value <= 0 else SPARK_LEVELS[min(top, int(value / peak * top + 0.5))]
        for value in values
    )


def display_user(me: dict[str, Any], azure: dict[str, Any] | None) -> str | None:
    """The top bar's name: the signed-in user, or locally (page sign-in off, so ``me`` is a
    placeholder) the Azure account the app calls Azure with; None locally without one. The
    public app's guest view is "guest"."""
    if me.get("guest"):
        return "guest"
    if me.get("authenticated"):
        return str(me.get("name") or "")
    name = (azure or {}).get("name") or (azure or {}).get("object_id")
    return str(name) if name else None


# The pool's circuit breaker. APIM keeps a breaker on each member; the app treats it as one pool
# setting that publishing writes to every member it manages (see frontend/src/pool-breaker.ts).

# The app's platform breaker; the same as the backend's CircuitBreaker defaults.
DEFAULT_BREAKER: dict[str, Any] = {
    "failure_count": 1,
    "interval_seconds": 60,
    "trip_duration_seconds": 60,
    "accept_retry_after": True,
    "status_code_ranges": [
        {"minimum": 429, "maximum": 429},
        {"minimum": 408, "maximum": 408},
        {"minimum": 500, "maximum": 599},
    ],
    "error_reasons": ["BackendConnectionFailure", "Timeout"],
}


def follows_pool(backend: dict[str, Any] | None) -> bool:
    """Publishing writes the pool's rule to this member; others are read-only here."""
    return backend is not None and bool(backend.get("managed")) and not backend.get("protected")


def pool_breaker(
    member_ids: list[str], backends: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], bool]:
    """The rule the most managed members share (a tie goes to the earlier member), else the
    default; and whether they all have it already. ``backends`` is keyed by casefolded ID."""
    rules = [
        backend.get("circuit_breaker")
        for backend in (backends.get(member_id.casefold()) for member_id in member_ids)
        if backend is not None and follows_pool(backend)
    ]
    counts: list[tuple[dict[str, Any], int]] = []
    for rule in rules:
        if not rule:
            continue
        index = next((i for i, (seen, _) in enumerate(counts) if seen == rule), None)
        if index is None:
            counts.append((rule, 1))
        else:
            counts[index] = (rule, counts[index][1] + 1)
    best = max(counts, key=lambda entry: entry[1], default=None)
    # max() keeps the first of equal counts, which is the earlier member.
    rule = best[0] if best else DEFAULT_BREAKER
    return rule, all(candidate == rule for candidate in rules)


def members_to_update(
    member_ids: list[str], backends: dict[str, dict[str, Any]], rule: dict[str, Any]
) -> list[str]:
    """Managed members whose rule publishing ``rule`` would rewrite."""
    return [
        member_id
        for member_id in member_ids
        if follows_pool(backends.get(member_id.casefold()))
        and backends[member_id.casefold()].get("circuit_breaker") != rule
    ]


def read_only_members(member_ids: list[str], backends: dict[str, dict[str, Any]]) -> list[str]:
    """Members publishing can't update: they keep whatever rule they have."""
    return [
        member_id
        for member_id in member_ids
        if member_id.casefold() in backends and not follows_pool(backends[member_id.casefold()])
    ]


# Deploying a model into a Foundry account (see frontend/src/deploy-model.ts).

# Same as DEPLOYMENT_NAME_PATTERN in backend/pool_manager/models.py.
DEPLOYMENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
_DEPLOYMENT_NAME_MAX = 64


def default_version(models: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The version to preselect: the default one, else the first listed."""
    versions = [model for model in models if model["name"] == name]
    return next((model for model in versions if model.get("is_default_version")), None) or (
        versions[0] if versions else None
    )


def suggest_deployment_name(model: str, taken: list[str]) -> str:
    """A free deployment name: the model's own, else model-2, model-3, ... (64 at most)."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", model)
    base = re.sub(r"^[^A-Za-z0-9]+", "", base)[:_DEPLOYMENT_NAME_MAX] or "deployment"
    used = {name.casefold() for name in taken}
    if base.casefold() not in used:
        return base
    index = 2
    while True:
        suffix = f"-{index}"
        candidate = f"{base[: _DEPLOYMENT_NAME_MAX - len(suffix)]}{suffix}"
        if candidate.casefold() not in used:
            return candidate
        index += 1


def capacity_limit(sku: dict[str, Any], available: int | None) -> int:
    """The type's maximum, capped by the quota left when known."""
    maximum = int(sku["max_capacity"])
    return maximum if available is None else min(maximum, available)


def clamp_capacity(value: float, sku: dict[str, Any], available: int | None = None) -> int:
    """A capacity within the type's range and step, and the quota left."""
    minimum, step = int(sku["min_capacity"]), int(sku["step"])
    limit = capacity_limit(sku, available)
    if limit < minimum:
        return minimum
    value = value if math.isfinite(value) else float(sku["default_capacity"])
    # Half away from zero, as Math.round does for these positive values.
    stepped = math.floor(value / step + 0.5) * step
    return int(min(max(stepped, minimum), limit))


def finished_state(state: str) -> bool:
    """Azure's final provisioning states; polling stops at any of them."""
    return state.casefold() in {"succeeded", "failed", "canceled"}
