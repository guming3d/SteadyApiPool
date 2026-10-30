[← SteadyApiPool](../README.md)

**English** | [简体中文](features.zh-CN.md)

# Features and how it works

What the app does, how a pool maps onto each kind of gateway, and what deploying a LiteLLM
gateway creates. To get going, see the [step-by-step guide](getting-started.md).

## What it does

| Area | What it does |
| --- | --- |
| **Setup wizard** | Four steps: **Sign in**, **AI gateway**, **Permissions** and **Gateway → Foundry**. It checks sign-in, the gateway (for LiteLLM: reachable, database connected, master key valid), its managed identity, your access to it and the gateway's access to Foundry. It fixes what your Azure rights allow and writes a ready-to-send admin request for the rest. Locally it also chooses the kind of gateway, and picks, creates, connects or deletes the gateway. |
| **Gateway choice** | APIM: an existing service, or a new one. LiteLLM: sizes the gateway for your peak traffic and recommends Container Apps or AKS, deploys on Container Apps in one step, or creates a new AKS cluster and installs LiteLLM with HTTPS on it, or generates the files for a cluster you already have, or connects any LiteLLM proxy. Later, **Switch gateway** in the gateway panel (the gateway name in the top bar) opens the same choice, for either kind; the gateway you leave is not changed. Before switching, the app checks the new gateway: an APIM service has finished provisioning; a LiteLLM gateway has finished provisioning, isn't stopped, its master key can be read, and LiteLLM answers with its database connected. If it fails, you stay on the current gateway and see why, with **Switch anyway** to fix it in setup. |
| **Guided tour** | A two-minute walkthrough for people new to AI gateways. It runs once after setup; **Tour** in the top bar replays it. |
| **Deployment discovery** | Lists the Azure OpenAI / Foundry deployments in your subscriptions, grouped by model, and imports the ones you select into a new or existing pool. |
| **Deploy a model** | Creates a pay-per-token OpenAI model deployment in a Foundry account, using your own Azure rights, so you don't need the Foundry portal. |
| **Pools** | Create and edit pools of 2–30 members in **Failover**, **Balanced** or **Weighted** mode. On APIM a pool is a `Pool` backend, with optional session affinity; on LiteLLM it is a model group whose ID is the model name clients send. |
| **Members** | Edited inside their pool. Besides Foundry deployments, you can add any OpenAI-compatible endpoint (on LiteLLM with no auth, an API key stored by LiteLLM as an encrypted credential, or the gateway's managed identity). Members the app created go once no pool uses them. |
| **Failure handling** | A pool setting written to every member. APIM: one circuit breaker rule. LiteLLM: the cooldown (how many failures in a minute bench a member, and for how long), retries on another member and the timeout per attempt. |
| **Pool API (APIM)** | Creating a pool also creates and binds an HTTP API for it, so clients have an endpoint right away. **API binding** points an existing API at a pool through its policy, with a diff preview first; unbinding restores the original policy byte for byte. |
| **Model names and aliases (LiteLLM)** | The pool ID always works; aliases point other model names at the pool, so existing clients don't have to change. **View LiteLLM config** shows the pool as a LiteLLM `config.yaml` would write it (secrets left out), with a diff before you publish, and **Export YAML**. |
| **Routing preview** | Shows how the gateway routes traffic through the pool and simulates failures, breaker trips or cooldowns, and recovery. |
| **Test call** | Gets a test key (an APIM subscription key, or a LiteLLM virtual key), sends a test request from the page, shows which member answered, and gives ready-to-copy client code. |
| **Load test** | Simulated users calling the pool, from its call guide or `scripts/benchmark.py`: throughput, latency and time-to-first-token percentiles, 429s and the members that answered (on APIM, through a bound API, the regions that answered), plus failovers on LiteLLM, with a verdict and exports. |
| **Metrics** | Requests, availability, 429s and latency for the pool (from APIM's Azure Monitor metrics, or the LiteLLM gateway's OpenTelemetry traces in Application Insights), plus traffic share, tokens and latency per member from Azure Monitor. |
| **Gateway metrics** | The **Gateway metrics** tab: all the gateway's traffic (every API on APIM, every model on LiteLLM) and the load on what it runs on, from Azure Monitor. APIM: capacity on the classic tiers, the gateway's CPU and memory on the v2 tiers. LiteLLM on Container Apps: replicas against their maximum, and each replica's CPU and memory as a share of what it is given; on AKS the nodes' CPU and memory; and the PostgreSQL server deployed with it (CPU, memory, connections). Values above 70% are marked busy, above 90% saturated. A gateway you connected by URL shows only its traffic. |
| **Cost** | This month's spend for the gateway's resources and their list price. Open it from the gateway name in the top bar, which on LiteLLM also links LiteLLM's own admin UI: sign in as `admin` with the master key as the password. The app never shows the key; the panel gives the command that reads it with your own Azure rights (`az containerapp secret show` on Container Apps, `kubectl` on AKS). |
| **Same model** | Refuses to save a pool whose Foundry members serve different models. The model is read from Azure, not guessed from the deployment name. |
| **Pool quota** | Under a pool's members: the tokens and requests a minute its Foundry deployments allow together (each deployment's own `rateLimits`, else its capacity at 1,000 TPM and 6 RPM a unit). On LiteLLM it warns when that is less than the traffic the gateway was sized for. |
| **Resize (LiteLLM)** | **Resize for new traffic** in the gateway panel sizes a gateway this app deployed for a new peak and lists every change before applying it with your own Azure rights: replicas, their size and scale trigger on Container Apps; the node range and LiteLLM's pods on AKS; the database only when you tick it. It warns that production traffic may be affected and waits for you to confirm; what it can't change in place comes as commands. |
| **Removing** | Removes a pool, or everything this app created on the gateway, after showing exactly what will go. Locally it can also delete a gateway it created (an APIM service is soft-deleted and restorable for 48 hours; a LiteLLM gateway is gone for good). |
| **Public app (APIM)** | A website on Container Apps where anyone signs in with their own Microsoft account and works with their own Azure rights; nothing is stored on the server. |
| **Safety** | The app only changes and deletes what it created, and only after you have seen it. On APIM every update is ETag-conditional and it never rewrites policy it didn't write; on LiteLLM pools added elsewhere are shown read-only. Resources with a protected ID prefix (`POOL_MANAGER_PROTECTED_PREFIXES`) are read-only on both. |

