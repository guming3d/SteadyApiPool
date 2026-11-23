"""ARM request bodies and readbacks for AI Gateway: model providers, models (the members) and
aliases (the pools), and the pool's failure rule as the CEL expression the alias holds.

Child resources of an AI Gateway carry no tags, so what this app wrote is recognized by its
``description``, which starts with ``MANAGED_MARKER``. An alias's description also holds the
pool's title and description; a model's, the deployment URL it was imported from.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from ..models import PoolMemberView
from .models import (
    DEFAULT_RETRIES,
    BackendView,
    CircuitBreaker,
    FailureStatusCodeRange,
    MemberBackendWrite,
    PoolView,
    PoolWrite,
)

MANAGED_MARKER = "Managed by SteadyApiPool."
# The tag on gateways this app created (child resources take no tags).
MANAGED_TAG = ("managed-by", "steadyapipool")
WORKSPACE = "/workspaces/default"
# Audience of the token the gateway's identity sends to Foundry members.
COGNITIVE_SERVICES_RESOURCE = "https://cognitiveservices.azure.com/"
# What a Foundry chat model serves on the gateway's OpenAI v1 surface (as Microsoft's labs
# register them), and an embeddings model.
CHAT_ENDPOINTS = ("/openai/v1/chat/completions", "/openai/v1/responses")
EMBEDDING_ENDPOINTS = ("/openai/v1/embeddings",)
# A custom provider's paths are relative to its base URL (an OpenAI-compatible ``.../v1``).
CUSTOM_ENDPOINTS = ("/chat/completions",)
_TITLE = "Title: "
_URL = "url="
_DEPLOYMENTS = "/openai/deployments/"
# Child resource names: lower case letters, digits and hyphens (models also take dots).
_CHILD_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def is_protected(resource_id: str, protected_prefixes: list[str]) -> bool:
    return any(resource_id.casefold().startswith(prefix) for prefix in protected_prefixes)


def is_managed(properties: Mapping[str, Any]) -> bool:
    return str(properties.get("description") or "").startswith(MANAGED_MARKER)


def child_name(value: str, *, dots: bool = False, limit: int = 63) -> str:
    """A valid provider or model name from a member ID or account name: lower case, hyphens
    for anything else, and a stable hash when it is too long."""
    allowed = "a-z0-9.-" if dots else "a-z0-9-"
    slug = re.sub(rf"[^{allowed}]+", "-", value.casefold())
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    if len(slug) < 3:
        slug = f"{slug}-x"[:3].ljust(3, "x")
    if len(slug) > limit:
        digest = hashlib.sha256(value.encode()).hexdigest()[:8]
        slug = f"{slug[: limit - 9].rstrip('-.')}-{digest}"
    pattern = _MODEL_NAME if dots else _CHILD_NAME
    assert pattern.fullmatch(slug), slug
    return slug


def name_of(resource_id: str) -> str:
    return resource_id.rstrip("/").rpartition("/")[2]


def provider_of(model_id: str) -> str:
    """The model provider in a model registration's ARM ID."""
    parts = model_id.rstrip("/").split("/")
    lowered = [part.casefold() for part in parts]
    return parts[lowered.index("modelproviders") + 1] if "modelproviders" in lowered else ""


# -- the failure rule ------------------------------------------------------------------------


def _status(item: FailureStatusCodeRange) -> str:
    if item.minimum == item.maximum:
        return f"response.status_code == {item.minimum}"
    return f"(response.status_code >= {item.minimum} && response.status_code <= {item.maximum})"


def failure_condition(breaker: CircuitBreaker) -> str:
    """The rule's ``when``: a CEL expression over the backend's response, which is null when
    the backend sent no HTTP answer (a timeout, a refused connection)."""
    statuses = " || ".join(_status(item) for item in breaker.status_code_ranges)
    if breaker.no_response:
        expression = f"response == null || {statuses}" if statuses else "response == null"
    else:
        expression = f"response != null && ({statuses})"
    return f"@cel({expression})"


_RANGE = re.compile(
    r"\(response\.status_code >= (\d{3}) && response\.status_code <= (\d{3})\)"
    r"|response\.status_code == (\d{3})"
)


