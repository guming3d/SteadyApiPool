"""How big a LiteLLM gateway has to be for a traffic scenario: ``plan(sizing, platform)``.

Pure arithmetic, shown to the user step by step (the plan carries every intermediate number),
and the only source of the deployed sizes: the region check, the price and the templates' parameters
all come from the plan the backend computes, never from the browser.

1. Requests a second and tokens a minute from the peak RPM and the average tokens.
2. How long a call takes (a speed preset, or the scenario's own response time), so how many
   calls are in flight at the peak; and how many if Foundry slowed every call to the timeout.
3. Gateway CPU per call (a fixed cost, the bodies, each streamed token), so what one replica
   serves at the target CPU. One LiteLLM worker per replica uses at most one core.
4. How many calls one replica can hold (memory for the bodies in flight, an event loop cap).
5. Replicas at the peak are whichever of 3 and 4 needs more; the range adds the headroom and,
   for zone redundancy, one replica more (and at least two).
6. The database (connections: replicas x pool x 2 while a revision rolls over), Redis (only
   with more than one replica), AKS nodes, and the outbound ports for the slowed-down case.

The CPU figures (``CPU_MS_*``) were measured with tests/litellm/litellm_stack/calibrate.py on
LiteLLM as this app deploys it; the memory ones (``BODY_COPIES``) are estimates; the Azure limits
are documented ones. The load test after deploying (benchmark.py) is what confirms a plan.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .models import AksPlan, GatewayPlan, GatewayPlans, GatewayPlatform, GatewaySizing, PlanNote

# -- assumptions ---------------------------------------------------------------------------------

# Seconds to the first token and output tokens a second, per model speed preset.
SPEEDS = {"fast": (0.5, 120.0), "standard": (1.0, 60.0), "reasoning": (8.0, 40.0)}
# Reading the prompt: seconds per 1,000 input tokens before the first token.
PREFILL_SECONDS_PER_1K = 0.1
# Gateway CPU per call, measured on 2026-10-28 with calibrate.py: LiteLLM v1.106.4 configured as
# this app deploys it (a virtual key, spend batched, the OpenTelemetry callback), one worker, a
# fake upstream that answers at once, on Docker Desktop (Azure's CPUs may differ somewhat).
# - About 27 ms a call (23-30 ms; 23 ms without OpenTelemetry), whatever the size of the bodies
#   (100 to 20,000 prompt tokens, 10 to 2,000 output tokens unstreamed).
# - Streaming costs about 0.75 ms a chunk (0.65 ms of it LiteLLM's own streaming; 0.74 ms a token
#   at 500 tokens, 0.86 ms at 2,000), and Azure sends about one token a chunk.
# - With OpenTelemetry, each streamed chunk costs about 0.11 ms more per 1,000 prompt tokens
#   (a 5,000-token prompt made a 500-token stream 669 ms instead of 396; 359 ms without it).
# - One worker flat out: 26-46 calls a second at 1.05-1.08 cores.
CPU_MS_PER_REQUEST = 27.0
CPU_MS_PER_1K_TOKENS = 0.1
CPU_MS_PER_STREAMED_TOKEN = 0.75
CPU_MS_PER_STREAMED_TOKEN_PER_1K_INPUT = 0.11
# One worker per replica (LiteLLM's guidance on Kubernetes): one core at most, kept at 70 %.
WORKER_CORES = 1.0
TARGET_CPU = 0.7
# Memory a call in flight holds: its bodies (about 4 bytes a token) several times over (the
# request, its parsed form, the upstream request, the response, logging).
BYTES_PER_TOKEN = 4
BODY_COPIES = 8
BASE_MEMORY_GIB = 1.5
MEMORY_TARGET = 0.8
MAX_IN_FLIGHT_PER_REPLICA = 1000

# Container Apps: what a replica gets (LiteLLM, then the collector sidecar on top), its ingress
# limit, and the most replicas a plan puts there (the environment's default quota is 100 cores).
ACA_SMALL = (1.0, 2.0)
ACA_STANDARD = (2.0, 4.0)
SIDECAR = (0.25, 0.5)
# The ingress ends a request that sends nothing for 240 s (measured: a silent call got a 504 at
# 241 s, a call streaming every 10 s ran its full 300 s), so a streamed call only needs its first
# token within that.
ACA_INGRESS_SECONDS = 240
ACA_MAX_REPLICAS = 30
# AKS: a pod is 1 vCPU and 4 GiB (gateway_template.AKS_POD_RESOURCES); node sizes from the
# smallest, with the pods each takes after the system pods; up to 10 nodes before a larger size,
# at most 100 of the largest.
AKS_POD = (1.0, 4.0)
NODE_SIZES = (("Standard_D4ds_v5", 4, 2), ("Standard_D8ds_v5", 8, 5), ("Standard_D16ds_v5", 16, 12))
# The VM families a plan's node size can come from, in order, with their quota family: the
# region check (capacity.py) takes the first the subscription may use there. Same vCPUs and
# memory in each, so the same pods a node.
NODE_FAMILIES = (
    ("ds_v5", "standardDDSv5Family", "DDSv5 vCPUs"),
    ("ds_v7", "StandardDdsv7Family", "Ddsv7 vCPUs"),
    ("ds_v6", "StandardDdsv6Family", "Ddsv6 vCPUs"),
)
AKS_PREFERRED_NODES = 10
AKS_MAX_NODES = 100
SNAT_PORTS_PER_IP = 64_000
MAX_OUTBOUND_IPS = 16
# LiteLLM's database connections per replica, and the route on AKS: a call and its retry.
DB_POOL = 10
RETRIES = 1
MIN_ROUTE_SECONDS = 600


@dataclass(frozen=True)
class PostgresSize:
    """A size the templates use: its vCores, its edition in the capabilities API, the quota
    family its vCores count in (some regions don't list the family; then only ``cores``), and
    its user connections (max_connections less the 15 Azure keeps)."""

    sku: str
    vcores: int
    edition: str
    edition_label: str
    family: str
    connections: int


POSTGRES_SIZES = (
    PostgresSize("Standard_B1ms", 1, "Burstable", "Burstable", "standardBSFamily", 35),
    PostgresSize("Standard_B2s", 2, "Burstable", "Burstable", "standardBSFamily", 414),
    PostgresSize(
        "Standard_D2ds_v5", 2, "GeneralPurpose", "General Purpose", "standardDDSv5Family", 844
    ),
    PostgresSize(
        "Standard_D4ds_v5", 4, "GeneralPurpose", "General Purpose", "standardDDSv5Family", 1703
    ),
    PostgresSize(
        "Standard_D8ds_v5", 8, "GeneralPurpose", "General Purpose", "standardDDSv5Family", 3422
    ),
    PostgresSize(
        "Standard_D16ds_v5", 16, "GeneralPurpose", "General Purpose", "standardDDSv5Family", 4985
    ),
)
# The General Purpose sizes a plan picks from, smallest first; Burstable only for one replica
# without zone redundancy (a trial), since it runs out of CPU credits under sustained load.
_GENERAL = [size for size in POSTGRES_SIZES if size.edition == "GeneralPurpose"]
DATABASE_TARGET = 0.8
# Azure Managed Redis (Balanced) by the peak it carries: rough, it holds cooldowns and counters.
REDIS_SIZES = ((1000, "Balanced_B0"), (2500, "Balanced_B1"), (5000, "Balanced_B3"))
REDIS_LARGEST = "Balanced_B5"
# Past this, LiteLLM recommends routing spend writes through Redis (use_redis_transaction_buffer).
REDIS_BUFFER_RPS = 1000
REDIS_BUFFER_REPLICAS = 10


def describe(note: PlanNote) -> str:
    """A problem in words, for the backend's own refusals (the UI words every note itself)."""
    values = note.values
    texts = {
        "aca_timeout": (
            f"Container Apps' ingress ends a call that sends nothing for {ACA_INGRESS_SECONDS} s, "
            f"and these calls may take {values.get('timeout')} s without streaming (or before "
            "their first token): deploy on AKS, or stream the responses"
        ),
        "aca_replicas": (
            f"The peak needs {values.get('replicas')} replicas, more than the "
            f"{ACA_MAX_REPLICAS} a Container Apps gateway is planned for: deploy on AKS"
        ),
        "aks_too_big": (
            f"The peak needs {values.get('nodes')} nodes, more than one cluster is planned for "
            f"({AKS_MAX_NODES}): split the traffic over gateways in more regions"
        ),
        "database_too_small": (
            f"{values.get('replicas')} replicas need more database connections than the largest "
            "server offers: split the traffic over more gateways"
        ),
    }
    return texts.get(note.code, note.code)


def postgres_size(sku: str) -> PostgresSize:
    return next(
        (size for size in POSTGRES_SIZES if size.sku.casefold() == sku.casefold()),
        POSTGRES_SIZES[0],
    )


def node_family(node_size: str) -> int | None:
    """The index in NODE_FAMILIES of a ``Standard_D<n>ds_v<k>`` size, or None for others."""
    lowered = node_size.casefold()
    return next(
        (i for i, (suffix, _, _) in enumerate(NODE_FAMILIES) if lowered.endswith(suffix)), None
    )


def node_in_family(node_size: str, family: int) -> str:
    """The same node size in another of NODE_FAMILIES (``Standard_D8ds_v5`` -> ``_v7``)."""
    current = node_family(node_size)
    if current is None:
        return node_size
    stem = node_size[: -len(NODE_FAMILIES[current][0])]
    return stem + NODE_FAMILIES[family][0]


def node_pods(node_size: str) -> int | None:
    """LiteLLM pods a node of this size takes (NODE_SIZES, in any of NODE_FAMILIES)."""
    family = node_family(node_size)
    if family is None:
        return None
    base = node_in_family(node_size, 0).casefold()
    return next((pods for size, _, pods in NODE_SIZES if size.casefold() == base), None)


def call_seconds(sizing: GatewaySizing) -> float:
    """Average seconds from the request to the last token."""
    if sizing.response_seconds is not None:
        return sizing.response_seconds
    first, per_second = SPEEDS[sizing.model_speed]
    prefill = sizing.input_tokens / 1000 * PREFILL_SECONDS_PER_1K
    return first + prefill + sizing.output_tokens / per_second


def first_token_seconds(sizing: GatewaySizing) -> float | None:
    """Seconds to the first token from the speed preset; None with the scenario's own time."""
    if sizing.response_seconds is not None:
        return None
    first, _ = SPEEDS[sizing.model_speed]
    return first + sizing.input_tokens / 1000 * PREFILL_SECONDS_PER_1K


def cpu_ms(sizing: GatewaySizing) -> float:
    tokens = sizing.input_tokens + sizing.output_tokens
    streamed = 0.0
    if sizing.streaming:
        per_chunk = (
            CPU_MS_PER_STREAMED_TOKEN
            + sizing.input_tokens / 1000 * CPU_MS_PER_STREAMED_TOKEN_PER_1K_INPUT
        )
        streamed = sizing.output_tokens * per_chunk
    return CPU_MS_PER_REQUEST + tokens / 1000 * CPU_MS_PER_1K_TOKENS + streamed


def _replica_in_flight(sizing: GatewaySizing, memory_gib: float) -> int:
    per_call = (sizing.input_tokens + sizing.output_tokens) * BYTES_PER_TOKEN * BODY_COPIES
    room = max(memory_gib * MEMORY_TARGET - BASE_MEMORY_GIB, 0.1) * 2**30
    return max(1, min(MAX_IN_FLIGHT_PER_REPLICA, int(room // per_call)))


def _database(max_replicas: int, trial: bool) -> tuple[PostgresSize, int, int]:
    """The database size, its connections at most, and the pool per replica."""
    if trial:
        size = POSTGRES_SIZES[0]
        return size, max_replicas * DB_POOL * 2, DB_POOL
    needed = max_replicas * DB_POOL * 2
    for size in _GENERAL:
        if needed <= size.connections * DATABASE_TARGET:
            return size, needed, DB_POOL
    largest = _GENERAL[-1]
    pool = int(largest.connections * DATABASE_TARGET // (max_replicas * 2))
    return largest, max_replicas * max(pool, 1) * 2, pool


def _redis(rps: float, max_replicas: int) -> str | None:
    if max_replicas <= 1:
        return None
    return next((sku for limit, sku in REDIS_SIZES if rps <= limit), REDIS_LARGEST)


def _aks(
    sizing: GatewaySizing, min_pods: int, peak_pods: int, max_pods: int, slow: int
) -> tuple[AksPlan, list[PlanNote], list[PlanNote]]:
    warnings: list[PlanNote] = []
    problems: list[PlanNote] = []
    least = 3 if sizing.zone_redundant else 1
    chosen = NODE_SIZES[-1]
    for size in NODE_SIZES:
        if math.ceil(max_pods / size[2]) <= AKS_PREFERRED_NODES:
            chosen = size
            break
    node_size, vcpus, per_node = chosen
    node_max = max(least, math.ceil(max_pods / per_node))
    node_min = min(node_max, max(least, math.ceil(min_pods / per_node)))
    if node_max > AKS_MAX_NODES:
        problems.append(PlanNote(code="aks_too_big", values={"nodes": node_max}))
    # Every call to Foundry holds a SNAT port: with every call slowed to the timeout, spread
    # over the nodes the peak runs on. Each IP's ports are shared by the most nodes there can
    # be, one more for an upgrade's surge node.
    peak_nodes = max(node_min, math.ceil(peak_pods / per_node))
    needed = math.ceil(slow / peak_nodes)
    slots = node_max + 1
    ips = max(1, math.ceil(min(needed, SNAT_PORTS_PER_IP) * slots / SNAT_PORTS_PER_IP))
    ips = min(ips, MAX_OUTBOUND_IPS)
    ports = min(SNAT_PORTS_PER_IP, SNAT_PORTS_PER_IP * ips // slots // 8 * 8)
    if ports < needed:
        # Calls queue for a port only in that slowed-down case; at the usual pace they fit.
        warnings.append(PlanNote(code="outbound_capped", values={"ports": ports, "needed": needed}))
    plan = AksPlan(
        node_size=node_size,
        node_vcpus=vcpus,
        pods_per_node=per_node,
        node_min=node_min,
        node_max=node_max,
        tier="Standard" if sizing.zone_redundant else "Free",
        outbound_ips=ips,
        outbound_ports=ports,
        ports_needed=needed,
    )
    return plan, warnings, problems


def recommended(sizing: GatewaySizing) -> tuple[GatewayPlatform, list[PlanNote]]:
    """Where the gateway should run, and why."""
    aca = _plan(sizing, "aca")
    reasons: list[PlanNote] = []
    if sizing.self_hosted_models:
        reasons.append(PlanNote(code="platform_gpu"))
    if sizing.kubernetes_team:
        reasons.append(PlanNote(code="platform_team"))
    reasons += [note for note in aca.problems if note.code in {"aca_timeout", "aca_replicas"}]
    platform: GatewayPlatform = "aks" if reasons else "aca"
    reasons = [
        PlanNote(code="platform_timeout", values=note.values)
        if note.code == "aca_timeout"
        else PlanNote(code="platform_scale", values=note.values)
        if note.code == "aca_replicas"
        else note
        for note in reasons
    ]
    if platform == "aca":
        reasons.insert(0, PlanNote(code="platform_simple"))
    return platform, reasons


def plan(sizing: GatewaySizing, platform: GatewayPlatform | None = None) -> GatewayPlan:
    """The plan for ``platform``, or for the recommended one; the other platform's plan says
    only that it isn't the recommended one (its problems say why it can't run, if so)."""
    best, reasons = recommended(sizing)
    chosen = platform or best
    result = _plan(sizing, chosen)
    if chosen != best:
        reasons = [] if result.problems else [PlanNote(code="platform_chosen")]
    if sizing.private_network:
        reasons.append(PlanNote(code="private_aks" if chosen == "aks" else "private_manual"))
    return result.model_copy(update={"reasons": reasons})


def plans(sizing: GatewaySizing) -> GatewayPlans:
    best, _ = recommended(sizing)
    return GatewayPlans(recommended=best, aca=plan(sizing, "aca"), aks=plan(sizing, "aks"))


def _plan(sizing: GatewaySizing, platform: GatewayPlatform) -> GatewayPlan:
    warnings: list[PlanNote] = []
    problems: list[PlanNote] = []
    rps = sizing.peak_rpm / 60
    tpm = sizing.peak_rpm * (sizing.input_tokens + sizing.output_tokens)
    seconds = call_seconds(sizing)
    in_flight = math.ceil(rps * seconds)
    slow = math.ceil(rps * sizing.timeout_seconds)
    cost = cpu_ms(sizing)
    replica_rps = WORKER_CORES * TARGET_CPU * 1000 / cost

    def size_for(memory_gib: float) -> tuple[float, int]:
        holds = _replica_in_flight(sizing, memory_gib)
        return max(rps / replica_rps, in_flight / holds), holds

    memory = AKS_POD[1] if platform == "aks" else ACA_STANDARD[1]
    load, holds = size_for(memory)
    peak = max(1, math.ceil(load))
    spare = 1 if sizing.zone_redundant else 0
    maximum = max(peak, math.ceil(load * (1 + sizing.headroom_percent / 100))) + spare
    least = 2 if sizing.zone_redundant else 1
    minimum = min(maximum, max(least, math.ceil(peak / 2)))
    maximum = max(maximum, minimum)
    trial = maximum == 1
    if platform == "aks":
        cpu, memory = AKS_POD
    elif trial:
        # One small replica, if the calls it holds still fit.
        small_load, small_holds = size_for(ACA_SMALL[1])
        if small_load <= 1:
            cpu, memory = ACA_SMALL
            holds = small_holds
        else:
            cpu, memory = ACA_STANDARD
    else:
        cpu, memory = ACA_STANDARD
    bound: Literal["cpu", "in_flight"] = (
        "cpu" if rps / replica_rps >= in_flight / holds else "in_flight"
    )

    database, connections, pool = _database(maximum, trial)
    if pool < DB_POOL:
        warnings.append(PlanNote(code="pool_lowered", values={"pool": pool}))
    if pool < 2:
        problems.append(PlanNote(code="database_too_small", values={"replicas": maximum}))
    redis = _redis(rps, maximum)

    if seconds > sizing.timeout_seconds:
        warnings.append(
            PlanNote(
                code="call_exceeds_timeout",
                values={"seconds": round(seconds, 1), "timeout": sizing.timeout_seconds},
            )
        )
    if maximum == 1:
        warnings.append(PlanNote(code="single_replica"))
    if rps >= REDIS_BUFFER_RPS or maximum >= REDIS_BUFFER_REPLICAS:
        warnings.append(PlanNote(code="redis_buffer"))
    warnings.append(PlanNote(code="foundry_quota", values={"tpm": tpm, "rpm": sizing.peak_rpm}))

    aks: AksPlan | None = None
    if platform == "aks":
        aks, more_warnings, more_problems = _aks(sizing, minimum, peak, maximum, slow)
        warnings += more_warnings
        problems += more_problems
    else:
        # Silent for longer than the ingress allows: a call that doesn't stream and may run past
        # it, or a streamed one whose first token comes later than that.
        first = first_token_seconds(sizing) or 0.0
        silent = first if sizing.streaming else max(float(sizing.timeout_seconds), seconds)
        if silent > ACA_INGRESS_SECONDS:
            problems.append(
                PlanNote(
                    code="aca_timeout",
                    values={
                        "timeout": sizing.timeout_seconds,
                        "limit": ACA_INGRESS_SECONDS,
                        "streaming": "yes" if sizing.streaming else "no",
                    },
                )
            )
        if maximum > ACA_MAX_REPLICAS:
            problems.append(
                PlanNote(
                    code="aca_replicas", values={"replicas": maximum, "limit": ACA_MAX_REPLICAS}
                )
            )

    bandwidth = rps * (sizing.input_tokens + sizing.output_tokens) * BYTES_PER_TOKEN * 2 * 8 / 1e6
    return GatewayPlan(
        platform=platform,
        profile="dev" if trial else "production",
        rps=round(rps, 2),
        tpm=tpm,
        call_seconds=round(seconds, 2),
        in_flight=in_flight,
        in_flight_slow=slow,
        cpu_ms=round(cost, 2),
        replica_rps=round(replica_rps, 1),
        replica_in_flight=holds,
        bound_by=bound,
        peak_replicas=peak,
        min_replicas=minimum,
        max_replicas=maximum,
        scale_rps=max(1, math.floor(rps / peak)),
        replica_cpu=cpu,
        replica_memory_gib=memory,
        bandwidth_mbps=round(bandwidth, 2),
        postgres_sku=database.sku,
        postgres_tier="Burstable" if database.edition == "Burstable" else "GeneralPurpose",
        postgres_connections=connections,
        postgres_limit=database.connections,
        db_pool=max(pool, 1),
        redis_sku=redis,
        aks=aks,
        request_timeout=sizing.timeout_seconds,
        route_timeout=max(MIN_ROUTE_SECONDS, sizing.timeout_seconds * (RETRIES + 1) + 60),
        reasons=[],
        warnings=warnings,
        problems=problems,
    )