### How a pool maps to APIM

| In the app | In APIM |
| --- | --- |
| Pool | A backend of type `Pool` whose `pool.services` are the members, with `priority` and `weight`, and optionally `sessionAffinity` |
| Member | A single backend with the deployment's URL and managed-identity auth (APIM's own identity, resource `https://cognitiveservices.azure.com`) |
| Circuit breaker | One rule, written to every member backend this app created |
| Pool API | An HTTP API with a policy that sets the pool as backend, sends the managed-identity token, and retries |
| Test key | A subscription scoped to the pool's API |

Defaults for a new pool:
- **Circuit breaker:** trips on 1 failure among 429 / 408 / 5xx / connection failure / timeout
  within 60 s, trips for 60 s, and honours `Retry-After`.
- **Retry:** 2 attempts, 1 s interval, fast first retry.
- **Backend timeout:** 120 s.

`POOL_MANAGER_POOL_API_VERSION` defaults to `2025-09-01-preview`: GA `2024-05-01` can't carry
`sessionAffinity`, so with it session affinity is off.

### How a pool maps to LiteLLM

| In the app | In LiteLLM |
| --- | --- |
| Pool | A model group: one model row per member, all with `model_name` = the pool ID |
| Member | A row with `litellm_params.model = azure/<deployment>` and `api_base` = the account endpoint, and no key: the gateway signs in with its managed identity (`enable_azure_ad_token_refresh`) |
| Priority / weight | `litellm_params.order` (priority + 1) and `litellm_params.weight`. LiteLLM uses the lowest order whose members aren't cooling down, and splits by weight within it |
| Cooldown | `model_info.allowed_fails` and `model_info.cooldown_time` on every row of the pool |
| Retries / timeout | `litellm_params.num_retries` and `timeout` on every row |
| Alias | `router_settings.model_group_alias` |
| Test key | A virtual key with alias `<prefix>test-<pool>`, scoped to the pool's model names |

