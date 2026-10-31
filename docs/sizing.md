[← SteadyApiPool](../README.md)

**English** | [简体中文](sizing.zh-CN.md)

# Sizing a LiteLLM gateway

How to estimate, from a customer's traffic, the LiteLLM gateway they need: Container Apps
replicas, PostgreSQL, Redis, or the AKS nodes and pods. It is the arithmetic the app's sizer
(**Deploy a gateway** in the setup, `backend/pool_manager/litellm/sizing.py`) does, written out so
you can follow it, check it by hand or explain it to a customer. The app deploys exactly the plan
the sizer computes, so the numbers here are the ones you get.

A plan is an estimate. The CPU figures were measured, the memory ones are estimates, and your
prompts and region differ: after deploying, [load-test a pool](getting-started.md#load-test-the-pool-optional)
at the peak to confirm it.

## What to ask the customer

| Input | What it means | Default |
| --- | --- | --- |
| **Peak requests a minute (RPM)** | All callers together at the busiest minute, not the average | (required) |
| **Average input tokens** | Prompt per call, including system prompt, history and tool results | (required) |
| **Average output tokens** | Answer per call | (required) |
| **Timeout** | The longest a call may take (LiteLLM's `request_timeout`) | 120 s |
| **Streaming** | Whether answers are streamed | yes |
| Model speed | fast, standard or reasoning, or their own average response time | standard |
| Headroom | Extra above the peak | 25 % |
| Zone redundancy | Survive losing an availability zone | on |
| AKS team, own GPU models, private network | Reasons to prefer AKS | off |

Customers often give **tokens a minute (TPM)** instead of RPM. Convert with the tokens a call uses:

```
RPM = TPM ÷ (input tokens + output tokens)
```

For example, three agents at about 15,000 TPM each are 45,000 TPM; at 2,000 input and 500 output
tokens a call that is 45,000 ÷ 2,500 = **18 RPM** on average. Size for the peak: if it is twice
the average, enter **36 RPM**.

## The short version

- **Streaming is what makes a gateway big.** LiteLLM spends about 27 ms of CPU on a call whatever
  its size, but about 0.75 ms on every streamed token. With a 2,000-token prompt, a 500-token
  streamed answer costs about 19 times the same answer unstreamed; a large prompt costs little
  unless the answer is streamed.
- **One replica** (one LiteLLM worker, kept at 70 % of a core) carries about **1,500 RPM without
  streaming**, about **80 RPM of streamed chat** (2,000 in / 500 out) and about **17 RPM** of
  streamed 50,000-token agent steps.
- **Every production gateway has at least 2 replicas** (3 AKS nodes), a General Purpose
  PostgreSQL and Redis, however small the traffic.
- **Container Apps** unless it can't: more than **30 replicas**, calls that may send nothing for
  **240 s** (not streamed and slow, or a first token that late), a Kubernetes team, or own GPU
  models. Then **AKS**.

Container Apps replicas (fewest–most) for common call shapes, with the defaults above (standard
model, 120 s timeout, 25 % headroom, zone redundancy). "AKS" means Container Apps is ruled out,
with the AKS nodes instead:

| Call shape (input / output tokens) | 60 RPM | 300 RPM | 1,000 RPM | 3,000 RPM | 10,000 RPM |
| --- | --- | --- | --- | --- | --- |
| Short answer, 500 / 100, streamed | 2 | 2 | 2–5 | 4–11 | AKS: 3–7 × D8ds_v5 |
| Chat, 2,000 / 500, streamed | 2 | 2–6 | 7–17 | AKS: 4–10 × D8ds_v5 | AKS: 6–13 × D16ds_v5 |
| RAG, 8,000 / 300, streamed | 2 | 2–6 | 7–17 | AKS: 4–10 × D8ds_v5 | AKS: 6–13 × D16ds_v5 |
| Agent step, 50,000 / 400, streamed | 2–6 | 10–24 | AKS: 3–7 × D16ds_v5 | AKS: 8–19 × D16ds_v5 | AKS: 26–63 × D16ds_v5 |
| Any of these, not streamed | 2 | 2 | 2 | 2–4 | 4–10 (4–11 for 50,000) |

All of them get Redis `Balanced_B0`. PostgreSQL is `Standard_D2ds_v5` up to 33 replicas, then
larger ([step 7](#7-postgresql)).

## Step by step

Each step gives the formula, then the customer above (36 RPM, 2,000 in / 500 out, streamed, the
defaults) as the example.

### 1. Requests and tokens

```
requests a second = RPM ÷ 60
tokens a minute   = RPM × (input + output)
```

The tokens a minute are the **Foundry quota** the pool's deployments need together. The gateway
doesn't change that; a pool of several deployments adds their quotas up.

> 36 ÷ 60 = **0.6 requests a second**; 36 × 2,500 = **90,000 TPM** of Foundry quota.

### 2. How long a call takes, and calls in flight

```
call time       = first token + 0.1 s per 1,000 input tokens + output ÷ tokens a second
calls in flight = requests a second × call time
slowed calls    = requests a second × timeout      (if Foundry slowed every call to the timeout)
```

| Model speed | First token | Tokens a second |
| --- | --- | --- |
| fast | 0.5 s | 120 |
| standard | 1 s | 60 |
| reasoning | 8 s | 40 |

Or enter the average response time yourself, and it replaces this formula.

> 1 + 0.2 + 500 ÷ 60 = **9.5 s** a call; 0.6 × 9.5 = **6 calls in flight**; 0.6 × 120 = **72**
> if every call took the whole timeout.

### 3. Gateway CPU per call, so what one replica carries

```
CPU per call = 27 ms
             + 0.1 ms per 1,000 tokens (input + output)
             + if streamed: output × (0.75 ms + 0.11 ms per 1,000 input tokens)

requests a second one replica carries = 700 ms ÷ CPU per call
```

The 700 ms is one LiteLLM worker (one core) kept at 70 %; a replica runs one worker. The figures
were measured with `calibrate.py` on LiteLLM v1.106.4 configured as the app deploys it (a virtual
key, spend batched, OpenTelemetry on). The 0.11 ms is OpenTelemetry's cost per streamed chunk,
which grows with the prompt.

> 27 + 0.25 + 500 × (0.75 + 0.22) = **512 ms** a call; 700 ÷ 512 = **1.4 requests a second**
> (about 82 RPM) per replica.

### 4. Calls one replica holds in memory

```
memory per call = (input + output) × 4 bytes × 8 copies
calls held      = (memory × 80 % − 1.5 GiB) ÷ memory per call, at most 1,000
```

A replica has 4 GiB (Container Apps and AKS alike), so 1.7 GiB for calls in flight. This only
limits very large prompts with slow answers; for most traffic it is the 1,000 cap.

> 2,500 × 32 = 80 KB a call; 1.7 GiB holds over 20,000, so **1,000**.

### 5. Replicas

```
load     = the larger of  requests a second ÷ step 3  and  calls in flight ÷ step 4
at peak  = load, rounded up (at least 1)
most     = the larger of (at peak) and (load × (1 + headroom), rounded up),
           + 1 spare with zone redundancy
fewest   = half the peak, rounded up, at least 2 with zone redundancy (1 without), at most "most"
```

The fewest replicas run all the time; the autoscaler adds them up to the most.
With zone redundancy off and a peak one replica carries, the plan is a single replica: the **dev**
profile (a trial, no redundancy, Burstable database, no Redis).

> load = max(0.6 ÷ 1.4, 6 ÷ 1,000) = 0.44, so **1 at the peak**; most = 1 + 1 spare = **2**;
> fewest = **2**. The gateway runs 2 replicas and never needs more.

### 6. Container Apps replica and scale rules

| | Value |
| --- | --- |
| Replica | 2 vCPU, 4 GiB (1 vCPU, 2 GiB for a single dev replica), plus the OpenTelemetry collector sidecar 0.25 vCPU, 0.5 GiB |
| Scale out on calls in flight | more than (calls in flight ÷ replicas at the peak) per replica |
| Scale out on CPU | 27 % of the replica's allocation, which is 60 % of its one worker's core (60 × 1 ÷ 2.25) |
| Limit | 30 replicas a plan; the ingress ends a request that sends nothing for 240 s |
| Zones | with zone redundancy, a zone-redundant environment (in a virtual network of its own; Azure sets that only when it's created) |

60 % is below the 70 % the replicas are sized at, so a new replica (a few minutes to start)
arrives before the others are saturated.

> 2 replicas of 2 vCPU / 4 GiB; scale on 6 calls in flight per replica or 27 % CPU.

### 7. PostgreSQL

```
connections = most replicas × 10 (LiteLLM's pool per replica) × 2 (an update runs old and new side by side)
```

The plan takes the smallest General Purpose server whose connections, at 80 %, cover that:

| Server | Connections (80 %) | Up to |
| --- | --- | --- |
| `Standard_B1ms` (Burstable) | 35 | a single dev replica only |
| `Standard_D2ds_v5` | 844 (675) | 33 replicas |
| `Standard_D4ds_v5` | 1,703 (1,362) | 68 replicas |
| `Standard_D8ds_v5` | 3,422 (2,737) | 136 replicas |
| `Standard_D16ds_v5` | 4,985 (3,988) | 199 replicas |

Past 199 replicas the plan keeps D16ds_v5 and lowers LiteLLM's pool per replica (a warning);
below 2 connections a replica (about 1,000 replicas) it refuses and says to split the traffic.
Burstable isn't used for more than one replica: it runs out of CPU credits under steady load.

**High availability.** With zone redundancy the server (General Purpose: zone redundancy means
two replicas at least) gets a standby in another zone that takes over if it fails. Azure bills the
standby like the server, so the database costs twice as much. Without it the database is a single
server: while it restarts (maintenance, a resize) or fails, the gateway may refuse calls and stops
recording spend until it's back.

> 2 × 10 × 2 = 40 connections: **`Standard_D2ds_v5`**, with a standby in another zone.

### 8. Redis

| Requests a second at the peak | Azure Managed Redis |
| --- | --- |
| a single replica | none |
| up to 1,000 | `Balanced_B0` |
| up to 2,500 | `Balanced_B1` |
| up to 5,000 | `Balanced_B3` |
| more | `Balanced_B5` |

Redis shares the cooldowns and counters between replicas. From 1,000 requests a second or 10
replicas, spend is also written through Redis (`use_redis_transaction_buffer`, LiteLLM's guidance).

> 0.6 requests a second over 2 replicas: **`Balanced_B0`**, spend written directly.

### 9. AKS nodes and outbound ports

On AKS each replica is a pod of 1 vCPU and 4 GiB (requests and limits), so the pods are the
replicas of step 5. Nodes:

| Node | Pods each |
| --- | --- |
| `Standard_D4ds_v5` | 2 |
| `Standard_D8ds_v5` | 5 |
| `Standard_D16ds_v5` | 12 |

```
node size = the smallest whose (most pods ÷ pods each) is 10 nodes or fewer (else D16ds_v5)
most nodes   = most pods ÷ pods each, rounded up, at least 3 with zone redundancy
fewest nodes = fewest pods ÷ pods each, rounded up, at least 3 with zone redundancy
tier         = Standard with zone redundancy, else Free
```

More than 100 nodes is refused: split the traffic over gateways in more regions. Where the
subscription can't use the Ddsv5 family in the region, the same size in Ddsv7, then Ddsv6, is
taken.

**Outbound ports.** Every call to Foundry holds a SNAT port while it runs. Each node needs ports
for its share of the slowed calls of step 2:

```
ports a node = slowed calls ÷ nodes at the peak
outbound IPs = enough that (most nodes + 1 for an upgrade) × ports a node fit in 64,000 per IP, at most 16
```

If 16 IPs are still short, the plan warns: calls would only wait for a port when Foundry is that
slow.

**Timeouts.** The route on AKS allows a call and its retry: the larger of 600 s and
(timeout × 2 + 60 s).

**Draining.** On both platforms a replica or pod that stops (scaling in, a new revision, a node's
maintenance) takes no new calls and waits for those in flight: a call and its retry, timeout × 2
(240 s at the default 120 s), then 30 s more before it is killed.

> 2 pods: **3 × `Standard_D4ds_v5`** (3 for the zones, 2 pods running), Standard tier; 72 slowed
> calls over 3 nodes = 24 ports a node, 1 outbound IP with 16,000 ports a node.

### 10. Container Apps or AKS

The sizer recommends **AKS** when any of these holds, and **Container Apps** otherwise:

| Reason | Why |
| --- | --- |
| Not streamed, and the timeout (or the call time) is over 240 s | The Container Apps ingress ends a request that sends nothing for 240 s (measured: a 504 at 241 s) |
| Streamed, but the first token may come after 240 s | Same; once tokens flow, a streamed call may run longer |
| More than 30 replicas at the most | Container Apps' plan limit |
| The customer runs AKS already, or hosts own GPU models | Their choice of platform |

A private network works on both, but isn't automated: on Container Apps, put the environment in a
virtual network afterwards; on AKS, use an internal load balancer or private ingress.

The 240 s can be raised to 30 minutes with Container Apps' premium ingress, which needs a
dedicated workload profile of at least two nodes, billed all the time. The app doesn't set it up,
and the sizer still assumes 240 s.

> Streamed, first token after 1.2 s, 2 replicas: **Container Apps**.

## Worked examples

All with a standard-speed model, 25 % headroom and zone redundancy.

| | Customer's agents | Chat | Large chat | Reasoning agent |
| --- | --- | --- | --- | --- |
| Peak | 36 RPM | 1,800 RPM | 15,000 RPM | 300 RPM |
| Tokens (in / out) | 2,000 / 500 | 2,000 / 500 | 2,000 / 500 | 20,000 / 2,000 |
| Streamed, timeout | yes, 120 s | yes, 120 s | yes, 120 s | **no**, 600 s, reasoning model |
| Foundry quota needed | 90,000 TPM | 4.5 M TPM | 37.5 M TPM | 6.6 M TPM |
| Call time, in flight | 9.5 s, 6 | 9.5 s, 286 | 9.5 s, 2,384 | 60 s, 300 |
| CPU a call, per replica | 512 ms, 82 RPM | 512 ms, 82 RPM | 512 ms, 82 RPM | 29 ms, 1,440 RPM |
| Replicas (fewest–most) | 2–2 | 11–29 | 92–230 | 2–2 |
| **Recommended** | **Container Apps** | **Container Apps** | **AKS** (over 30 replicas) | **AKS** (silent past 240 s) |
| Container Apps | 2 × 2 vCPU / 4 GiB | 11–29 × 2 vCPU / 4 GiB | ruled out | ruled out |
| AKS | 3 × D4ds_v5 | 3–6 × D8ds_v5 | 8–20 × D16ds_v5 | 3 × D4ds_v5 |
| PostgreSQL | D2ds_v5 | D2ds_v5 | D16ds_v5 (pool lowered to 8) | D2ds_v5 |
| Redis | B0 | B0, spend through Redis | B0, spend through Redis | B0 |

The chat and large chat plans are the two that were [benchmarked](benchmarks.md): the
Container Apps gateway carried **3,720 RPM** of streamed chat against the 1,800 it was sized for,
the AKS one **11,280 RPM** against 15,000. So the Container Apps plans have room to spare, and
for large AKS plans, go by the measured number until you have load-tested your own.

## What a plan doesn't cover

- **Foundry quota.** The gateway can't serve more than the pool's deployments allow. Step 1's
  tokens a minute is what the pool needs together; a pool spreads it over several deployments
  and fails over when one returns 429.
- **Price.** The deploy form prices the plan in the region you pick (retail list prices, from
  the fewest replicas or nodes to the most). Once deployed, the gateway panel shows the month's
  actual spend.
- **Region capacity.** The deploy form checks the region offers each resource and the
  subscription has quota, before anything is created.
- **Changing traffic.** **Resize for new traffic** in the gateway panel recomputes the plan for
  a gateway the app deployed and applies it after you confirm. It resizes the database whenever
  the server the gateway has can't take the new replicas' connections (step 7), and leaves what
  Azure only sets at creation (zone redundancy, the database's geo-redundant backup) or what needs
  a command (the database's standby) to you.

## Run the sizer yourself

In the app: **Deploy a gateway** in the setup, then **How we got this** on each platform's card.

From a terminal, with no Azure needed (from `backend/`):

```sh
.venv/Scripts/python -c "from pool_manager.litellm import sizing; from pool_manager.litellm.models import GatewaySizing; print(sizing.plans(GatewaySizing(peak_rpm=36, input_tokens=2000, output_tokens=500, timeout_seconds=120, streaming=True)).model_dump_json(indent=2))"
```

`GatewaySizing` also takes `model_speed` (`"fast"`, `"standard"`, `"reasoning"`),
`response_seconds`, `headroom_percent`, `zone_redundant`, `kubernetes_team`,
`self_hosted_models` and `private_network`. The result has both platforms' plans, every number
above, and the notes the app shows.
