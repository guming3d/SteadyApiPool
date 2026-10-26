"""LiteLLM model rows: building them for a pool's members and reading pools back from them.

A pool is the rows that share its ``model_name``. Each row this app writes carries what the app
needs in ``model_info.pool_manager``: the managed marker, the pool's title, description and
aliases, and the member it routes to. Rows without it (added in LiteLLM's own UI or config) are
read but never written.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote, unquote, urlsplit

from pydantic import SecretStr

from .models import (
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    BackendView,
    CircuitBreaker,
    MemberAuthType,
    PoolMemberView,
    PoolView,
)

MANAGED_MARKER = "Managed by AI Gateway Pool Manager."
METADATA_KEY = "pool_manager"
_AZURE_DEPLOYMENT = re.compile(r"^(?P<base>https?://[^/]+)/openai/deployments/(?P<name>[^/]+)$")

Provider = Literal["azure", "openai"]


def is_protected(resource_id: str, protected_prefixes: list[str]) -> bool:
    return any(resource_id.casefold().startswith(prefix) for prefix in protected_prefixes)


def credential_name(member_id: str) -> str:
    """The LiteLLM credential holding an api_key member's key, shared by all its rows."""
    return f"{member_id}-credential"


def row_id(pool_id: str, member_id: str) -> str:
    """Deterministic, so a retried save finds the row it already created."""
    candidate = f"{pool_id}--{member_id}"
    if len(candidate) <= 160:
        return candidate
    digest = hashlib.sha256(candidate.casefold().encode()).hexdigest()[:12]
    return f"{candidate[:147]}-{digest}"


@dataclass
class Member:
    """A member as rows describe it, or as added before any pool uses it."""

    id: str
    title: str | None
    url: str
    provider: Provider
    # Azure: the deployment name; OpenAI-compatible: the model the endpoint serves.
    model: str
    auth: MemberAuthType | Literal["custom"]
    managed: bool = True
    # A key given for an api_key member and not yet stored in its credential.
    secret: SecretStr | None = None
    pools: list[str] = field(default_factory=list)

    def view(self, protected_prefixes: list[str]) -> BackendView:
        return BackendView(
            id=self.id,
            title=self.title,
            url=self.url,
            provider=self.provider,
            model=self.model,
            auth_type=self.auth,
            pools=sorted(self.pools, key=str.casefold),
            managed=self.managed,
            protected=is_protected(self.id, protected_prefixes),
        )


def member_target(url: str, model: str | None) -> tuple[Provider, str, str]:
    """``(provider, api_base, model)`` for a member URL: an Azure OpenAI deployment URL, or an
    OpenAI-compatible base URL with the model it serves."""
    url = url.rstrip("/")
    match = _AZURE_DEPLOYMENT.fullmatch(url)
    if match:
        return "azure", match.group("base"), unquote(match.group("name"))
    if not model:
        raise ValueError(
            "an OpenAI-compatible member needs the model to ask it for; Azure OpenAI members "
            "use a .../openai/deployments/<name> URL"
        )
    return "openai", url, model


def member_metadata(member: Member, position: int) -> dict[str, Any]:
    return {
        "id": member.id,
        "title": member.title,
        "url": member.url,
        "provider": member.provider,
        "model": member.model,
        "auth": member.auth,
        "position": position,
    }


def member_params(member: Member) -> dict[str, Any]:
    """The part of ``litellm_params`` that says where the member is and how to call it."""
    _, api_base, model = member_target(member.url, member.model)
    params: dict[str, Any] = {"model": f"{member.provider}/{model}", "api_base": api_base}
    if member.auth == "api_key":
        params["litellm_credential_name"] = credential_name(member.id)
    elif member.provider == "openai":
        # The OpenAI client insists on a key even when the endpoint takes none.
        params["api_key"] = "none"
    # managed_identity: no key at all, so LiteLLM's Azure AD token refresh (the gateway's own
    # identity, AZURE_CLIENT_ID) authenticates the call.
    return params


@dataclass
class PoolSettings:
    title: str | None
    description: str | None
    aliases: list[str]
    breaker: CircuitBreaker
    retries: int
    timeout_seconds: int


def pool_row(
    pool_id: str,
    member: Member,
    *,
    priority: int,
    weight: int,
    position: int,
    settings: PoolSettings,
) -> dict[str, Any]:
    return {
        "model_name": pool_id,
        "litellm_params": {
            **member_params(member),
            # LiteLLM's order is 1-based: it sends to the lowest order whose members are up.
            "order": priority + 1,
            "weight": weight,
            "num_retries": settings.retries,
            "timeout": settings.timeout_seconds,
        },
        "model_info": {
            "id": row_id(pool_id, member.id),
            "allowed_fails": settings.breaker.allowed_fails,
            "cooldown_time": settings.breaker.cooldown_seconds,
            METADATA_KEY: {
                "managed_by": MANAGED_MARKER,
                "pool": {
                    "title": settings.title,
                    "description": settings.description,
                    "aliases": list(settings.aliases),
                },
                "member": member_metadata(member, position),
            },
        },
    }