Defaults for a new pool:
- **Cooldown:** a member that fails once within a minute (429s count) sits out 60 s; its traffic
  goes to the other members, or in failover to the next priority.
- **Retries:** 1 further attempt on another member.
- **Timeout per attempt:** 120 s.

**Worth knowing about LiteLLM:**
- **No ETags.** LiteLLM has no conditional updates, so a pool save is best-effort, not atomic. The
  app re-reads the pool's rows and compares them with what you loaded (and refuses if someone
  changed them), then writes new rows first, then changes, then removals, so the pool never runs
  empty. Rows have deterministic IDs, so publishing again after a failure finishes the save.
- **Keys are shown once.** LiteLLM keeps only a hash of each virtual key. Regenerating keys in
  place (`/key/regenerate`) is a LiteLLM Enterprise feature, so **Regenerate the test key**
  deletes the old test key and creates a new one.
- **Session pinning is gateway-wide.** Responses API follow-ups (`previous_response_id`,
  encrypted reasoning items) go back to the deployment that answered first. It is set in the
  gateway's config for every pool, not per pool.
- **No health checks.** Only real failed requests put a member in cooldown. When every member is
  cooling down, the client gets the last upstream error (a 429 stays a 429), so clients should
  back off and retry.
- With more than one replica, cooldowns are only shared through Redis, which the sizer adds to
  every plan with more than one replica.

### Discovered deployments

- **What is listed:** only `Succeeded` deployments in **OpenAI format** on `OpenAI` or `AIServices` accounts. Partner models (Anthropic, DeepSeek, xAI, …) use other inference paths and are skipped.
- **Members:** each imported member points at `https://<account>.openai.azure.com/openai/deployments/<deployment>` (on LiteLLM a row `azure/<deployment>`). Deployments of the same model with different names, such as `gpt-5.6-luna` and the DataZone `gpt-5.6-luna-dz`, can therefore share one pool.
- **How clients call it:**
  - APIM appends the operation path, so clients call `https://<apim>/<api-path>/chat/completions?api-version=…` with the header `Ocp-Apim-Subscription-Key`. The API the app creates for a pool is already shaped this way. If you bind your own API instead, it must expose `/chat/completions` (or a catch-all `/*`), not `/deployments/{id}/…`.
  - LiteLLM: `https://<gateway>/v1/chat/completions` (or `/v1/responses`) with `Authorization: Bearer <virtual key>` and the pool ID or an alias as `model`.
- **Auth:** members authenticate with the **gateway's managed identity**, because most accounts disable key auth. It needs **Cognitive Services OpenAI User** on each account; the setup wizard, the Deployments selection bar and the Test call tab check that and grant it if you may (**Grant access**), or give you an admin request with the exact `az role assignment create` command. On APIM the service's system-assigned identity must be on as well (**Turn on identity**), and an API you bind yourself must send the managed-identity token for `https://cognitiveservices.azure.com`.
- **Caching:** results are cached for 5 minutes. **Refresh** rescans, which takes about 15 s for 25 accounts.

### A LiteLLM gateway on Container Apps

What **Deploy a gateway** creates in the resource group you choose (`infra/gateway/litellm-aca.bicep`,
compiled into `backend/pool_manager/litellm/templates/litellm-aca.json`). Everything is tagged
`managed-by: ai-gateway-pool` and `ai-gateway: <name>`.

