"""Where the LiteLLM gateway is and how to reach its admin API.

A gateway on Container Apps is read through ARM: its URL (the ingress FQDN), the user-assigned
identity it calls Foundry as, its Application Insights component (a tag) and the master key,
which is a Container App secret read with ``listSecrets`` and kept in this process only. An
external gateway (AKS, or any LiteLLM proxy) comes from the settings, with the master key in the
environment or the local secret store.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal

import httpx

from ..arm import ArmClient, ArmError
from ..config import Settings
from .client import LiteLLMClient, LiteLLMError

logger = logging.getLogger(__name__)

# The Container App secret holding the master key, as the deploy template names it.
MASTER_KEY_SECRET = "litellm-master-key"
# Tags on resources this app deploys for a gateway.
MANAGED_TAG = "managed-by"
MANAGED_TAG_VALUE = "ai-gateway-pool"
GATEWAY_TAG = "ai-gateway"
APP_INSIGHTS_TAG = "ai-gateway-app-insights"
_INFO_SECONDS = 60.0


class GatewayUnavailable(Exception):
    """The gateway can't be reached or managed yet; ``status`` is what the API answers."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class GatewayInfo:
    name: str
    kind: str
    url: str | None
    principal_id: str | None
    app_insights_id: str | None
    resource_id: str | None
    location: str | None
    provisioning_state: str | None
    # Deployed by this app (the managed-by tag).
    managed: bool
    tags: Mapping[str, str]
    # Size of each replica (all its containers) and how many run; None when unknown.
    cpu: float | None = None
    memory_gib: float | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None


def _gib(value: object) -> float:
    text = str(value or "0").strip()
    for suffix, factor in (("Gi", 1.0), ("G", 1.0), ("Mi", 1 / 1024)):
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * factor
            except ValueError:
                return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def container_app_info(app: Mapping[str, Any], name: str) -> GatewayInfo:
    properties = app.get("properties") or {}
    ingress = (properties.get("configuration") or {}).get("ingress") or {}
    fqdn = str(ingress.get("fqdn") or "")
    identity = app.get("identity") or {}
    assigned = identity.get("userAssignedIdentities") or {}
    principal = next(
        (
            str(item.get("principalId"))
            for item in assigned.values()
            if isinstance(item, Mapping) and item.get("principalId")
        ),
        None,
    ) or (str(identity.get("principalId")) if identity.get("principalId") else None)
    tags = {str(k): str(v) for k, v in (app.get("tags") or {}).items()}
    template = properties.get("template") or {}
    containers = [c for c in template.get("containers") or [] if isinstance(c, Mapping)]
    resources = [c.get("resources") or {} for c in containers]
    scale = template.get("scale") or {}
    min_replicas = scale.get("minReplicas")
    max_replicas = scale.get("maxReplicas")
    return GatewayInfo(
        name=name,
        kind="aca",
        url=f"https://{fqdn}" if fqdn else None,
        principal_id=principal,
        app_insights_id=tags.get(APP_INSIGHTS_TAG) or None,
        resource_id=str(app.get("id") or "") or None,
        location=str(app.get("location") or "") or None,
        provisioning_state=str(properties.get("provisioningState") or "") or None,
        managed=tags.get(MANAGED_TAG) == MANAGED_TAG_VALUE,
        tags=tags,
        cpu=sum(float(r.get("cpu") or 0) for r in resources) if resources else None,
        memory_gib=sum(_gib(r.get("memory")) for r in resources) if resources else None,
        min_replicas=min_replicas if isinstance(min_replicas, int) else None,
        max_replicas=max_replicas if isinstance(max_replicas, int) else None,
    )


def master_key_secret(secrets: Mapping[str, Any]) -> str:
    """The master key in a Container App's ``listSecrets`` answer; empty when it has none."""
    return next(
        (
            str(item.get("value"))
            for item in secrets.get("value") or []
            if isinstance(item, Mapping) and item.get("name") == MASTER_KEY_SECRET
        ),
        "",
    )


ProbeProblem = Literal["key", "unreachable", "database"]
# Long enough for a replica that is just starting, short enough that a gateway nobody can reach
# is reported within seconds rather than after the admin client's 30 s per call.
PROBE_TIMEOUT_SECONDS = 10.0