# -- reading rows ----------------------------------------------------------------------------


def row_name(row: Mapping[str, Any]) -> str:
    return str(row.get("model_name") or "")


def row_info(row: Mapping[str, Any]) -> Mapping[str, Any]:
    info = row.get("model_info")
    return info if isinstance(info, Mapping) else {}


def row_params(row: Mapping[str, Any]) -> Mapping[str, Any]:
    params = row.get("litellm_params")
    return params if isinstance(params, Mapping) else {}


def row_metadata(row: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = row_info(row).get(METADATA_KEY)
    if isinstance(metadata, Mapping) and metadata.get("managed_by") == MANAGED_MARKER:
        return metadata
    return {}


def row_model_id(row: Mapping[str, Any]) -> str:
    return str(row_info(row).get("id") or "")


def is_managed(row: Mapping[str, Any]) -> bool:
    return bool(row_metadata(row))


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def row_member(row: Mapping[str, Any]) -> Member:
    """The member a row routes to: from this app's metadata, or read from its params."""
    metadata = row_metadata(row)
    described = metadata.get("member") if metadata else None
    if isinstance(described, Mapping) and described.get("id") and described.get("url"):
        provider: Provider = "openai" if described.get("provider") == "openai" else "azure"
        auth = described.get("auth")
        return Member(
            id=str(described["id"]),
            title=str(described["title"]) if described.get("title") else None,
            url=str(described["url"]),
            provider=provider,
            model=str(described.get("model") or ""),
            auth=auth if auth in {"none", "api_key", "managed_identity"} else "custom",
        )
    params = row_params(row)
    provider_name, _, model = str(params.get("model") or "").partition("/")
    api_base = str(params.get("api_base") or "").rstrip("/")
    azure = provider_name == "azure"
    return Member(
        id=row_model_id(row),
        title=None,
        url=f"{api_base}/openai/deployments/{quote(model, safe='')}" if azure else api_base,
        provider="azure" if azure else "openai",
        model=model,
        # Secrets are not returned, so how a foreign row authenticates is unknown.
        auth="custom",
        managed=False,
    )


def _position(row: Mapping[str, Any]) -> int:
    member = row_metadata(row).get("member")
    position = _int(member.get("position")) if isinstance(member, Mapping) else None
    return position if position is not None else 1_000


def _versioned_info(row: Mapping[str, Any]) -> dict[str, Any]:
    """``model_info`` without what doesn't make a pool save conflict: LiteLLM's timestamps,
    and the aliases, which a save re-reads from the gateway instead of taking from the editor."""
    info = {k: v for k, v in row_info(row).items() if k not in {"updated_at", "updated_by"}}
    metadata = info.get(METADATA_KEY)
    if isinstance(metadata, Mapping) and isinstance(metadata.get("pool"), Mapping):
        pool = {k: v for k, v in metadata["pool"].items() if k != "aliases"}
        info[METADATA_KEY] = {**metadata, "pool": pool}
    return info


def pool_etag(rows: Sequence[Mapping[str, Any]]) -> str:
    """A version of the pool for conditional saves: changes whenever a member row does."""
    canonical = sorted(
        (
            row_model_id(row),
            json.dumps(row_params(row), sort_keys=True, default=str),
            json.dumps(_versioned_info(row), sort_keys=True, default=str),
        )
        for row in rows
    )
    digest = hashlib.sha256(json.dumps(canonical).encode()).hexdigest()[:16]
    return f'"{digest}"'


def parse_pool(
    pool_id: str, rows: Sequence[Mapping[str, Any]], protected_prefixes: list[str]
) -> PoolView:
    ordered = sorted(rows, key=_position)
    metadata = next((row_metadata(row) for row in ordered if row_metadata(row)), {})
    pool = metadata.get("pool") if isinstance(metadata.get("pool"), Mapping) else {}
    assert isinstance(pool, Mapping)
    first_info = row_info(ordered[0]) if ordered else {}
    first_params = row_params(ordered[0]) if ordered else {}
    allowed = _int(first_info.get("allowed_fails"))
    cooldown = _int(first_info.get("cooldown_time"))
    breaker = (
        CircuitBreaker(allowed_fails=allowed, cooldown_seconds=cooldown)
        if allowed is not None and cooldown is not None and 0 <= allowed <= 1000 and cooldown > 0
        else None
    )
    timeout = first_params.get("timeout")
    aliases = pool.get("aliases")
    return PoolView(
        id=pool_id,
        title=str(pool["title"]) if pool.get("title") else None,
        description=str(pool["description"]) if pool.get("description") else None,
        members=[
            PoolMemberView(
                backend_id=row_member(row).id,
                priority=max((_int(row_params(row).get("order")) or 1) - 1, 0),
                weight=w if (w := _int(row_params(row).get("weight"))) is not None else 1,
            )
            for row in ordered
        ],
        circuit_breaker=breaker,
        retries=_int(first_params.get("num_retries")),
        timeout_seconds=round(timeout) if isinstance(timeout, int | float) else None,
        aliases=[str(item) for item in aliases] if isinstance(aliases, list) else [],
        managed=bool(rows) and all(is_managed(row) for row in rows),
        protected=is_protected(pool_id, protected_prefixes),
        etag=pool_etag(rows),
    )


def pool_settings(view: PoolView | None, **overrides: Any) -> PoolSettings:
    """A pool's settings for writing: the overrides, else what it has, else the defaults."""

    def pick(name: str, current: Any, default: Any) -> Any:
        value = overrides.get(name)
        if value is not None:
            return value
        return current if current is not None else default

    return PoolSettings(
        title=overrides.get("title", view.title if view else None),
        description=overrides.get("description", view.description if view else None),
        aliases=list(overrides.get("aliases", view.aliases if view else [])),
        breaker=pick("breaker", view.circuit_breaker if view else None, CircuitBreaker()),
        retries=pick("retries", view.retries if view else None, DEFAULT_RETRIES),
        timeout_seconds=pick(
            "timeout_seconds", view.timeout_seconds if view else None, DEFAULT_TIMEOUT_SECONDS
        ),
    )


def azure_deployment_url(api_base: str, deployment: str) -> str:
    return f"{api_base.rstrip('/')}/openai/deployments/{quote(deployment, safe='')}"


def url_host(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold()


# -- config.yaml, for people learning LiteLLM ------------------------------------------------

_PLAIN = re.compile(r"^[A-Za-z0-9_./@:-]+$")
_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~", ""}


def _scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    text = str(value)
    if _PLAIN.fullmatch(text) and text.casefold() not in _RESERVED and not text[0].isdigit():
        return text
    return json.dumps(text, ensure_ascii=False)


def to_yaml(value: object, indent: int = 0) -> str:
    """YAML for JSON-like data (dicts, lists, scalars), enough to show a config.yaml."""
    pad = "  " * indent
    if isinstance(value, Mapping):
        if not value:
            return f"{pad}{{}}\n"
        lines = []
        for key, item in value.items():
            if isinstance(item, Mapping) and item or isinstance(item, list) and item:
                lines.append(f"{pad}{_scalar(key)}:\n{to_yaml(item, indent + 1)}")
            else:
                inline = (
                    "{}"
                    if isinstance(item, Mapping)
                    else "[]"
                    if isinstance(item, list)
                    else _scalar(item)
                )
                lines.append(f"{pad}{_scalar(key)}: {inline}\n")
        return "".join(lines)
    if isinstance(value, list):
        if not value:
            return f"{pad}[]\n"
        lines = []
        for item in value:
            if isinstance(item, Mapping) and item:
                nested = to_yaml(item, indent + 1)
                lines.append(f"{pad}- {nested.lstrip()}")
            elif isinstance(item, list) and item:
                lines.append(f"{pad}-\n{to_yaml(item, indent + 1)}")
            else:
                lines.append(f"{pad}- {_scalar(item)}\n")
        return "".join(lines)
    return f"{pad}{_scalar(value)}\n"


def config_yaml(pool_id: str, rows: Sequence[Mapping[str, Any]], aliases: Sequence[str]) -> str:
    """The pool's part of a LiteLLM config.yaml: its rows, then its aliases."""
    model_list = []
    for row in sorted(rows, key=_position):
        params = {k: v for k, v in row_params(row).items() if v is not None and k != "api_key"}
        if "api_key" in row_params(row):
            params["api_key"] = "<hidden>"
        info = row_info(row)
        model_list.append(
            {
                "model_name": pool_id,
                "litellm_params": params,
                "model_info": {
                    key: info[key]
                    for key in ("id", "allowed_fails", "cooldown_time")
                    if info.get(key) is not None
                },
            }
        )
    document: dict[str, Any] = {"model_list": model_list}
    if aliases:
        document["router_settings"] = {"model_group_alias": {alias: pool_id for alias in aliases}}
    return to_yaml(document)