| Resource | Notes |
| --- | --- |
| Container App `<name>` | LiteLLM `v1.106.4` (pinned image `ghcr.io/berriai/litellm:v1.106.4`) on port 4000 behind external HTTPS ingress, plus an OpenTelemetry Collector sidecar that sends model-call spans to Application Insights. Its replicas, their size (1 vCPU and 2 GiB for a single replica, else 2 vCPU and 4 GiB, LiteLLM's guidance for one worker) and its scale rules come from the plan: a replica is added past the calls in flight each holds at the peak, or past 60 % of its one worker's core (27 % of a 2 vCPU replica and its sidecar). The ingress ends a request that sends nothing for 240 seconds. The master key, salt key, database URL and `config.yaml` are Container App secrets |
| Container Apps environment `cae-<name>` | Workload profiles environment with LiteLLM on its Consumption profile; logs to Log Analytics. You can add a dedicated profile with premium ingress later (idle timeout up to 30 minutes instead of 240 seconds); it needs at least two dedicated nodes |
| User-assigned identity `id-<name>` | What LiteLLM calls Foundry as (`AZURE_CLIENT_ID`); the setup wizard grants it *Cognitive Services OpenAI User* on member accounts. It has no other role |
| Azure Database for PostgreSQL Flexible Server | LiteLLM's model table, virtual keys and spend. Burstable B1ms for a single replica, else the General Purpose size (D2ds_v5 and up) the plan's connections need: Burstable runs out of CPU credits under sustained load. 32 GB. LiteLLM writes spend once a minute (through Redis at LiteLLM's threshold) and keeps at most 10 connections per replica. Public network access, limited to Azure services |
| Azure Managed Redis (more than one replica) | Balanced B0 to B5 by the peak, so replicas share cooldowns |
| Log Analytics workspace `log-<name>`, Application Insights `appi-<name>` | Container logs, and the traces behind the Metrics tab |

LiteLLM runs with the settings of its [production checklist](https://docs.litellm.ai/docs/proxy/prod):
one worker per replica, spend written once a minute (through Redis, `use_redis_transaction_buffer`,
from 1,000 requests a second or 10 replicas), failed calls kept out of the spend table
(`disable_error_logs`: they are in the logs and in the spans the Monitor tab reads), JSON logs at
`ERROR` with `LITELLM_MODE=PRODUCTION`, and `trusted_proxy_ranges` set to the private address
ranges, so its limit on failed Admin UI sign-ins counts each client's own address behind the
ingress. A gateway on AKS gets the same.

The gateway's secrets are generated once by the app when it deploys and are never rotated by it:
the salt key encrypts the member keys LiteLLM stores, so it must not change. That is also why a
gateway that already exists is never deployed over.

#### Before deploying, and when it fails anyway

A deployment creates several resources at once, and one that Azure refuses halfway used to leave
the others behind, billed. Now:

- **The region is checked first.** While you fill in the form, the app reads, without changing
  anything, whether the region can take each resource the gateway needs: that Azure offers each
  resource type there, PostgreSQL Flexible Server (the subscription's restrictions in the region,
  the server version, the plan's size and its vCore quota), room for one more
  Container Apps environment and, on AKS, the node size, its zones and the vCPU quota. It lists
  every problem at once, offers regions where everything fits, and **Deploy** stays off until the
  region works.
- **Azure validates the deployment before it starts.** Clicking **Deploy** asks ARM to validate
  the deployment exactly as it will be sent (the providers' own checks of sizes, names and
  policies). If it fails, nothing is created; an empty resource group the app made for it goes
  again.
- **A resource that fails anyway stops the deployment and its resources are deleted.** Azure can
  still run out of capacity in the minutes a deployment takes. With **If a resource fails, stop
  the deployment and delete what it created** ticked (the default), the app stops the deployment
  as soon as a resource fails, then deletes the resources it created: only those tagged as this
  gateway's, never the resource group. It keeps trying while Azure refuses a delete (a
  dependency still there, a resource still being created) and shows what it deleted and what
  Azure is still deleting. The deployment's record stays, with the error, and the same name can
  be deployed again once Azure has finished. This happens while the app follows the deployment,
  so if you closed it, it starts when you open the gateway step again. Untick the option to keep
  the resources for a look; **Delete what was created** then removes them by hand. On AKS this
  covers the Azure resources; an install in the cluster that fails keeps **Run the install
  again**.

**Deleting a gateway** (**Delete the gateway** in the gateway panel, or **Delete** in the gateway
list) is limited to gateways this app deployed, and needs the name typed again. It deletes the
resources tagged as that gateway's, starting with the Container App, and never the resource
group. There is **no soft-delete**: Azure keeps no copy, and the database with its pools, keys and
spend history is gone. A connected gateway is never deleted; **Disconnect** only forgets it and
its key.

### A LiteLLM gateway on AKS

What **Deploy on AKS** creates (`infra/gateway/litellm-aks.bicep`, compiled into
`backend/pool_manager/litellm/templates/litellm-aks.json`, then the install in
`litellm/gateway_template.py`'s `aks_bootstrap`). The Azure resources are tagged like a Container
Apps gateway's.

| Resource | Notes |
| --- | --- |
| AKS cluster `<name>` | Entra ID with Azure RBAC and no local accounts, OIDC issuer and workload identity, Azure CNI overlay, Azure Linux nodes. Their size and autoscaler range, the tier (Standard with zone redundancy), and the outbound IPs and SNAT ports per node come from the plan: every concurrent call to Foundry holds a port, and the default is 1,024 a node. The managed Gateway API with application routing's Istio implementation is the ingress (the managed NGINX ingress is retired). Patch versions and node images upgrade automatically |
| In the cluster, namespace `litellm` | LiteLLM's Helm chart (pinned `1.106.4`) with workload identity, 1 vCPU and 4 GiB per pod, as many as the plan says (scaled at 60 % CPU), an OpenTelemetry Collector sending model-call spans to Application Insights, and a Gateway on a public IP whose DNS name is `<name>.<region>.cloudapp.azure.com`. cert-manager (in `cert-manager`) gets its Let's Encrypt certificate and renews it. Plain HTTP only redirects to HTTPS |
| User-assigned identity `id-<name>` | Federated with LiteLLM's service account; what LiteLLM calls Foundry as. The setup wizard grants it *Cognitive Services OpenAI User* on member accounts |
| PostgreSQL, Redis (more than one pod), Log Analytics, Application Insights | As for Container Apps |
| Role assignment | *Azure Kubernetes Service RBAC Cluster Admin* for you, on the cluster |

The app made the secrets (master key, salt key, Postgres password) and kept them on your machine
until the install finished; they reached the cluster only as Kubernetes secrets, through Run
Command's files, and the install never prints them. Afterwards the app keeps only the master key,
encrypted, like for a connected gateway.

**You maintain the cluster.** The app manages pools, aliases and keys in LiteLLM, and changes the
cluster only when you resize it from the gateway panel (**Resize for new traffic**: the node
range, and LiteLLM's pods through `helm upgrade --reuse-values` in Run Command, after a warning
and your confirmation). The gateway panel lists the commands, ready to copy, and links to the
cluster in the portal:

```sh
az aks get-credentials -g <group> -n <name>                 # kubectl access (Entra ID sign-in)
kubectl -n litellm get pods,gateway,certificate             # LiteLLM, HTTPS and the certificate
kubectl -n litellm get secret litellm-masterkey -o jsonpath='{.data.masterkey}' | base64 -d
helm upgrade litellm oci://ghcr.io/berriai/litellm-helm -n litellm --reuse-values \
  --version <new-version> --set image.tag=v<new-version>  # a newer LiteLLM
az aks get-upgrades -g <group> -n <name> -o table && az aks upgrade -g <group> -n <name> --kubernetes-version <version>
```

To use your own domain later, point it at the Gateway and change the `Certificate`, the Gateway's
listener `hostname` and the HTTPRoutes in namespace `litellm`, then reconnect the app with the
new URL. **Delete the gateway** works as for Container Apps: it deletes the cluster (Azure removes
its node resource group with it) and the other tagged resources, never the resource group, and
can't be undone.
