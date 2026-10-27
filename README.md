# AI Gateway Pool Manager (LiteLLM)

**English** | [简体中文](README.zh-CN.md)

A standalone web app that gives you an **AI gateway with failover** in front of your Azure OpenAI /
Foundry model deployments, without having to learn LiteLLM or Foundry first. It deploys (or
connects) a [LiteLLM](https://github.com/BerriAI/litellm) gateway, finds your model deployments,
and groups deployments of one model into a **pool**. Clients call the pool by one model name;
the gateway spreads requests across the members (failover, balanced or weighted), benches a
member that keeps failing (cooldown), retries on another member, and keeps Responses API
follow-ups on the deployment that answered first.

![A pool's Routing tab: the client calls the LiteLLM gateway with one model name, and the pool spreads requests across Foundry deployments in several regions, with cooldown and retries](docs/images/pool-routing.png)

*One endpoint and one model name for clients, several model deployments behind it: the Routing
tab shows how the gateway routes traffic through the pool, and lets you click a deployment to see
what happens when it fails or is throttled.*

```
apim-backend-pool/
├── backend/    FastAPI app (pool_manager) + tests; serves the built frontend
├── frontend/   React/Vite UI (built into backend/pool_manager/static)
├── infra/      Bicep: the gateway on Container Apps (infra/gateway), and the App Service hosting
│               of this app with Easy Auth and least-privilege roles
├── scripts/    Local start scripts, the doctor, the demo server, release packaging and the
│               gateway template build
├── docs/       Screenshots for this README
├── tui/        Terminal UI: still the APIM version, not usable with the gateway yet
├── deploy/     Public multi-tenant app: not available with the gateway yet
└── azure.yaml  azd project definition (App Service hosting)
```

> [!NOTE]
> This branch replaces the earlier Azure API Management (APIM) version. Two parts haven't been
> ported yet: the [terminal UI](#terminal-ui) and the [public multi-tenant app](#public-app).

New here? [Get started](#get-started) takes about half an hour, most of it waiting for Azure to
deploy the gateway, and needs only Python.

## Get started

This guide runs the app on your own computer. You install Python and nothing else. You don't
need the Azure CLI, Node.js, Docker, a config file, or any LiteLLM experience; the app checks
what's missing and fixes it with you. (To give your team a shared URL instead, see
[Deploy to Azure](#deploy-to-azure).)

**What you need**

- **Python 3.11 or newer.** `winget install -e --id Python.Python.3.13` on Windows,
  `brew install python@3.13` on macOS, or see [Prerequisites per OS](#prerequisites-per-os).
- **An Azure account** that can deploy resources in a subscription (*Contributor*), or read an
  existing gateway, and read your Azure OpenAI / Foundry resources (*Reader*).
- **Some Azure OpenAI / Foundry model deployments**, ideally of the same model in more than one
  region. If you don't have any, the app can create them (step 4).

### 1. Download and start

Download `apim-backend-pool-<version>.zip` from
[Releases](https://github.com/guming3d/apim-backend-pool-manager/releases), unzip it, and run
the start script in the unzipped folder:

```powershell
scripts\start-local.cmd        # Windows
```

```sh
scripts/start-local.sh         # macOS, Linux, WSL
```

The first run checks your machine and installs what the app needs into a local `.venv`, which
takes about a minute. Then open **http://127.0.0.1:8000**. Keep the window open while you use
the app, and press Ctrl+C to stop it.

### 2. Sign in to Azure

Click **Sign in with browser** and sign in with your Azure account. On a machine without a
browser (SSH, WSL, a server), click **Use a code instead**. The app remembers the sign-in in your
OS's encrypted store, so you do this once.

![Setup page, step 1 of Sign in, AI gateway, Permissions and Gateway → Foundry: Sign in with browser, or Use a code instead](docs/images/onboarding-1-sign-in.png)

### 3. Choose, deploy or connect a gateway

The app lists the LiteLLM gateways on Azure Container Apps in each subscription you can access and
says whether you may manage them. Click **Use this gateway**.

![Setup page, step 2: the LiteLLM gateways in a subscription, each with Use this gateway, plus Deploy a gateway and Connect a gateway](docs/images/onboarding-2-choose-gateway.png)

No gateway yet? Click **Deploy a gateway**. The app first asks **Where should the gateway run?**
(see [Container Apps or AKS?](#container-apps-or-aks) below) and recommends one:

- **Deploy on Container Apps** opens a form with everything filled in: a unique name, a resource
  group, a region and the size (**Dev / trial** or **Production**), with a list-price estimate.
  Click **Deploy**. The app deploys LiteLLM, PostgreSQL, a managed identity and Application
  Insights with your own Azure rights, and follows the deployment, which usually takes 5–15
  minutes (PostgreSQL is the slowest). You can close the app meanwhile; setup picks it up again.
  Azure bills the resources from the moment they're created.
- **Deploy on AKS** opens the same form for a **new AKS cluster**. Click **Deploy**; the app
  then works in three phases, shown as it goes (usually 15–25 minutes in all):
  1. **Azure resources**: the cluster (Entra ID with Azure RBAC, workload identity, the managed
     Gateway API), PostgreSQL, the gateway's identity, Application Insights and, for production,
     Redis. You get *Azure Kubernetes Service RBAC Cluster Admin* on the cluster, so deploying
     needs Owner or User Access Administrator on the resource group.
  2. **Install in the cluster**: through AKS Run Command (no kubectl or Helm needed on your
     machine) the app installs cert-manager, an OpenTelemetry Collector, LiteLLM's Helm chart and
     an HTTPS Gateway.
  3. **HTTPS certificate**: Let's Encrypt issues a certificate for
     `https://<name>.<region>.cloudapp.azure.com`; once LiteLLM answers there, the app connects
     to it like any gateway.

  Keep the page open while it runs (the progress drives the install on; if you close it, come
  back to the **AI gateway** step). If the install fails, its output is shown with **Run the
  install again**. After that the cluster is **yours to maintain**; see
  [A gateway on AKS](#a-gateway-on-aks).
- **Use my own cluster (files)** is for a cluster you already have: it asks for the cluster, its
  resource group and a namespace, and generates a `values.yaml`, a `deploy.sh` and a `README.md`
  (Helm chart, workload identity, PostgreSQL Flexible Server). The cluster needs the OIDC issuer
  and workload identity enabled. Run the script, for example in Azure Cloud Shell, then click
  **Deployed, connect it**. The script exposes LiteLLM on plain HTTP on a public IP, and
  **Connect** accepts only HTTPS URLs (plain HTTP only for localhost, so the master key never
  crosses the network unencrypted). To try it from the machine running the app, keep
  `kubectl -n <namespace> port-forward svc/litellm 4000:4000` running and connect to
  `http://localhost:4000`; for real traffic, put HTTPS in front and connect to its HTTPS URL.

**Connect a gateway** works for any LiteLLM proxy with a database (`STORE_MODEL_IN_DB`): enter its
HTTPS URL, its master key and, optionally, the object ID of the managed identity it calls Foundry
as and its Application Insights resource ID. The app checks both against the gateway and keeps the
master key encrypted on this machine, never in a file in plain text.

![Where should the gateway run? A few questions, Azure Container Apps recommended, and the AKS option](docs/images/onboarding-2b-deploy-gateway.png)

A two-minute guided tour opens next. Take it or skip it; **Tour** in the top bar replays it.

#### Container Apps or AKS?

The questionnaire asks what the gateway is for, the peak tokens per minute, the average tokens per
request, and three yes/no questions. It recommends:

| | Azure Container Apps (the default) | Azure Kubernetes Service (AKS) |
| --- | --- | --- |
| When | Most cases: trials, and production traffic up to about 250 requests a second (what the production profile's 5 replicas serve) | You already run an AKS cluster and have people for it; self-hosted (GPU) models should run next to the gateway; or the peak is over about 250 requests a second (peak TPM ÷ 60 ÷ tokens per request) |
| How | Deployed by this app in one step, with no cluster to run | A new cluster deployed and set up by this app (then maintained by you), or the generated files for a cluster you already have |
| Size | **Dev / trial**: one replica, no Redis. **Production**: 2–5 replicas scaling with load, sharing cooldowns through Redis | **Dev / trial**: one Standard_D4ds_v5 node, Free tier, no Redis. **Production**: 3–6 nodes across zones, Standard tier (uptime SLA), 2–6 LiteLLM replicas sharing cooldowns through Redis |
| List price | Dev about **$46–115 a month**; production about **$171–865 a month** | New cluster: dev about **$190–230 a month**; production about **$670–1,200 a month**. Your own cluster: its cost, plus PostgreSQL |

The prices are **retail list-price estimates** (USD, East US 2, from quiet to busy) for what each
profile deploys: Container Apps replicas, PostgreSQL and, for production, Redis. They leave out
free grants, log ingestion and discounts, and of course your model usage, which Foundry bills as
usual. Once the gateway exists, its panel shows this month's actual spend and the list price for
its region. For production the recommendation also says how many replicas to plan for (about one
per 50 requests a second at the peak).

A gateway that must not be reachable from the internet works on both, but private networking
isn't automated here: on Container Apps, put the environment in a virtual network afterwards (or
adapt the template); on AKS, use an internal load balancer or private ingress.

### 4. Pick the deployments to pool

Open the **Deployments** tab. It lists every Azure OpenAI / Foundry deployment in the gateway's
subscription, grouped by model. (Deployments in other subscriptions: set
`POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS` in `backend/.env`.) Click **Create pool from this model**
on the model you want. To pool only some of a model's deployments, tick them and choose
**Import & create pool**.

![Deployments tab: gpt-5.6-luna deployed in three regions, with Create pool from this model](docs/images/onboarding-3-deployments.png)

Nothing deployed yet? **Deploy a model** creates a deployment in one of your Foundry resources,
then lists it here.

### 5. Publish the pool

The pool opens with everything filled in: an ID (here `pool-gpt-5-6-luna`), which is also the
model name clients send, the members, and the cooldown, retry and timeout settings. The defaults
work. **Failover** sends all traffic to the primary and switches to a standby when it fails or
runs out of quota; choose **Balanced** to spread traffic over every member instead, or
**Weighted** to set the shares.

![Add pool, pre-filled in Failover mode with three members and the cooldown settings](docs/images/onboarding-4-create-pool.png)

Click **Publish to the gateway**. The app lists exactly what it will write. Click **Publish to
the gateway** again to confirm. Changes take effect on the gateway immediately.

![Review and publish: the new pool and its three members](docs/images/onboarding-5-publish.png)

### 6. Let the gateway call your models, and get a key

The pool opens on its **Test call** tab. It shows the gateway's URL and the pool's model names
(**Add an alias** lets clients that already send another name, such as `gpt-5.6-luna`, reach the
pool), and walks you through the rest:

1. **Grant all** gives the gateway's managed identity the *Cognitive Services OpenAI User* role on
   each Foundry resource, so the gateway calls your models without API keys. That takes a role
   allowed to assign roles on those resources (*Owner*, *User Access Administrator* or *Role
   Based Access Control Administrator*). Without one, **Permission check** in the top bar shows a
   ready-to-send request for your Azure admin.
2. **Create a test key** creates a LiteLLM virtual key that can call only this pool's model names,
   and fills it in. LiteLLM keeps only a hash of it, so copy it now if you want to keep it: it's
   shown this once.

![Test call tab: the endpoint and model names, Before you call, and Get a virtual key with Create a test key](docs/images/onboarding-6-grant-and-key.png)

### 7. Send a test request

Click **Send request**. The request goes through the gateway and the pool, and you get the
model's answer with the member that served it (and how many times LiteLLM tried other members
first). If you only just granted access, Azure can take up to 10 minutes to apply the role; until
then the page explains the error, so try again shortly.

![Try it here: 200, the call succeeded, answered by the pool's primary member pool-aoai-westus3-gpt-5-6-luna-dz](docs/images/onboarding-7-test-call.png)

Your apps call the pool like any OpenAI endpoint: base URL `<gateway>/v1`, the virtual key as the
API key, and the pool ID (or an alias) as the model. No `api-version` is needed. **Send a request**,
further down, has the code for curl, Python, JavaScript and PowerShell with your URL filled in.
In Python:

```python
from openai import OpenAI

client = OpenAI(base_url="https://<your-gateway>/v1", api_key="<your-virtual-key>")

response = client.chat.completions.create(
    model="pool-gpt-5-6-luna",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

### Load-test the pool (optional)

**Load test**, under Try it here, simulates several users calling the pool at once: each sends a
request, waits for the answer, then sends the next. Pick **Smoke** (2 users, 30 s), **Typical**
(10 users, 2 min) or **Stress** (30 users, 3 min), or set the users, duration, ramp-up, a rate cap,
the prompt size, `max_tokens` and streaming under **Advanced settings**. The page estimates the
requests and tokens first; every request is billed by Foundry like real traffic and counts against
the deployments' rate limits. While it runs you see throughput, the success rate, latency p50/p95/
p99, time to first token, 429s and failovers, a chart per second and which members answered; at
the end a plain verdict (for example that the pool needs more capacity because every member
returned 429 at once), **Export JSON / CSV** and the same test as a terminal command. A run is
limited to 50 users, 10 minutes and 5,000 requests, one at a time, and only owners can start one.

From a terminal, against this or any OpenAI-compatible endpoint (the key stays in an environment
variable, out of the shell history):

```powershell
$env:LITELLM_KEY = "<your-virtual-key>"
backend\.venv\Scripts\python scripts\benchmark.py --url https://<your-gateway>/v1 --model pool-gpt-5-6-luna --preset typical
#   --users 20 --duration 120 --ramp-up 10 --rate 15 --prompt long --max-tokens 256 --no-stream
#   --json result.json --csv requests.csv        (macOS/Linux: backend/.venv/bin/python, export LITELLM_KEY=...)
```

It prints a status line every second and the report at the end; Ctrl+C stops it (requests already
sent finish) and still prints the report. `--i-know` lifts the limits for the terminal only.

### 8. Watch it work

The **Metrics** tab shows requests, availability, the 429s that reached clients (and how many a
failover saved) and latency, plus each member's share of the traffic and tokens. **Routing**
simulates a member failing, so you can see the failover before it happens for real.

![Metrics tab: requests, availability, 429s, latency and each member's traffic share](docs/images/onboarding-8-metrics.png)

That's it. Everything else (more pools, weights, aliases, custom endpoints, the LiteLLM config
view, cleaning up) is in the same UI; [What it does](#what-it-does) lists it all.

> [!CAUTION]
> The app acts on Azure as **you**, and on your **real** gateway with its master key. Changes are
> live for every client at once.

**If something gets in the way**

- **Permission check** in the top bar re-runs the setup checks. Whatever your own Azure rights
  can't fix becomes a request to send your admin: the role, the scope, an `az` command and a
  portal link.
- `python scripts/doctor.py` explains what's wrong with your machine or network (Python version,
  proxy, company TLS certificates, DNS) and how to fix it on your OS.
- Behind a proxy or on a locked-down network? See
  [Networks, proxies and certificates](#networks-proxies-and-certificates).

**Try it without Azure.** From a clone, `scripts/demo_server.py` runs the same UI against an
in-memory Azure and LiteLLM gateway with sample deployments, which is how the screenshots above
were made. Nothing reaches Azure. It needs the backend's virtual environment (the start script
creates it) and the built UI (Node.js 22+):

```sh
cd frontend && npm ci && npm run build && cd ..
backend/.venv/Scripts/python scripts/demo_server.py    # bin/python on macOS and Linux; http://127.0.0.1:8010
```

It starts signed out, like a first run. `POST /demo/stage/{sign-in,ready,pool}` jumps to a point
in onboarding (`pool` is a pool already in use, with an hour of metrics).

## What it does

| Area | What it does |
| --- | --- |
| **Setup wizard** | Four steps: **Sign in**, **AI gateway**, **Permissions** and **Gateway → Foundry**. It checks sign-in, the gateway (reachable, database connected, master key valid, managed identity), your access to it and the gateway's access to Foundry. It fixes what your Azure rights allow and writes a ready-to-send admin request for the rest. Locally it also chooses, deploys, connects, disconnects or deletes the gateway. |
| **Gateway choice** | Recommends Container Apps or AKS from a few answers. Deploys on Container Apps in one step, or creates a new AKS cluster and installs LiteLLM with HTTPS on it, or generates the files for a cluster you already have. |
| **Guided tour** | A two-minute walkthrough for people new to AI gateways. It runs once after setup; **Tour** in the top bar replays it. |
| **Deployment discovery** | Lists the Azure OpenAI / Foundry deployments in your subscriptions, grouped by model, and imports the ones you select into a new or existing pool. |
| **Deploy a model** | Creates a pay-per-token OpenAI model deployment in a Foundry account, using your own Azure rights, so you don't need the Foundry portal. |
| **Pools** | Create and edit pools of 2–30 members in **Failover**, **Balanced** or **Weighted** mode. A pool is a LiteLLM model group; its ID is the model name clients send. |
| **Members** | Edited inside their pool. Besides Foundry deployments, **Add a custom endpoint** adds another Azure OpenAI resource or any OpenAI-compatible API, with no auth, an API key (stored by LiteLLM as an encrypted credential) or the gateway's managed identity. |
| **Cooldown, retries, timeout** | Pool settings, written to every member: how many failures in a minute bench a member and for how long, how often a failed request is retried on another member, and the timeout per attempt. |
| **Model names and aliases** | The pool ID always works; aliases point other model names at the pool, so existing clients don't have to change. |
| **LiteLLM config** | **View LiteLLM config** shows the pool as a LiteLLM `config.yaml` would write it (secrets left out), with a diff before you publish, and **Export YAML**. |
| **Routing preview** | Shows how the gateway routes traffic through the pool and simulates failures, cooldowns and recovery. |
| **Test call** | Creates the pool's test virtual key, sends a test request from the page, shows which member answered, and gives ready-to-copy client code. |
| **Load test** | Simulated users calling the pool, from the Test call tab or `scripts/benchmark.py`: throughput, latency and time-to-first-token percentiles, 429s, failovers and the members that answered, with a verdict and exports. |
| **Metrics** | Requests, availability, 429s and latency for the pool from the gateway's OpenTelemetry traces in Application Insights, plus traffic share, tokens and latency per member from Azure Monitor. |
| **Cost** | This month's spend for the gateway's resources and their list price. Open it from the gateway name in the top bar (**Manage the gateway**), which also links LiteLLM's own admin UI. |
| **Same model** | Refuses to save a pool whose Foundry members serve different models. The model is read from Azure, not guessed from the deployment name. |
| **Removing** | Removes a pool (with its aliases and test key), or everything this app created on the gateway, after showing exactly what will go. Locally it can also delete a gateway it deployed. |
| **Safety** | The app only changes and deletes what it created, and only after you have seen it. Pools added elsewhere (LiteLLM's admin UI or config) are shown read-only, as are pools with a protected ID prefix (`POOL_MANAGER_PROTECTED_PREFIXES`). |

Defaults for a new pool:
- **Cooldown:** a member that fails once within a minute (429s count) sits out 60 s; its traffic
  goes to the other members, or in failover to the next priority.
- **Retries:** 1 further attempt on another member.
- **Timeout per attempt:** 120 s.

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
- With more than one replica, cooldowns are only shared through Redis, which the production
  profile on Container Apps includes.

### Discovered deployments

- **What is listed:** only `Succeeded` deployments in **OpenAI format** on `OpenAI` or `AIServices` accounts. Partner models (Anthropic, DeepSeek, xAI, …) use other inference paths and are skipped.
- **Members:** each imported member points at `https://<account>.openai.azure.com/openai/deployments/<deployment>`, which becomes a LiteLLM row `azure/<deployment>`. Deployments of the same model with different names, such as `gpt-5.6-luna` and the DataZone `gpt-5.6-luna-dz`, can therefore share one pool.
- **How clients call it:** `https://<gateway>/v1/chat/completions` (or `/v1/responses`) with `Authorization: Bearer <virtual key>` and the pool ID or an alias as `model`.
- **Auth:** members authenticate with the **gateway's managed identity**, because most accounts disable key auth. It needs **Cognitive Services OpenAI User** on each account; the setup wizard, the Deployments selection bar and the Test call tab check that and grant it if you may (**Grant access**), or give you an admin request with the exact `az role assignment create` command.
- **Caching:** results are cached for 5 minutes. **Refresh** rescans, which takes about 15 s for 25 accounts.

### A gateway on Container Apps

What **Deploy a gateway** creates in the resource group you choose (`infra/gateway/litellm-aca.bicep`,
compiled into `backend/pool_manager/templates/litellm-aca.json`). Everything is tagged
`managed-by: ai-gateway-pool` and `ai-gateway: <name>`.

| Resource | Notes |
| --- | --- |
| Container App `<name>` | LiteLLM `v1.106.4` (pinned image `ghcr.io/berriai/litellm:v1.106.4`) on port 4000 behind external HTTPS ingress, plus an OpenTelemetry Collector sidecar that sends model-call spans to Application Insights. Dev: 1 replica; production: 2–5 replicas. The master key, salt key, database URL and `config.yaml` are Container App secrets |
| Container Apps environment `cae-<name>` | Consumption plan, logs to Log Analytics |
| User-assigned identity `id-<name>` | What LiteLLM calls Foundry as (`AZURE_CLIENT_ID`); the setup wizard grants it *Cognitive Services OpenAI User* on member accounts. It has no other role |
| Azure Database for PostgreSQL Flexible Server | LiteLLM's model table, virtual keys and spend. Burstable B1ms (dev) or B2s (production), 32 GB. Public network access, limited to Azure services |
| Azure Managed Redis (production only) | Balanced B0, so replicas share cooldowns |
| Log Analytics workspace `log-<name>`, Application Insights `appi-<name>` | Container logs, and the traces behind the Metrics tab |

The gateway's secrets are generated once by the app when it deploys and are never rotated by it:
the salt key encrypts the member keys LiteLLM stores, so it must not change. That is also why a
gateway that already exists is never deployed over.

**Deleting a gateway** (**Delete the gateway** in the gateway panel, or **Delete** in the gateway
list) is limited to gateways this app deployed, and needs the name typed again. It deletes the
resources tagged as that gateway's, starting with the Container App, and never the resource
group. There is **no soft-delete**: Azure keeps no copy, and the database with its pools, keys and
spend history is gone. A connected gateway is never deleted; **Disconnect** only forgets it and
its key.

### A gateway on AKS

What **Deploy on AKS** creates (`infra/gateway/litellm-aks.bicep`, compiled into
`backend/pool_manager/templates/litellm-aks.json`, then the install in
`gateway_template.aks_bootstrap`). The Azure resources are tagged like a Container Apps gateway's.

| Resource | Notes |
| --- | --- |
| AKS cluster `<name>` | Entra ID with Azure RBAC and no local accounts, OIDC issuer and workload identity, Azure CNI overlay, Azure Linux nodes (Standard_D4ds_v5). The managed Gateway API with application routing's Istio implementation is the ingress (the managed NGINX ingress is retired). Patch versions and node images upgrade automatically |
| In the cluster, namespace `litellm` | LiteLLM's Helm chart (pinned `1.106.4`) with workload identity, an OpenTelemetry Collector sending model-call spans to Application Insights, and a Gateway on a public IP whose DNS name is `<name>.<region>.cloudapp.azure.com`. cert-manager (in `cert-manager`) gets its Let's Encrypt certificate and renews it. Plain HTTP only redirects to HTTPS |
| User-assigned identity `id-<name>` | Federated with LiteLLM's service account; what LiteLLM calls Foundry as. The setup wizard grants it *Cognitive Services OpenAI User* on member accounts |
| PostgreSQL, Redis (production), Log Analytics, Application Insights | As for Container Apps |
| Role assignment | *Azure Kubernetes Service RBAC Cluster Admin* for you, on the cluster |

The app made the secrets (master key, salt key, Postgres password) and kept them on your machine
until the install finished; they reached the cluster only as Kubernetes secrets, through Run
Command's files, and the install never prints them. Afterwards the app keeps only the master key,
encrypted, like for a connected gateway.

**You maintain the cluster.** The app manages pools, aliases and keys in LiteLLM, and nothing in
the cluster. The gateway panel lists the commands, ready to copy, and links to the cluster in the
portal:

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

## Deploy to Azure

This hosts the manager itself on App Service with Entra sign-in (Easy Auth), for a team. It manages
an **existing** gateway: Bicep never creates or changes the gateway, because its secrets are
generated once by the app and a re-provision must not rotate them. So deploy the gateway first
from the local app ([Get started](#get-started), steps 1–3), or on AKS.

Prerequisites: [azd](https://aka.ms/azd), Azure CLI and Node.js 20.19+ or 22.12+. You need
permission to create custom roles and role assignments (Owner or User Access Administrator) on
the gateway's resource group, and on each subscription scanned for deployments.

**1. Create the Entra app registration for sign-in** (once):

```sh
az ad app create --display-name "AI Gateway Pool Manager" \
  --sign-in-audience AzureADMyOrg --enable-id-token-issuance true
# Note the appId. Add the redirect URI after the first provision (step 3).
```

**2. Configure and provision:**

```sh
cd apim-backend-pool
azd env new pool-manager
# A gateway on Container Apps (its Container App name)...
azd env set GATEWAY_NAME <gateway-name>
azd env set GATEWAY_RESOURCE_GROUP <gateway-resource-group>   # default: the azd resource group
# azd env set GATEWAY_SUBSCRIPTION_ID <id>                     # only if it is in another subscription
# ...or a gateway elsewhere (AKS, any LiteLLM proxy with a database):
# azd env set GATEWAY_URL https://litellm.contoso.com
# azd env set GATEWAY_MASTER_KEY sk-...
# azd env set GATEWAY_PRINCIPAL_ID <object-id>        # optional: the identity it calls Foundry as
# azd env set GATEWAY_APP_INSIGHTS_ID <resource-id>   # optional: where its OpenTelemetry goes
azd env set ENTRA_CLIENT_ID <appId>
azd env set POOL_OWNERS "you@contoso.com"  # comma-separated UPNs or object IDs who may change things
azd provision --preview                    # review the what-if first
azd up
```

**3. Register the redirect URI** printed as `AUTH_REDIRECT_URI`:

```sh
az ad app update --id <appId> --web-redirect-uris "$(azd env get-value AUTH_REDIRECT_URI)"
```

Open `WEB_APP_URL` and sign in. Users listed in `POOL_OWNERS` may make changes and see keys. With
delegated Azure access (below), so may users whose own Azure roles already let them read the
gateway's master key (a Container App secret), since they could change the gateway directly
anyway. For a gateway outside Container Apps, only owners can. Everyone else is read-only. To
restrict who can sign in at all, set *Assignment required* on the app's enterprise application.

### Delegated Azure access (optional, recommended)

The app identity reads the gateway and never gets permission to change role assignments. To let
the setup wizard **fix** access problems (grant the gateway's identity a role on Foundry) and
check what each user may do, Easy Auth also gets an Azure Resource Manager token for the signed-in
user. Fixes then run with that user's own Azure rights and show up as that user in the activity
log. Without it, the wizard still checks everything and writes the admin requests, and the cost
panel can't read actual spend.

Set up the app registration first. Turning the parameter on without these steps breaks sign-in:

```sh
APP_ID=<appId>
# Delegated permission "Azure Service Management / user_impersonation", then consent to it.
az ad app permission add --id $APP_ID --api 797f4846-ba00-4fd7-ba43-dac1f8f63013 \
  --api-permissions 41094075-9dad-400e-a0bd-54e686782033=Scope
az ad app permission admin-consent --id $APP_ID   # or let each user consent on first sign-in

azd env set ENABLE_DELEGATED_AZURE_ACCESS true
azd provision

# Easy Auth signs in with a managed identity instead of a client secret (federated credential).
az ad app federated-credential create --id $APP_ID --parameters "{
  \"name\": \"pool-manager-easy-auth\",
  \"issuer\": \"$(azd env get-value AUTH_FIC_ISSUER)\",
  \"subject\": \"$(azd env get-value AUTH_FIC_SUBJECT)\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"
```

Users who signed in before then need to sign in again; the wizard shows an **Allow Azure access**
button for that.

### What gets deployed

| Resource | Notes |
| --- | --- |
| Linux App Service plan (B1) + Web App (Python 3.12) | HTTPS only, TLS 1.2, FTP/SCM basic auth disabled, system-assigned identity. With `GATEWAY_URL`, the gateway's master key is an app setting |
| User-assigned identity `id-pool-auth-*` (only with delegated Azure access) | Federated credential for Easy Auth instead of a client secret. It has no Azure roles |
| App Service Authentication (Easy Auth) | Entra sign-in is required on every path except `/health` |
| Custom role *AI Gateway Pool Manager* on the gateway's Container App and its Application Insights component (Container Apps gateways only) | `Microsoft.App/containerApps/read` and `listSecrets/action` (the gateway's URL, identity and master key; every pool change then goes through LiteLLM's admin API), `Insights/components/read` and `Insights/logs/*/read` for the Metrics tab, and `Authorization/permissions/read` so setup can check the app's own access. It can't change the Container App, its database or anything else |
| Custom role *AI Gateway Pool Deployment Reader* per discovery subscription | `CognitiveServices/accounts/read` and `accounts/deployments/read`, `Insights/metrics/read` for the members' metrics, plus read-only `Authorization/permissions`, `roleAssignments` and `roleDefinitions` so setup can check the gateway's role on each account. It has no `listKeys` and cannot change roles. Turn it off with the Bicep parameter `enableDeploymentDiscovery=false`, and choose subscriptions with `discoverySubscriptionIds` (the default is the gateway's subscription) |

The gateway's own identity needs *Cognitive Services OpenAI User* (or any role that allows
inference) on each member account. The setup wizard checks this and, with delegated Azure access,
grants it.

### Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `POOL_MANAGER_SUBSCRIPTION_ID` / `_RESOURCE_GROUP` / `_GATEWAY_NAME` | required when deployed (or `_GATEWAY_URL`) | A gateway on Container Apps. Locally, leave them out to choose, deploy or connect one on the setup page; set all three to fix it |
| `POOL_MANAGER_GATEWAY_URL` | empty | A gateway elsewhere (AKS, any LiteLLM proxy with a database), with `_GATEWAY_NAME` as its display name. `_SUBSCRIPTION_ID` is then only where deployments are searched by default |
| `POOL_MANAGER_GATEWAY_MASTER_KEY` | empty | That gateway's master key. Required when deployed; locally the setup page can keep it encrypted instead |
| `POOL_MANAGER_GATEWAY_PRINCIPAL_ID` | empty | That gateway's managed identity (object ID), so setup can check and grant its Foundry access |
| `POOL_MANAGER_GATEWAY_APP_INSIGHTS_ID` | empty | The Application Insights component its OpenTelemetry goes to, for the Metrics tab |
| `POOL_MANAGER_TENANT_ID` | from the setup page | Tenant of the subscription, so local tokens are requested there |
| `POOL_MANAGER_OWNERS` | empty | Who may make changes |
| `POOL_MANAGER_RBAC_WRITERS` | `true` | Also let users make changes whose own Azure roles let them read the gateway's master key. Needs delegated Azure access |
| `POOL_MANAGER_DELEGATED_ACCESS` | `false` | Set by `enableDelegatedAzureAccess`: Easy Auth provides the user's ARM token, so setup can fix access |
| `POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS` | the gateway's subscription | Comma-separated subscriptions scanned for deployments |
| `POOL_MANAGER_PROTECTED_PREFIXES` | empty | Comma-separated pool ID prefixes shown read-only, for pools another tool manages (e.g. `finops-`) |
| `POOL_MANAGER_ID_PREFIX` | `pool-` | Prefix for new pools, members and test keys |
| `POOL_MANAGER_AUTH_MODE` | `easy_auth` | `disabled` is for local development; the app refuses to start that way on App Service. `entra` (the public app) isn't available with the gateway yet |
| `POOL_MANAGER_SYSTEM_TRUST` | `true` | Locally, verify TLS against the OS certificate store (company CAs behind TLS inspection) |
| `AZURE_CLIENT_ID` | — | Use a user-assigned identity instead of system-assigned |

## Public app

The public multi-tenant website on Container Apps (`deploy/container-apps/`,
`POOL_MANAGER_AUTH_MODE=entra`), where anyone signs in with their own Microsoft account, is **not
available with the LiteLLM gateway yet**: each user would bring their own gateway and master key,
which isn't built. The backend refuses to start in that mode. Use the app locally or
[deploy it to App Service](#deploy-to-azure) for your team.

## Run it locally

Locally the app runs on your machine and acts on Azure as **you**. All it needs is
**Python 3.11+**. Node.js is only needed to work on the browser UI, and the Azure CLI is optional.

**Quickest: the release package.** Download `apim-backend-pool-<version>.zip` from
[Releases](https://github.com/guming3d/apim-backend-pool-manager/releases). It has the browser
UI and the gateway template built in, so it needs no Node.js and no git. Unzip it, then:

```powershell
scripts\start-local.cmd                 # Windows; works whatever the PowerShell execution policy
```

```sh
scripts/start-local.sh                  # macOS, Linux, WSL
```

Open http://127.0.0.1:8000. The setup page signs you in, then lets you choose, deploy or connect
a gateway (the choice is saved in `backend/.pool-manager.json`, never with a master key; a
connected gateway's key is kept OS-encrypted in `backend/.pool-manager-gateways.bin`). To pin a
gateway instead, set `POOL_MANAGER_SUBSCRIPTION_ID`, `_RESOURCE_GROUP` and `_GATEWAY_NAME` (or
`_GATEWAY_NAME` and `_GATEWAY_URL`) in `backend/.env`; `backend/.env.example` shows both.

**From a clone**, the same scripts run the Vite dev server on `:5174` when Node.js 22+ is
installed. Without Node.js they serve the UI from `backend/pool_manager/static`, if it has been
built (`-Prebuilt` / `--prebuilt` forces that).

```powershell
.\scripts\start-local.ps1               # -Prebuilt -BackendPort 8001 -FrontendPort 5175 -SkipInstall -SkipChecks
```

```sh
scripts/start-local.sh                  # --prebuilt --backend-port 8001 --frontend-port 5175 --skip-install --skip-checks
```

The scripts first run `scripts/doctor.py --brief`. It uses only the standard library, so it runs
before anything is installed, even on an older Python, and says how to fix what it finds on this
OS. Then they install dependencies only when they changed, and run until Ctrl+C. Run
`python scripts/doctor.py` yourself for the full report.

### Prerequisites per OS

| | Python 3.11+ | Node.js 22+ (UI development only) | Azure CLI (optional) |
| --- | --- | --- | --- |
| Windows | `winget install -e --id Python.Python.3.13` | `winget install -e --id OpenJS.NodeJS.LTS` | `winget install -e --id Microsoft.AzureCLI` |
| macOS | `brew install python@3.13` (macOS ships 3.9) | `brew install node` | `brew install azure-cli` |
| Ubuntu, Debian | `sudo apt install python3 python3-venv`; on Ubuntu 22.04 / Debian 11 `python3.11 python3.11-venv` | [nodejs.org](https://nodejs.org/en/download) (apt's is older) | `curl -sL https://aka.ms/InstallAzureCLIDeb \| sudo bash` |
| Fedora, RHEL | `sudo dnf install python3.12` | [nodejs.org](https://nodejs.org/en/download) | [Install guide](https://learn.microsoft.com/cli/azure/install-azure-cli) |

On Windows, if `python` opens the Microsoft Store, turn the alias off in *Settings > Apps >
Advanced app settings > App execution aliases*. The scripts look for `python3.13` … `python3.11`
before `python3`, so a newer Python installed alongside the system one is found.

### Signing in

The app tries, in order: a sign-in made on its setup page, then the Azure CLI (`az login`), the
Azure Developer CLI (`azd auth login`) and Azure PowerShell (`Connect-AzAccount`). A tool that
isn't installed or signed in is skipped for a minute rather than retried on every request.

- **Sign in with browser** opens the Microsoft sign-in page on this machine.
- **Use a code instead** is for a machine with no browser: SSH, Linux servers, containers, or WSL
  when the browser doesn't open. Enter the code it shows at https://microsoft.com/devicelogin on any
  device.
- Sign-ins made in the app are **remembered** across restarts, in the OS's encrypted store (DPAPI
  on Windows, the Keychain on macOS, libsecret on Linux). `backend/.pool-manager-auth.json` holds
  only the account record. Where nothing can encrypt (Linux or WSL without a keyring), the app
  keeps the sign-in, and a connected gateway's master key, in memory and says so; it never writes
  them in plain text. **Sign out of this** forgets the sign-in.

### Networks, proxies and certificates

- **Proxy:** set `HTTPS_PROXY=http://proxy:8080` (and `NO_PROXY=localhost,127.0.0.1`). The app,
  pip and npm all read it.
- **TLS inspection** (a company certificate): install the company CA in the OS certificate store.
  Locally the app verifies TLS against that store ([truststore](https://pypi.org/project/truststore/);
  `POOL_MANAGER_SYSTEM_TRUST=false` turns it off). pip 24.2+ does the same. For older pip, set
  `PIP_CERT=<ca.pem>`; for npm, `NODE_EXTRA_CA_CERTS=<ca.pem>`.
- **Package mirrors:** `PIP_INDEX_URL=https://<mirror>/simple/` and
  `npm_config_registry=https://<mirror>/`. The doctor checks the mirror instead of PyPI and npm.
- The app needs `https://login.microsoftonline.com` and `https://management.azure.com`, plus your
  gateway's URL (its admin API and the test call), `https://api.loganalytics.azure.com` for the
  Metrics tab and `https://prices.azure.com` for list prices. The doctor tells a DNS failure, a
  timeout, an untrusted certificate and a proxy asking for credentials apart.

### Remote machines and WSL

Over SSH, sign in with a code and forward the port to open the UI on your own machine:
`ssh -L 8000:localhost:8000 <host>` (or `5174` with the dev server). In WSL, run
`scripts/start-local.sh` inside WSL with a Linux Python; `.gitattributes` keeps the shell
scripts' LF line endings even in a Windows checkout.

### By hand

```sh
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # bin/pip on Linux/macOS
cp .env.example .env                       # nothing to change; choose the gateway on the setup page
.venv/Scripts/python -m uvicorn pool_manager.app:app --reload           # serves static/ if built

cd ../frontend && npm ci && npm run dev    # http://localhost:5174, proxies /api to :8000
```

`python scripts/package-release.py` builds the release zip from a clone, after
`npm run build`. Pushing a `v*` tag publishes one (`.github/workflows/release.yml`).

After changing `infra/gateway/litellm-aca.bicep`, run `python scripts/build-gateway-template.py`
(needs the Azure CLI with Bicep) to recompile `backend/pool_manager/templates/litellm-aca.json`,
which is what the app deploys. CI runs it with `--check`.

### Terminal UI

`tui/` is a [Textual](https://textual.textualize.io/) terminal client of the backend's `/api`. It
still targets the APIM version and hasn't been ported to the LiteLLM gateway, so
`start-local.ps1 -Tui` / `start-local.sh --tui` refuse with a message. It isn't checked in CI or
included in the release package.

> [!CAUTION]
> Local development uses **your** Azure identity and your **real** gateway. Changes are live.

## Tests

```sh
cd backend && .venv/Scripts/pip install pytest==9.1.1 ruff==0.15.22 mypy==2.3.0
.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy
cd ../frontend && npm test && npm run build
```

Backend tests run against an in-memory Azure (`tests/fake_azure.py`: ARM, the gateway's Container
App and secrets, template deployments, permissions and roles, Foundry, Azure Monitor, Log
Analytics, Cost Management and Retail Prices) and an in-memory LiteLLM proxy
(`tests/fake_litellm.py`, its admin API and chat completions, modelled on v1.106). No Azure access
is needed.

An opt-in integration test runs the service layer against a **real** LiteLLM proxy with
PostgreSQL and two fake Azure OpenAI regions in Docker (`tests/litellm_stack/`). It checks that
LiteLLM takes the rows, credentials, aliases and keys the app writes, and routes the way the pool
says:

```sh
cd backend
docker compose -f tests/litellm_stack/docker-compose.yml up -d --wait
.venv/Scripts/python -m pytest -q -m litellm
```