def _parse_statuses(text: str) -> list[FailureStatusCodeRange] | None:
    ranges: list[FailureStatusCodeRange] = []
    for part in text.split(" || "):
        match = _RANGE.fullmatch(part.strip())
        if not match:
            return None
        low, high, single = match.groups()
        minimum, maximum = (int(single), int(single)) if single else (int(low), int(high))
        ranges.append(FailureStatusCodeRange(minimum=minimum, maximum=maximum))
    return ranges


def parse_condition(when: str) -> tuple[list[FailureStatusCodeRange], bool] | None:
    """The status ranges and ``no_response`` of a rule this app wrote; None for any other."""
    match = re.fullmatch(r"@cel\((.*)\)", when.strip(), re.S)
    if not match:
        return None
    expression = match.group(1).strip()
    if expression == "response == null":
        return [], True
    if expression.startswith("response == null || "):
        ranges = _parse_statuses(expression.removeprefix("response == null || "))
        return (ranges, True) if ranges else None
    inner = re.fullmatch(r"response != null && \((.*)\)", expression)
    if inner:
        ranges = _parse_statuses(inner.group(1))
        return (ranges, False) if ranges else None
    return None


def iso_seconds(seconds: int) -> str:
    return f"PT{seconds}S"


def parse_iso_seconds(value: str) -> int | None:
    """Seconds in an ISO 8601 duration up to days (``PT60S``, ``PT1M30S``, ``P1D``)."""
    match = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip().upper()
    )
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(group or 0) for group in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def failure_rule(breaker: CircuitBreaker) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "when": failure_condition(breaker),
        "retryAttempts": breaker.same_member_retries,
    }
    if breaker.exclude_seconds:
        rule["excludeFor"] = iso_seconds(breaker.exclude_seconds)
    return rule


def parse_failures(failures: object) -> tuple[CircuitBreaker | None, bool]:
    """The pool's breaker from an alias's failure rules, and whether they are someone else's
    (several rules, or one this app wouldn't write)."""
    if not isinstance(failures, list) or not failures:
        return None, False
    if len(failures) > 1 or not isinstance(failures[0], Mapping):
        return None, True
    rule = failures[0]
    parsed = parse_condition(str(rule.get("when") or ""))
    exclude = rule.get("excludeFor")
    seconds = parse_iso_seconds(str(exclude)) if exclude else 0
    retries = rule.get("retryAttempts") or 0
    if parsed is None or seconds is None or not isinstance(retries, int) or retries > 10:
        return None, True
    if seconds > 86_400:
        return None, True
    ranges, no_response = parsed
    return (
        CircuitBreaker(
            status_code_ranges=ranges,
            no_response=no_response,
            exclude_seconds=seconds,
            same_member_retries=retries,
        ),
        False,
    )


# -- aliases (pools) -----------------------------------------------------------------------


def alias_description(title: str | None, description: str | None) -> str:
    lines = [MANAGED_MARKER]
    if title:
        lines.append(f"{_TITLE}{title.strip()}")
    if description:
        lines.append(description.strip())
    return "\n".join(lines)


def parse_description(text: str) -> tuple[str | None, str | None]:
    """(title, description) of a pool; anything after the marker for aliases this app didn't
    write is their description."""
    if not text.startswith(MANAGED_MARKER):
        return None, text.strip() or None
    lines = text.removeprefix(MANAGED_MARKER).strip("\n").split("\n")
    title = None
    if lines and lines[0].startswith(_TITLE):
        title = lines.pop(0).removeprefix(_TITLE).strip() or None
    rest = "\n".join(lines).strip()
    return title, rest or None


