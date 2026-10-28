# SteadyApiPool

**English** | [简体中文](README.zh-CN.md)

A standalone web app that gives you an **AI gateway with failover** in front of your Azure OpenAI /
Foundry model deployments, without having to learn the gateway or Foundry first. It finds your
model deployments and groups deployments of one model into a **pool**. Clients call the pool at
one endpoint; the gateway spreads requests across the members (failover, balanced or weighted),
benches a member that keeps failing, and retries on another member.

You choose the gateway on the setup page:

- **[Azure API Management](https://learn.microsoft.com/azure/api-management/) (APIM)**, with its
  native backend pools, circuit breakers and session affinity. The app uses an APIM service you
  have, or creates one, and binds each pool to an API.
- **A [LiteLLM](https://github.com/BerriAI/litellm) gateway**, an open-source AI gateway. The app
  deploys it on Azure Container Apps or AKS, or connects one you already run, and gives clients
  OpenAI-style model names, aliases and virtual keys.

![A pool's Routing tab: the client calls the gateway with one model name, and the pool spreads requests across Foundry deployments in several regions, with cooldown and retries](docs/images/pool-routing.png)

*One endpoint for clients, several model deployments behind it: the Routing tab shows how the
gateway routes traffic through the pool, and lets you click a deployment to see what happens when
it fails or is throttled.*

## Which gateway?

| | Azure API Management | LiteLLM |
| --- | --- | --- |
| Pick it when | Your organization already runs APIM, or wants a managed Azure service with its policies, products and portal | You want an OpenAI-style endpoint with model names, aliases and per-key limits, or an open-source gateway you can run anywhere |
| What the app sets up | An APIM service (yours, or a new Basic v2 one), a `Pool` backend per pool and an API bound to it | A LiteLLM proxy with PostgreSQL on Container Apps (one step) or a new AKS cluster, or a connection to yours; a model group per pool |
| How clients call it | `https://<apim>.azure-api.net/<api-path>/chat/completions?api-version=…` with `Ocp-Apim-Subscription-Key` | `https://<gateway>/v1/chat/completions` with a virtual key and the pool ID (or an alias) as the model |
| Failure handling | A circuit breaker per member, retries in the API's policy, optional session affinity | Cooldown per member, retries on another member, Responses API follow-ups pinned to the deployment that answered |
| Cost | The service's tier (Basic v2 is the suggested start; Developer has no SLA), billed by the hour | From about $46 a month for one Container Apps replica; the sizer prices the plan for your traffic in your region before you deploy (list prices) |
| Also available as | The [public app](docs/deploy.md#public-app-on-azure-container-apps), where anyone signs in with their own Azure account | — |

Both kinds manage the same Foundry deployments with the same identity rules (the gateway calls
them with its managed identity, no keys), and you can switch between them on the setup page.

## Quick start

The app runs on your computer and needs only **Python**: no Azure CLI, Node.js, Docker or config
file, and no APIM or LiteLLM experience. It checks what's missing and fixes it with you. It takes
ten minutes to half an hour, most of it Azure creating the gateway if you don't have one. Every
option of every step is in the [step-by-step guide](docs/getting-started.md).

**What you need**

- **Python 3.11 or newer**: `winget install -e --id Python.Python.3.13` on Windows,
  `brew install python@3.13` on macOS, or see
  [Prerequisites per OS](docs/development.md#prerequisites-per-os).
- **An Azure account** that can manage an existing gateway or create one (*Contributor*), and read
  your Azure OpenAI / Foundry resources (*Reader*).
- **Some Azure OpenAI / Foundry model deployments**, ideally of one model in several regions. The
  app can create them.

### 1. Download and start

Download `SteadyApiPool-<version>.zip` from
[Releases](https://github.com/guming3d/SteadyApiPool/releases), unzip it, and run:

```powershell
scripts\start-local.cmd        # Windows
```

```sh
scripts/start-local.sh         # macOS, Linux, WSL
```

The first run installs what the app needs (about a minute). Then open **http://127.0.0.1:8000**.

### 2. Sign in to Azure

Click **Sign in with browser**, or **Use a code instead** on a machine without a browser. The app
remembers the sign-in.

![Setup page, step 1 of Sign in, AI gateway, Permissions and Gateway → Foundry: Sign in with browser, or Use a code instead](docs/images/onboarding-1-sign-in.png)

### 3. Choose the gateway

Pick **LiteLLM gateway** or **Azure API Management** (you can switch later), then **Use this
gateway** or **Use this service** on one you have.

![Setup page, step 2: LiteLLM gateway or Azure API Management, with the LiteLLM gateways in a subscription, each with Use this gateway, plus Deploy a gateway and Connect a gateway](docs/images/onboarding-2-choose-gateway.png)

No APIM service yet? **Create an APIM service** fills everything in (the Basic v2 tier) and waits
until the service is ready, usually a few minutes.

![Setup page, step 2 with Azure API Management chosen: the APIM services in a subscription, each with Use this service, plus Create an APIM service](docs/images/onboarding-2-choose-apim.png)

![Create an APIM service form, pre-filled with Basic v2](docs/images/onboarding-2b-create-apim.png)

No LiteLLM gateway yet? **Deploy a gateway** sizes it for your peak traffic, recommends Azure
Container Apps or AKS and shows how it got there, then deploys it (5–25 minutes). See
[Sizing the gateway](docs/getting-started.md#sizing-the-gateway) for the numbers behind it, or
**Connect a gateway** to use a LiteLLM proxy you already run.

![Size the gateway for your traffic: peak RPM, tokens, timeout and streaming, with the Container Apps plan recommended and How we got this open, beside the AKS plan](docs/images/onboarding-2b-deploy-gateway.png)

### 4. Pick the deployments to pool

Open the **Deployments** tab and click **Create pool from this model**. Nothing deployed yet?
**Deploy a model** creates one.

![Deployments tab: gpt-5.6-luna deployed in three regions, with Create pool from this model](docs/images/onboarding-3-deployments.png)

### 5. Publish the pool

The pool opens filled in. **Failover** sends all traffic to the primary and switches to a standby
when it fails; **Balanced** and **Weighted** spread it instead.

![Add pool, pre-filled in Failover mode with three members and the cooldown settings](docs/images/onboarding-4-create-pool.png)

Click **Publish to APIM** (or **Publish to the gateway**), review exactly what the app will write,
and confirm.

![Review and publish: the new pool and its three members](docs/images/onboarding-5-publish.png)

### 6. Let the gateway call your models, and get a key

On the **Test call** tab, **Grant all** lets the gateway's managed identity call your Foundry
resources without API keys, and **Create a test key** gets a key that can call only this pool.

![Test call tab: the endpoint and model names, Before you call, and Get a virtual key with Create a test key](docs/images/onboarding-6-grant-and-key.png)

### 7. Send a test request

Click **Send request** to get the model's answer and the member that served it. **Send a request**,
further down, has the client code for curl, Python, JavaScript and PowerShell.

![Try it here: 200, the call succeeded, answered by the pool's primary member pool-aoai-westus3-gpt-5-6-luna-dz](docs/images/onboarding-7-test-call.png)

### 8. Watch it work

The **Metrics** tab shows requests, availability, 429s and latency, and each member's share of the
traffic. **Routing** simulates a member failing.

![Metrics tab: requests, availability, 429s, latency and each member's traffic share](docs/images/onboarding-8-metrics.png)

> [!CAUTION]
> The app acts on Azure as **you**, on your **real** gateway (and, for LiteLLM, with its master
> key). Changes are live for every client at once.

Stuck? **Permission check** in the top bar re-runs the setup checks and writes a request for your
Azure admin for what your rights can't fix, and `python scripts/doctor.py` explains machine and
network problems. More in [If something gets in the way](docs/getting-started.md#if-something-gets-in-the-way).

## Documentation

- [Step-by-step guide](docs/getting-started.md): every option of each step, deploying a LiteLLM
  gateway on Container Apps or AKS, client code, the load test, troubleshooting, and a demo that
  runs without Azure.
- [Features and how it works](docs/features.md): everything the app does, how a pool maps to APIM
  and to LiteLLM, and what a LiteLLM gateway deployment creates and how it's maintained.
- [Host it in Azure](docs/deploy.md): App Service for a team with `azd`, and the public
  multi-tenant app.
- [Run it locally and develop](docs/development.md): prerequisites per OS, sign-in, proxies and
  certificates, WSL, the project layout, and the tests.
