"""Resizing a gateway this app deployed for new traffic, with the signed-in user's own rights.

``plan`` reads the gateway as it is and lists what the sizing plan for the new scenario would
change on the platform it runs on, one item each, and changes nothing:

- applied: on Container Apps the replica range, the scale rule and the replica size (a new
  revision rolls out); on AKS the node pool's range and LiteLLM's pods (``helm upgrade
  --reuse-values`` through Run Command, so pods roll);
- optional, only when asked: the database's size (the server restarts for a few minutes);
- left to the user, with the command where there is one: a different node size, the outbound
  ports and the tier on AKS, Redis for a gateway deployed without it (replicas then don't share
  cooldowns), and timeouts, which are each pool's setting.

``apply`` recomputes the plan, refuses a gateway that changed since the plan was read
(``etag``), writes, and records the new scenario in the gateway's tags (``ai-gateway-sizing``,
and on AKS ``ai-gateway-install``, so a later install keeps the new size). Only gateways tagged
as this app's are resized; nothing else about them changes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from ..arm import ArmClient, ArmError, segment
from ..config import Settings
from ..errors import ServiceError, arm_failure
from .aks_deploy import AKS_SERVER_SCOPE, CONTAINER_SERVICE_API_VERSION
from .cleanup import tagged_resources
from .cost import sizes_from_tags
from .gateway import GATEWAY_TAG, MANAGED_TAG, MANAGED_TAG_VALUE
from .gateway_template import (
    AKS_NAMESPACE,
    LITELLM_HELM_CHART,
    LITELLM_VERSION,
    SIZING_TAG,
    InstallSize,
    parse_sizing_tag,
    sizing_tag,
)
from .models import (
    GatewayPlan,
    GatewayPlatform,
    GatewaySizing,
    ResizeAction,
    ResizeApply,
    ResizeChange,
    ResizeCommand,
    ResizeItem,
    ResizePlan,
    ResizeResult,
)
from .sizing import (
    AUTOSCALE_CPU_PERCENT,
    SIDECAR,
    node_family,
    node_in_family,
    node_pods,
    plan,
    postgres_size,
)

logger = logging.getLogger(__name__)

POSTGRES_API_VERSION = "2024-08-01"
INSTALL_TAG = "ai-gateway-install"
PROFILE_TAG = "ai-gateway-profile"
POSTGRES_TAG = "ai-gateway-postgres"
_POSTGRES_TYPE = "microsoft.dbforpostgresql/flexibleservers"
_LOG_LINES = 30


@dataclass
class Current:
    """The gateway as it is: what a resize compares with and changes."""

    platform: GatewayPlatform
    resource_id: str
    name: str
    subscription_id: str
    resource_group: str
    body: dict[str, Any]
    tags: dict[str, str]
    min_replicas: int
    max_replicas: int
    postgres_sku: str
    redis_sku: str | None
    scale_concurrency: int | None = None
    scale_cpu: int | None = None
    cpu: float | None = None
    memory: float | None = None
    pool: dict[str, Any] = field(default_factory=dict)
    node_min: int = 0
    node_max: int = 0
    outbound_ports: int = 0
    tier: str = "Free"

    @property
    def sizing(self) -> GatewaySizing | None:
        return parse_sizing_tag(self.tags.get(SIZING_TAG))

    @property
    def node_size(self) -> str:
        return str(self.pool.get("vmSize") or "")


def _memory(value: object) -> float | None:
    match = re.match(r"^\s*([\d.]+)\s*Gi\s*$", str(value or ""))
    return float(match.group(1)) if match else None


def _litellm_container(template: Mapping[str, Any]) -> dict[str, Any]:
    containers = template.get("containers") or [{}]
    found = next((c for c in containers if c.get("name") == "litellm"), containers[0])
    return dict(found)


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except ValueError:
        return None


def _http_rule(scale: Mapping[str, Any]) -> int | None:
    for rule in scale.get("rules") or []:
        value = ((rule.get("http") or {}).get("metadata") or {}).get("concurrentRequests")
        if value is not None:
            return _int(value)
    return None


def _is_cpu_rule(rule: Mapping[str, Any]) -> bool:
    return str((rule.get("custom") or {}).get("type") or "").casefold() == "cpu"


def _cpu_rule(scale: Mapping[str, Any]) -> int | None:
    for rule in scale.get("rules") or []:
        if _is_cpu_rule(rule):
            return _int(((rule.get("custom") or {}).get("metadata") or {}).get("value"))
    return None


def _scale_rules(concurrency: int | None, cpu: int | None) -> str:
    return f"{concurrency or '-'} / {f'{cpu} %' if cpu else '-'}"


def pods_per_node(node_size: str) -> int:
    """LiteLLM pods a node of this size takes (sizing.NODE_SIZES, in any of its node families),
    or a guess from its vCPUs."""
    pods = node_pods(node_size)
    if pods is not None:
        return pods
    vcpus = re.search(r"_[A-Z]+(\d+)", node_size)
    return max(1, int(vcpus.group(1)) // 2) if vcpus else 2


def _range(low: int, high: int) -> str:
    return str(low) if low == high else f"{low}-{high}"


def _etag(current: Current) -> str:
    """A hash of what a resize reads and changes."""
    state = {
        "replicas": [current.min_replicas, current.max_replicas],
        "scale": [current.scale_concurrency, current.scale_cpu],
        "size": [current.cpu, current.memory],
        "postgres": current.postgres_sku,
        "pool": [current.node_min, current.node_max, current.node_size],
        "tags": current.tags.get(SIZING_TAG),
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:32]


class GatewayResizer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # -- reading ------------------------------------------------------------------------

    def current(self, arm: ArmClient) -> Current:
        settings = self._settings
        try:
            if settings.gateway_cluster_id:
                return self._cluster(arm, settings.gateway_cluster_id)
            if settings.external:
                raise ServiceError(
                    409,
                    "This app didn't deploy this gateway; resize it where it runs",
                )
            return self._app(arm)
        except ArmError as error:
            raise arm_failure(error, "read the gateway") from error

    def _require_managed(self, body: Mapping[str, Any], name: str) -> dict[str, str]:
        tags = {str(k): str(v) for k, v in (body.get("tags") or {}).items()}
        if tags.get(MANAGED_TAG) != MANAGED_TAG_VALUE:
            raise ServiceError(409, f"{name} wasn't deployed by this app, so it isn't resized here")
        return tags

    def _app(self, arm: ArmClient) -> Current:
        settings = self._settings
        path = (
            f"/subscriptions/{segment(settings.subscription_id)}"
            f"/resourceGroups/{segment(settings.resource_group)}"
            f"/providers/Microsoft.App/containerApps/{segment(settings.gateway_name)}"
        )
        body = arm.get_arm_json(path, api_version=settings.container_apps_api_version)
        if body is None:
            raise ServiceError(404, f"The gateway's Container App {settings.gateway_name} is gone")
        tags = self._require_managed(body, settings.gateway_name)
        template = (body.get("properties") or {}).get("template") or {}
        scale = template.get("scale") or {}
        container = _litellm_container(template)
        resources = container.get("resources") or {}
        postgres, redis = sizes_from_tags(tags)
        low = int(scale.get("minReplicas") or 1)
        return Current(
            platform="aca",
            resource_id=str(body.get("id") or path),
            name=settings.gateway_name,
            subscription_id=settings.subscription_id,
            resource_group=settings.resource_group,
            body=body,
            tags=tags,
            min_replicas=low,
            max_replicas=int(scale.get("maxReplicas") or low),
            scale_concurrency=_http_rule(scale),
            scale_cpu=_cpu_rule(scale),
            cpu=float(resources["cpu"]) if resources.get("cpu") is not None else None,
            memory=_memory(resources.get("memory")),
            postgres_sku=postgres,
            redis_sku=redis,
        )

    def _cluster(self, arm: ArmClient, cluster_id: str) -> Current:
        body = arm.get_arm_json(cluster_id, api_version=CONTAINER_SERVICE_API_VERSION)
        name = cluster_id.rstrip("/").rsplit("/", 1)[-1]
        if body is None:
            raise ServiceError(404, f"The gateway's cluster {name} is gone")
        tags = self._require_managed(body, name)
        properties = body.get("properties") or {}
        pool = dict((properties.get("agentPoolProfiles") or [{}])[0])
        count = int(pool.get("count") or 1)
        size = InstallSize.parse(tags.get(INSTALL_TAG), tags.get(PROFILE_TAG))
        balancer = (properties.get("networkProfile") or {}).get("loadBalancerProfile") or {}
        postgres, redis = sizes_from_tags(tags)
        parts = cluster_id.split("/")
        return Current(
            platform="aks",
            resource_id=str(body.get("id") or cluster_id),
            name=name,
            subscription_id=parts[2],
            resource_group=parts[4],
            body=body,
            tags=tags,
            min_replicas=size.min_replicas,
            max_replicas=size.max_replicas,
            postgres_sku=postgres,
            redis_sku=redis,
            pool=pool,
            node_min=int(pool.get("minCount") or count) if pool.get("enableAutoScaling") else count,
            node_max=int(pool.get("maxCount") or count) if pool.get("enableAutoScaling") else count,
            outbound_ports=int(balancer.get("allocatedOutboundPorts") or 0),
            tier=str((body.get("sku") or {}).get("tier") or "Free"),
        )

    # -- the plan ----------------------------------------------------------------------

    def _nodes(
        self, current: Current, found: GatewayPlan, sizing: GatewaySizing
    ) -> tuple[int, int]:
        """The node range for the plan's pods at the cluster's own node size."""
        per_node = pods_per_node(current.node_size)
        least = 3 if sizing.zone_redundant else 1
        high = max(least, math.ceil(found.max_replicas / per_node))
        low = min(high, max(least, math.ceil(found.min_replicas / per_node)))
        return low, high

    def plan(self, arm: ArmClient, sizing: GatewaySizing) -> tuple[ResizePlan, Current]:
        current = self.current(arm)
        found = plan(sizing, current.platform)
        changes: list[ResizeChange] = []

        def change(
            item: ResizeItem,
            before: str,
            after: str,
            action: ResizeAction,
            command: str | None = None,
        ) -> None:
            if before != after:
                changes.append(
                    ResizeChange(
                        item=item, current=before, proposed=after, action=action, command=command
                    )
                )

        replicas = _range(found.min_replicas, found.max_replicas)
        if current.platform == "aca":
            change(
                "replicas", _range(current.min_replicas, current.max_replicas), replicas, "apply"
            )
            change(
                "scale_rule",
                _scale_rules(current.scale_concurrency, current.scale_cpu),
                _scale_rules(found.scale_concurrency, found.scale_cpu_percent),
                "apply",
            )
            change(
                "replica_size",
                f"{current.cpu:g} vCPU, {current.memory:g} GiB"
                if current.cpu and current.memory
                else "-",
                f"{found.replica_cpu:g} vCPU, {found.replica_memory_gib:g} GiB",
                "apply",
            )
        else:
            assert found.aks is not None
            low, high = self._nodes(current, found, sizing)
            change("nodes", _range(current.node_min, current.node_max), _range(low, high), "apply")
            change("pods", _range(current.min_replicas, current.max_replicas), replicas, "apply")
            group, cluster = current.resource_group, current.name
            # The plan's size in the family the cluster already runs (the region check may have
            # deployed it on Ddsv7 where Ddsv5 wasn't open).
            family = node_family(current.node_size)
            wanted = (
                node_in_family(found.aks.node_size, family)
                if family is not None
                else found.aks.node_size
            )
            if wanted.casefold() != current.node_size.casefold():
                change(
                    "node_size",
                    current.node_size,
                    wanted,
                    "manual",
                    f"az aks nodepool add -g {group} --cluster-name {cluster} -n litellm2 "
                    f"--node-vm-size {wanted} --enable-cluster-autoscaler "
                    f"--min-count {found.aks.node_min} --max-count {found.aks.node_max}",
                )
            if current.outbound_ports < found.aks.ports_needed:
                change(
                    "outbound",
                    str(current.outbound_ports or 1024),
                    str(found.aks.outbound_ports),
                    "manual",
                    f"az aks update -g {group} -n {cluster} "
                    f"--load-balancer-managed-outbound-ip-count {found.aks.outbound_ips} "
                    f"--load-balancer-outbound-ports {found.aks.outbound_ports}",
                )
            if found.aks.tier == "Standard" and current.tier.casefold() != "standard":
                change(
                    "tier",
                    current.tier,
                    "Standard",
                    "manual",
                    f"az aks update -g {group} -n {cluster} --tier standard",
                )
        change("database", current.postgres_sku, found.postgres_sku, "optional")
        if found.redis_sku and not current.redis_sku:
            change("redis", "-", found.redis_sku, "manual")
        before = current.sizing
        if before is not None and before.timeout_seconds != sizing.timeout_seconds:
            change("timeout", str(before.timeout_seconds), str(sizing.timeout_seconds), "manual")
        problems = [str(note.code) for note in found.problems]
        result = ResizePlan(
            platform=current.platform,
            plan=found,
            changes=changes,
            etag=_etag(current),
            sized_for=before,
            problems=problems,
        )
        return result, current

    # -- applying ----------------------------------------------------------------------

    def apply(self, arm: ArmClient, body: ResizeApply, actor: str) -> ResizeResult:
        planned, current = self.plan(arm, body.sizing)
        if planned.etag != body.etag:
            raise ServiceError(409, "The gateway changed since this plan was made; review it again")
        if planned.problems:
            raise ServiceError(
                409, "This traffic doesn't fit the gateway's platform; nothing was changed"
            )
        items = {change.item for change in planned.changes}
        applied: list[ResizeItem] = []
        command: str | None = None
        found = planned.plan
        try:
            if body.database and "database" in items:
                self._database(arm, current, found)
                applied.append("database")
            tags = {
                **current.tags,
                SIZING_TAG: sizing_tag(body.sizing),
                PROFILE_TAG: found.profile,
                POSTGRES_TAG: found.postgres_sku if "database" in applied else current.postgres_sku,
            }
            if current.platform == "aca":
                aca_items: tuple[ResizeItem, ...] = ("replicas", "scale_rule", "replica_size")
                applied += [item for item in aca_items if item in items]
                self._app_update(arm, current, found, tags)
            else:
                if "nodes" in items:
                    self._node_pool(arm, current, found, body.sizing)
                    applied.append("nodes")
                # The pods' range and the rest from the plan; spend through Redis as installed,
                # since a resize leaves LiteLLM's config as it is.
                installed = InstallSize.parse(current.tags.get(INSTALL_TAG))
                tags[INSTALL_TAG] = replace(InstallSize.of(found), buffer=installed.buffer).tag()
                arm.request(
                    "PATCH",
                    arm.arm_url(current.resource_id),
                    api_version=CONTAINER_SERVICE_API_VERSION,
                    json_body={"tags": tags},
                )
                if "pods" in items:
                    command = self._pods(arm, current, found)
                    applied.append("pods")
        except ArmError as error:
            raise arm_failure(error, "resize the gateway") from error
        logger.info(
            "%s resized gateway %s: %s (%s)",
            actor,
            current.name,
            ", ".join(applied) or "nothing to change",
            sizing_tag(body.sizing),
        )
        return ResizeResult(applied=applied, command_id=command)

    def _app_update(
        self, arm: ArmClient, current: Current, found: GatewayPlan, tags: dict[str, str]
    ) -> None:
        template = copy.deepcopy((current.body.get("properties") or {}).get("template") or {})
        scale = template.setdefault("scale", {})
        scale["minReplicas"] = found.min_replicas
        scale["maxReplicas"] = found.max_replicas
        rules = [
            rule
            for rule in scale.get("rules") or []
            if "http" not in rule and not _is_cpu_rule(rule)
        ]
        cpu = {"type": "Utilization", "value": str(found.scale_cpu_percent)}
        scale["rules"] = [
            {
                "name": "http",
                "http": {"metadata": {"concurrentRequests": str(found.scale_concurrency)}},
            },
            {"name": "cpu", "custom": {"type": "cpu", "metadata": cpu}},
            *rules,
        ]
        for container in template.get("containers") or []:
            if container.get("name") == "litellm":
                container["resources"] = {
                    "cpu": found.replica_cpu,
                    "memory": f"{found.replica_memory_gib:g}Gi",
                }
            elif container.get("name") == "otel-collector":
                container["resources"] = {"cpu": SIDECAR[0], "memory": f"{SIDECAR[1]:g}Gi"}
        arm.request(
            "PATCH",
            arm.arm_url(current.resource_id),
            api_version=self._settings.container_apps_api_version,
            json_body={"properties": {"template": template}, "tags": tags},
        )

    def _node_pool(
        self, arm: ArmClient, current: Current, found: GatewayPlan, sizing: GatewaySizing
    ) -> None:
        low, high = self._nodes(current, found, sizing)
        name = str(current.pool.get("name") or "system")
        path = f"{current.resource_id}/agentPools/{segment(name)}"
        pool = arm.get_arm_json(path, api_version=CONTAINER_SERVICE_API_VERSION) or {}
        properties = dict(pool.get("properties") or {})
        count = int(properties.get("count") or low)
        autoscale = high > low
        properties |= {
            "count": min(max(count, low), high),
            "enableAutoScaling": autoscale,
            "minCount": low if autoscale else None,
            "maxCount": high if autoscale else None,
        }
        arm.request(
            "PUT",
            arm.arm_url(path),
            api_version=CONTAINER_SERVICE_API_VERSION,
            json_body={"properties": properties},
        )

    def _pods(self, arm: ArmClient, current: Current, found: GatewayPlan) -> str:
        """``helm upgrade --reuse-values`` with the new pod range, through Run Command."""
        autoscale = found.max_replicas > found.min_replicas
        values = {
            "replicaCount": found.min_replicas,
            "autoscaling.enabled": str(autoscale).lower(),
            "autoscaling.minReplicas": found.min_replicas,
            "autoscaling.maxReplicas": found.max_replicas,
            "autoscaling.targetCPUUtilizationPercentage": AUTOSCALE_CPU_PERCENT,
            "pdb.enabled": str(found.min_replicas > 1).lower(),
            "pdb.minAvailable": 1,
        }
        sets = " ".join(f"--set {key}={value}" for key, value in values.items())
        script = (
            f"helm upgrade litellm {LITELLM_HELM_CHART} --version {LITELLM_VERSION} "
            f"--namespace {AKS_NAMESPACE} --reuse-values {sets} --wait --timeout 10m"
        )
        response = arm.request(
            "POST",
            arm.arm_url(f"{current.resource_id}/runCommand"),
            api_version=CONTAINER_SERVICE_API_VERSION,
            json_body={"command": script, "clusterToken": arm.token_for(AKS_SERVER_SCOPE)},
        )
        location = response.headers.get("Location") or ""
        command = urlsplit(location).path.rstrip("/").rsplit("/", 1)[-1]
        if not command:
            command = str((response.json() if response.content else {}).get("id") or "")
        if not command:
            raise ArmError(502, "RunCommand", "AKS didn't say where to follow the change")
        return command

    def command(self, arm: ArmClient, command: str) -> ResizeCommand:
        """The pods' change on AKS, while it runs and once it has."""
        cluster = self._settings.gateway_cluster_id
        if not cluster:
            raise ServiceError(404, "Only a gateway on AKS changes its pods with a command")
        try:
            response = arm.request(
                "GET",
                arm.arm_url(f"{cluster}/commandResults/{segment(command)}"),
                api_version=CONTAINER_SERVICE_API_VERSION,
            )
        except ArmError as error:
            raise arm_failure(error, "follow the change") from error
        if response.status_code == 202 or not response.content:
            return ResizeCommand(state="running", exit_code=None, log_tail=None)
        properties = response.json().get("properties") or {}
        state = str(properties.get("provisioningState") or "").casefold()
        if state not in {"succeeded", "failed", "canceled"}:
            return ResizeCommand(state="running", exit_code=None, log_tail=None)
        code = properties.get("exitCode")
        logs = str(properties.get("logs") or properties.get("reason") or "")
        tail = "\n".join(logs.strip().splitlines()[-_LOG_LINES:]) or None
        ok = state == "succeeded" and code == 0
        return ResizeCommand(
            state="succeeded" if ok else "failed",
            exit_code=code if isinstance(code, int) else None,
            log_tail=tail,
        )

    def _database(self, arm: ArmClient, current: Current, found: GatewayPlan) -> None:
        servers = [
            item
            for item in tagged_resources(
                arm, current.subscription_id, current.resource_group, current.name
            )
            if str(item.get("type") or "").casefold() == _POSTGRES_TYPE
        ]
        if not servers:
            raise ServiceError(404, f"The database of {current.name} wasn't found")
        size = postgres_size(found.postgres_sku)
        arm.request(
            "PATCH",
            arm.arm_url(str(servers[0]["id"])),
            api_version=POSTGRES_API_VERSION,
            json_body={"sku": {"name": size.sku, "tier": size.edition}},
        )


__all__ = ["GATEWAY_TAG", "GatewayResizer"]
