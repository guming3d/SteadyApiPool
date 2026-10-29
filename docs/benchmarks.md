[← SteadyApiPool](../README.md)

**English** | [简体中文](benchmarks.zh-CN.md)

# Benchmarks

How much traffic each gateway the app sets up carries, how much latency it adds, and what clients
see when a deployment fails, measured on Azure in October 2026. Use them as a reference for
choosing and sizing a gateway, not as a guarantee: your prompts, models, regions and network
differ, and the [sizer](getting-started.md#sizing-the-gateway) and a
[load test of your own pool](getting-started.md#load-test-the-pool-optional-litellm) are the way
to check your case. How it was measured, and how to measure it again, is in
[perf/README.md](../perf/README.md); every step of every run is in
[perf/results-2026-10/report.md](../perf/results-2026-10/report.md).

## What was tested

| Gateway | Size | Region |
|---|---|---|
| Azure API Management Basic v2 | 1 unit | South Central US |
| Azure API Management Standard v2 | 1 unit | South Central US |
| LiteLLM on Azure Container Apps | the sizer's plan for 1,800 RPM of chat: 11–29 replicas of 2 vCPU, PostgreSQL D2ds_v5, Redis B0 | West US 3 |
| LiteLLM on AKS | the sizer's plan for 15,000 RPM of chat: 8–20 D16ds_v5 nodes, 92–230 pods, PostgreSQL D16ds_v5, Redis B0 | Japan East |

Each gateway was created by the app, with the app's own pool settings (APIM: circuit breaker on
1 failure for 60 s, retry on 429; LiteLLM: cooldown on the first failure for 60 s, 1 retry, 120 s
timeout), and loaded from a VM in its own region. LiteLLM ran in other regions than APIM because
the test subscription couldn't create PostgreSQL in South Central US, Azure Managed Redis in
Central US, East US 2 or West Europe, or enough PostgreSQL vCores in West US 3; the results
compare gateways, each against a client next to it.

**The model.** For capacity and failover, the pool's three members are stand-in Azure OpenAI
deployments that answer like a model of a set speed (first token after 1 s plus 0.1 s per 1,000
prompt tokens, then 60 tokens a second), so what runs out is the gateway, not a model's quota.
A real model's quota runs out far sooner; that part was measured separately, with gpt-6-luna.

**The call shapes.**

| Shape | Input tokens | Output tokens | Streamed | Time to the last token (stand-in) |
|---|---|---|---|---|
| Short answer | 500 | 100 | no | 2.7 s |
| Chat | 2,000 | 500 | yes | 9.5 s |
| RAG | 8,000 | 300 | yes | 6.8 s |
| Agent loop step | 50,000 | 400 | no | 12.7 s |

**Passing a step.** Load started on schedule (open loop) for 2 minutes after a warm-up. A step
passed when at most 1% of requests failed, at least 95% of the load was served, and the p95
latency stayed within 1.25 × the same call sent straight to the stand-in model.

## Capacity

The highest load (requests a minute) each gateway passed. "≥" means it passed the highest step
tried, so it carries more.

| Shape | APIM Basic v2 | APIM Standard v2 | LiteLLM on Container Apps | LiteLLM on AKS |
|---|---|---|---|---|
| Short answer | ≥ 22,500 | ≥ 22,500 | ≥ 22,500 | ≥ 15,000 |
| Chat (streamed) | 4,500 | 12,000 | 3,720 | 11,280 |
| RAG (streamed) | 2,640 | 5,280 | 5,280 | 14,280 |
| Agent loop step | 10,500 | 16,860 | ≥ 22,500 | ≥ 15,000 |
| List price a month | about $150 (1 unit) | about $700 (1 unit) | about $760–5,320 | about $8,350–18,580 |

APIM's monthly price is the unit's; calls above the tier's included ones are charged on top
(Basic v2 $0.03, Standard v2 $0.025 per 10,000 at list price), which at these loads is more than
the unit. LiteLLM's ranges run from the fewest replicas or nodes to the most its autoscaler adds.

- **Streaming is what costs.** Every gateway carries several times more short answers than
  streamed chat: a streamed answer is hundreds of small chunks, each one passing through the
  gateway. The agent loop's 50,000-token prompt costs far less than streaming its answer would.
- **APIM** slows down gradually past its capacity (its first token comes later and later) and
  starts failing calls (5xx) on the largest prompts. A second unit or Standard v2 is the next
  step; Standard v2 carried 2.7× Basic v2's chat.
- **LiteLLM on Container Apps** carried twice the chat load it was sized for (3,720 RPM against
  1,800) and then failed abruptly: at 4,500 RPM calls queued for over 30 s and some failed. Size
  it for your peak with the sizer, which also says when to move to AKS.
- **LiteLLM on AKS** carried 11,280 RPM of chat against the 15,000 it was sized for, and
  14,280 of RAG. Its pods (92 to 230) and its ingress (2 to 5 replicas) scaled up during the
  ramps; each step had a 2-minute warm-up, and with streaming the ingress's 5 replicas may be
  what ran out first. Plan on the measured number, not the sized one, until you have load-tested
  your own.

## Latency the gateway adds

At 600 requests a minute, the median and p95 latency on top of the same call sent straight to
the stand-in model, in milliseconds:

| Shape | APIM Basic v2 | APIM Standard v2 | LiteLLM on Container Apps | LiteLLM on AKS |
|---|---|---|---|---|
| Short answer | +6 / +9 | +6 / +13 | +22 / +40 | +22 / +33 |
| Chat (streamed) | +8 / +51 | +5 / +12 | +69 / +195 | +57 / +70 |
| RAG (streamed) | +7 / +20 | +6 / +10 | +50 / +88 | +44 / +53 |
| Agent loop step | +9 / +12 | +9 / +14 | +27 / +48 | +26 / +35 |

Against a model call of 2–13 seconds, both are small: APIM adds under 10 ms at the median,
LiteLLM 20–70 ms. Near each gateway's capacity the p95 grows to one or two seconds before a
step fails; the [full results](../perf/results-2026-10/report.md) list every step.

## When a deployment fails

At 1,200 requests a minute of streamed chat, the stand-in `mock-a` (the pool's first member)
was made to answer 429 for 2 minutes, then 500, then nothing at all, with a healthy period after
each.

| `mock-a` | APIM Basic v2 | APIM Standard v2 | LiteLLM on Container Apps | LiteLLM on AKS |
|---|---|---|---|---|
| answers 429 | 0% failed, moved at once | 0% | 0% | 0% |
| answers 500 | 0.08% failed | 0.08% | 0% | 0% |
| comes back | traffic back after 60 s | after 60 s | after the 60 s cooldown | after the 60 s cooldown |
| hangs (never answers) | **99% failed** with 500 after the 120 s timeout | **99.8%** | 0.4% failed; answers after ~130 s | 0%; answers after ~130 s |

- A member that answers with an error is taken out at once on both gateways, and clients don't
  notice.
- A member that accepts calls and never answers is only found out when a call times out. Until
  then every call goes to it: APIM's retry covers 429 only, so those calls fail with 500;
  LiteLLM retries them on the next member, so they succeed, two minutes late. Lower the pool's
  timeout if your calls are short, so a hung region costs less.

## With a real model

The same calls straight to one gpt-6-luna deployment (125 requests and 125,000 tokens a minute,
Central US) and through each gateway's pool of four gpt-6-luna deployments (Central US first,
then East US 2, East US 2 DataZone and Sweden Central DataZone), each from its gateway's VM.

The loads are low because of the model's quota. On gpt-6-luna a quota unit allows 1 request and
1,000 tokens a minute; the first deployment has 125 units and the pool's four have 450 together,
all of the test subscription's gpt-6-luna quota. Whichever limit a call reaches first applies: 125
short answers a minute, but only about 3.5 agent loop steps, whose prompts fill the token limit.
The prompts were the same text as in the stand-in tests, which size it at four characters a
token; gpt-6-luna's tokenizer counts fewer, so the real calls were smaller than the shapes' names
say:

| Shape | Prompt tokens | Answer tokens |
|---|---|---|
| Short answer | 359 | 84–87 |
| Chat | 1,414 | 415–434 |
| Agent loop step | 35,158 | 281–350 |


| Load | Straight to one deployment | APIM Basic v2 | APIM Standard v2 | LiteLLM on Container Apps | LiteLLM on AKS |
|---|---|---|---|---|---|
| Short answers, 120 a minute | 100% | 100% | 100% | 100% | 100% |
| Short answers, 240 a minute | 70% (the rest 429) | 100% | 100% | 100% | 100% |
| Short answers, 360 a minute | 35% (the rest 429) | 100% | 100% | 100% | 100% |
| Chat, 160 a minute | 75% (the rest 429) | 100% | 100% | 100% | 100% |
| Agent loop steps, 8 a minute | 44% (the rest 429) | 100% | 100% | 100% | 100% |

Once the first deployment's quota ran out, every pool answered from the next one in its order,
and then the one after, without a single failed call. The median latency was the model's (1.4–1.8 s
for a short answer, 4–5 s for streamed chat, with the first token after 0.85–0.95 s) with or
without a gateway: the runs took place at different times, so the differences between them are
the model's own variation, and the stand-in tests above measure what the gateway adds. From
Japan East (AKS) to the US deployments the first token came about 0.45 s later.

The first LiteLLM run found that LiteLLM refused `reasoning_effort` for the two deployments whose
name isn't their model's (`gpt-6-luna-dz`): it picks a model's parameters by the deployment's
name. The app now tells LiteLLM which model each Foundry deployment serves (`base_model`, read
from the deployment), and the results above are from the run after that fix.