def alias_properties(
    pool_id: str,
    write: PoolWrite,
    member_ids: Mapping[str, str],
    existing: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The alias a pool is. ``member_ids`` maps each member's ID (case-folded) to its model
    registration's ARM ID. What the app doesn't manage (inline policies, session preference,
    automatic model selection) is kept as the alias has it."""
    current = dict(existing or {})
    for read_only in ("provisioningState", "supportedEndpoints", "gatewayEndpoints"):
        current.pop(read_only, None)
    breaker, _ = parse_failures(current.get("failures"))
    breaker = write.circuit_breaker or breaker or CircuitBreaker()
    retries = write.retries
    if retries is None:
        found = current.get("maxRetryAttempts")
        retries = found if isinstance(found, int) and existing is not None else DEFAULT_RETRIES
    order = current.get("selectionOrder")
    return {
        **current,
        "aliasName": current.get("aliasName") or pool_id,
        "description": alias_description(write.title, write.description),
        "backendModels": [
            {
                "resourceId": member_ids[member.backend_id.casefold()],
                "priority": member.priority,
                "weight": member.weight,
            }
            for member in write.members
        ],
        "selectionOrder": order if isinstance(order, list) and order else ["priority"],
        "failures": [failure_rule(breaker)],
        "maxRetryAttempts": max(retries, 0),
    }


def parse_pool(
    body: Mapping[str, Any],
    etag: str | None,
    protected_prefixes: list[str],
    member_of: Mapping[str, str],
) -> PoolView:
    """``member_of`` maps a model registration's ARM ID (case-folded) to its member ID."""
    pool_id = str(body.get("name") or "")
    properties = body.get("properties") or {}
    title, description = parse_description(str(properties.get("description") or ""))
    members: list[PoolMemberView] = []
    for target in properties.get("backendModels") or []:
        resource_id = str(target.get("resourceId") or "")
        priority = target.get("priority")
        weight = target.get("weight")
        members.append(
            PoolMemberView(
                backend_id=member_of.get(resource_id.casefold(), name_of(resource_id)),
                priority=priority if isinstance(priority, int) else 0,
                weight=weight if isinstance(weight, int) else 1,
            )
        )
    breaker, custom = parse_failures(properties.get("failures"))
    retries = properties.get("maxRetryAttempts")
    policies = [
        str(policy.get("type") or "")
        for policy in properties.get("policies") or []
        if isinstance(policy, Mapping)
    ]
    return PoolView(
        id=pool_id,
        model_name=str(properties.get("aliasName") or pool_id),
        title=title,
        description=description,
        members=members,
        circuit_breaker=breaker,
        custom_failures=custom,
        retries=retries if isinstance(retries, int) else None,
        policies=[policy for policy in policies if policy],
        managed=is_managed(properties),
        protected=is_protected(pool_id, protected_prefixes),
        etag=etag or (str(body["etag"]) if body.get("etag") else None),
    )


# -- model providers and models (members) ----------------------------------------------------


def foundry_provider_properties(account: Mapping[str, Any]) -> dict[str, Any]:
    """A Foundry provider for one account, called as the gateway's system-assigned identity."""
    account_id = str(account.get("id") or "")
    properties = account.get("properties") or {}
    endpoint = str(properties.get("endpoint") or "")
    return {
        "modelProviderType": "Foundry",
        "displayName": str(account.get("name") or name_of(account_id)),
        "description": f"{MANAGED_MARKER} Foundry account {name_of(account_id)}.",
        "foundry": {
            "endpoint": {"url": endpoint if endpoint.endswith("/") else f"{endpoint}/"},
            "resourceIds": [account_id],
        },
        "authentication": {"managedIdentity": {"resource": COGNITIVE_SERVICES_RESOURCE}},
    }


def custom_provider_properties(
    member_id: str, write: MemberBackendWrite, secret: str | None
) -> dict[str, Any]:
    """A custom provider for one member: its base URL and the secret header it sends. Without
    a ``secret`` (an update keeping the one the gateway has) the header goes without a value."""
    header: dict[str, Any] = {"name": write.auth.header_name}
    if write.auth.type == "bearer":
        header["name"] = "Authorization"
    if secret is not None:
        header["value"] = f"Bearer {secret}" if write.auth.type == "bearer" else secret
    return {
        "modelProviderType": "Custom",
        "displayName": write.title or member_id,
        "description": f"{MANAGED_MARKER} Endpoint of member {member_id}.",
        "custom": {"endpoint": {"url": str(write.url).rstrip("/")}},
        "authentication": {"headers": [header]},
    }


def foundry_model_properties(
    title: str, url: str, deployment_id: str, model_version: str | None, embeddings: bool
) -> dict[str, Any]:
    deployment: dict[str, Any] = {
        "resourceId": deployment_id,
        # The gateway calls the Foundry deployment this names.
        "modelName": name_of(deployment_id),
    }
    if model_version:
        deployment["modelVersion"] = model_version
    return {
        "displayName": title,
        "description": f"{MANAGED_MARKER} {_URL}{url}",
        "deployment": deployment,
        "supportedEndpoints": list(EMBEDDING_ENDPOINTS if embeddings else CHAT_ENDPOINTS),
    }


def custom_model_properties(member_id: str, write: MemberBackendWrite) -> dict[str, Any]:
    return {
        "displayName": write.title or member_id,
        "description": MANAGED_MARKER,
        "deployment": {"modelName": write.model},
        "supportedEndpoints": list(CUSTOM_ENDPOINTS),
    }


def _imported_url(description: str) -> str | None:
    marker = f"{MANAGED_MARKER} {_URL}"
    return (
        description.removeprefix(marker).strip() or None if description.startswith(marker) else None
    )


def deployment_url(provider: Mapping[str, Any], deployment_id: str) -> str | None:
    """Where a Foundry deployment is called, from its provider's endpoint, as discovery and the
    model check address deployments."""
    endpoint = str(((provider.get("foundry") or {}).get("endpoint") or {}).get("url") or "")
    if not endpoint or "/deployments/" not in deployment_id:
        return None
    return f"{endpoint.rstrip('/')}{_DEPLOYMENTS}{name_of(deployment_id)}"


def parse_model(
    body: Mapping[str, Any],
    etag: str | None,
    member_id: str,
    provider: Mapping[str, Any],
    pools: Iterable[str],
    protected_prefixes: list[str],
) -> BackendView:
    """A member. ``provider`` is its model provider's properties."""
    properties = body.get("properties") or {}
    deployment = properties.get("deployment") or {}
    kind_text = str(
        properties.get("providerKind") or provider.get("modelProviderType") or ""
    ).casefold()
    kind = "foundry" if kind_text == "foundry" else "github" if kind_text == "github" else "custom"
    deployment_id = str(deployment.get("resourceId") or "") or None
    description = str(properties.get("description") or "")
    header_name: str | None = None
    auth_type: Any = "custom"
    if kind == "foundry":
        url = _imported_url(description) or (
            deployment_url(provider, deployment_id) if deployment_id else None
        )
        if (provider.get("authentication") or {}).get("managedIdentity") is not None:
            auth_type = "managed_identity"
    else:
        url = str(((provider.get("custom") or {}).get("endpoint") or {}).get("url") or "") or None
        headers = (provider.get("authentication") or {}).get("headers") or []
        if len(headers) == 1 and isinstance(headers[0], Mapping):
            header_name = str(headers[0].get("name") or "") or None
            auth_type = "bearer" if (header_name or "").casefold() == "authorization" else "api_key"
    model_id = str(body.get("id") or "")
    return BackendView(
        id=member_id,
        title=str(properties.get("displayName") or "") or None,
        resource_id=model_id,
        provider=provider_of(model_id),
        kind=kind,  # type: ignore[arg-type]
        url=url,
        model=str(deployment.get("modelName") or name_of(model_id)),
        deployment_id=deployment_id,
        auth_type=auth_type,
        header_name=header_name,
        pools=sorted(pools, key=str.casefold),
        managed=is_managed(properties) and is_managed(provider),
        protected=is_protected(member_id, protected_prefixes),
        etag=etag or (str(body["etag"]) if body.get("etag") else None),
    )


def _plain(name: str) -> str:
    # Member IDs take letters, digits and hyphens; model names also take dots (gpt-6.1-sol).
    return re.sub(r"[^A-Za-z0-9-]", "-", name)


def member_ids(models: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Each model registration's member ID, by its ARM ID (case-folded): its own name (dots
    as hyphens), or ``<provider>--<name>`` when another model would get the same ID."""
    models = list(models)
    counts: dict[str, int] = {}
    for model in models:
        name = _plain(str(model.get("name") or "")).casefold()
        counts[name] = counts.get(name, 0) + 1
    ids: dict[str, str] = {}
    for model in models:
        model_id = str(model.get("id") or "")
        name = _plain(str(model.get("name") or ""))
        member = name if counts[name.casefold()] == 1 else f"{provider_of(model_id)}--{name}"
        if len(member) > 80:
            digest = hashlib.sha256(model_id.casefold().encode()).hexdigest()[:8]
            member = f"{member[:71].rstrip('-')}-{digest}"
        ids[model_id.casefold()] = member
    return ids