def probe_gateway(
    url: str, master_key: str, transport: httpx.BaseTransport | None = None
) -> tuple[ProbeProblem, str] | None:
    """Whether LiteLLM answers at ``url`` with its database connected and accepts the key;
    else what is wrong, and why."""
    client = LiteLLMClient(url, master_key, transport=transport, timeout=PROBE_TIMEOUT_SECONDS)
    try:
        health = client.readiness()
        client.models()  # Needs the master key; readiness does not.
    except LiteLLMError as error:
        if error.status in {401, 403}:
            return "key", "The gateway refused the master key"
        return "unreachable", f"Cannot reach a LiteLLM gateway at {url}: {error.message}"
    finally:
        client.close()
    database = str(health.get("db") or "")
    if database.casefold() not in {"connected", ""}:
        return "database", f"{url} has no database ({database}); pools need STORE_MODEL_IN_DB"
    return None


class GatewayConnection:
    def __init__(
        self,
        settings: Settings,
        arm: ArmClient,
        *,
        stored_key: Callable[[str], str | None] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._settings = settings
        self._arm = arm
        # Looks up an external gateway's master key by URL (secret_store.py).
        self._stored_key = stored_key
        self._transport = transport
        self._sleep = sleep
        self._lock = Lock()
        self._info: tuple[float, GatewayInfo] | None = None
        self._client: LiteLLMClient | None = None
        self._version: str | None = None

    @property
    def name(self) -> str:
        return self._settings.gateway_name

    def forget(self) -> None:
        """Reads the gateway again next time, e.g. after it was redeployed."""
        with self._lock:
            self._info = None
            if self._client is not None:
                self._client.close()
            self._client = None

    def info(self, *, refresh: bool = False) -> GatewayInfo:
        with self._lock:
            cached = self._info
            if refresh or cached is None or time.monotonic() - cached[0] > _INFO_SECONDS:
                cached = (time.monotonic(), self._read_info())
                self._info = cached
            return cached[1]

    def _read_info(self) -> GatewayInfo:
        settings = self._settings
        if settings.external:
            return GatewayInfo(
                name=settings.gateway_name,
                kind="external",
                url=settings.gateway_url.rstrip("/"),
                principal_id=settings.gateway_principal_id or None,
                app_insights_id=settings.gateway_app_insights_id or None,
                resource_id=None,
                location=None,
                provisioning_state="Succeeded",
                managed=False,
                tags={},
            )
        try:
            found = self._arm.get_json("", api_version=settings.container_apps_api_version)
        except ArmError as error:
            raise GatewayUnavailable(
                502 if error.status in {401, 403} else error.status,
                f"Cannot read the gateway's Container App {settings.gateway_name}: {error.message}",
            ) from error
        if found is None:
            raise GatewayUnavailable(
                404, f"The gateway's Container App {settings.gateway_name} was not found"
            )
        return container_app_info(found[0], settings.gateway_name)

    def _master_key(self, info: GatewayInfo) -> str:
        settings = self._settings
        if settings.gateway_master_key.get_secret_value():
            return settings.gateway_master_key.get_secret_value()
        if settings.external:
            key = self._stored_key(info.url or "") if self._stored_key else None
            if not key:
                raise GatewayUnavailable(
                    409, f"Enter the master key of {info.name} in setup to connect to it"
                )
            return key
        try:
            response = self._arm.request(
                "POST", "/listSecrets", api_version=settings.container_apps_api_version
            )
        except ArmError as error:
            raise GatewayUnavailable(
                403 if error.status in {401, 403} else 502,
                f"Cannot read the master key of {info.name} (it needs "
                f"Microsoft.App/containerApps/listSecrets/action): {error.message}",
            ) from error
        key = master_key_secret(response.json())
        if not key:
            raise GatewayUnavailable(
                409, f"{info.name} has no {MASTER_KEY_SECRET} secret; is it a LiteLLM gateway?"
            )
        return key

    def client(self) -> LiteLLMClient:
        """The admin client, built once with the master key."""
        info = self.info()
        if not info.url:
            raise GatewayUnavailable(409, f"{info.name} has no URL yet; it may still be deploying")
        with self._lock:
            if self._client is None:
                self._client = LiteLLMClient(
                    info.url, self._master_key(info), transport=self._transport, sleep=self._sleep
                )
            return self._client

    def version(self) -> str | None:
        """LiteLLM's version, from the readiness probe; ``None`` while unreachable."""
        if self._version is None:
            try:
                self._version = str(self.client().readiness().get("litellm_version") or "") or None
            except (GatewayUnavailable, LiteLLMError) as error:
                logger.warning("Cannot read the gateway's version: %s", error)
        return self._version
