[← SteadyApiPool](../README.md)

**English** | [简体中文](getting-started.zh-CN.md)

# Get started: step by step

This guide runs the app on your own computer. You install Python and nothing else. You don't
need the Azure CLI, Node.js, Docker, a config file, or any APIM or LiteLLM experience; the app
checks what's missing and fixes it with you. (To give your team a shared URL instead, see
[Deploy to Azure](deploy.md#deploy-to-azure).)

**What you need**

- **Python 3.11 or newer.** `winget install -e --id Python.Python.3.13` on Windows,
  `brew install python@3.13` on macOS, or see [Prerequisites per OS](development.md#prerequisites-per-os).
- **An Azure account** that can manage an existing gateway (an APIM service or a LiteLLM
  gateway's Container App), or create one in a subscription (*Contributor*), and read your Azure
  OpenAI / Foundry resources (*Reader*).
- **Some Azure OpenAI / Foundry model deployments**, ideally of the same model in more than one
  region. If you don't have any, the app can create them (step 4).

## 1. Download and start

Download `SteadyApiPool-<version>.zip` from
[Releases](https://github.com/guming3d/SteadyApiPool/releases), unzip it, and run
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

## 2. Sign in to Azure

Click **Sign in with browser** and sign in with your Azure account. On a machine without a
browser (SSH, WSL, a server), click **Use a code instead**. The app remembers the sign-in in your
OS's encrypted store, so you do this once.

![Setup page, step 1 of Sign in, AI gateway, Permissions and Gateway → Foundry: Sign in with browser, or Use a code instead](images/onboarding-1-sign-in.png)

## 3. Choose the kind of gateway, then the gateway

Pick **LiteLLM gateway** or **Azure API Management** (see
[Which gateway?](../README.md#which-gateway)); you can switch later. The app then lists the gateways of that kind in each
subscription you can access and says whether you may manage them.

![Setup page, step 2: LiteLLM gateway or Azure API Management, with the LiteLLM gateways in a subscription, each with Use this gateway, plus Deploy a gateway and Connect a gateway](images/onboarding-2-choose-gateway.png)

### Azure API Management

Click **Use this service** on the APIM service you want.

![Setup page, step 2 with Azure API Management chosen: the APIM services in a subscription, each with Use this service, plus Create an APIM service](images/onboarding-2-choose-apim.png)

No APIM service yet? Click **Create an APIM service**. Everything is filled in: a unique name,
a new resource group, a region and the recommended **Basic v2** tier. Check the publisher
email and click **Create and use**. The app waits until the service is ready, usually a few
minutes (the Developer tier takes 30–40). Azure bills the service from the moment it's created.

![Create an APIM service form, pre-filled with Basic v2](images/onboarding-2b-create-apim.png)

### LiteLLM gateway

Click **Use this gateway** on a LiteLLM gateway on Azure Container Apps.

No gateway yet? Click **Deploy a gateway**. The app first sizes it for your traffic (see
[Sizing the gateway](#sizing-the-gateway) below) and recommends Container Apps or AKS:

- **Deploy on Container Apps** opens a form with everything filled in: a unique name, a resource
  group and a region, with the plan it deploys and its list price in that region.
  As you pick the region it checks that the region can take every resource of the gateway (see
  [Before deploying, and when it fails anyway](features.md#before-deploying-and-when-it-fails-anyway)).
  Click **Deploy**. The app deploys LiteLLM, PostgreSQL, a managed identity and Application
  Insights with your own Azure rights, and follows the deployment, which usually takes 5–15
  minutes (PostgreSQL is the slowest). You can close the app meanwhile; setup picks it up again.
  Azure bills the resources from the moment they're created.
- **Deploy on AKS** opens the same form for a **new AKS cluster**. Click **Deploy**; the app
  then works in three phases, shown as it goes (usually 15–25 minutes in all):
  1. **Azure resources**: the cluster (Entra ID with Azure RBAC, workload identity, the managed
     Gateway API), PostgreSQL, the gateway's identity, Application Insights and, with more than
     one pod, Redis. You get *Azure Kubernetes Service RBAC Cluster Admin* on the cluster, so deploying
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
  [A LiteLLM gateway on AKS](features.md#a-litellm-gateway-on-aks).
- **Use my own cluster (files)** is for a cluster you already have: it asks for the cluster, its
  resource group and a namespace, and generates a `values.yaml`, a `deploy.sh` and a `README.md`
  (Helm chart, workload identity, PostgreSQL Flexible Server, one replica; the README says what more replicas need). The cluster needs the OIDC issuer
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

![Size the gateway for your traffic: peak RPM, tokens, timeout and streaming, with the Container Apps plan recommended and How we got this open, beside the AKS plan](images/onboarding-2b-deploy-gateway.png)

A two-minute guided tour opens next. Take it or skip it; **Tour** in the top bar replays it.

#### Sizing the gateway

The sizer asks for your traffic at its peak: requests a minute, the average input and output
tokens, the longest a call may take, and whether responses are streamed. **More options** has the
model speed (fast, standard or reasoning, or your own average response time), the headroom above
the peak (25 % by default), surviving the loss of an availability zone (on by default), and
whether you run AKS already, host your own models next to the gateway or need a private network.

From these the backend works out the gateway, and **How we got this** shows each step with its
numbers (every formula, a quick-reference table and worked examples are in
[Sizing a LiteLLM gateway](sizing.md)):

| Step | How |
| --- | --- |
| Requests and tokens | Requests a second = RPM ÷ 60. Tokens a minute = RPM × (input + output): the Foundry quota your pools need together |
| Calls in flight | Requests a second × the time a call takes (the first token, then the output at the model's speed); and the same with every call slowed to the timeout |
| One replica | Gateway CPU per call (a fixed cost, the bodies, each streamed token) against one LiteLLM worker at 70 % of a core; and how many calls its memory holds |
| Replicas | The peak's, plus the headroom, plus one for a zone (at least two). Container Apps adds a replica past the calls in flight each holds at the peak, or past 60 % of its worker's core; AKS at 60 % CPU |
| Database and Redis | The smallest PostgreSQL whose connections cover replicas × 10 × 2 (an update doubles them): General Purpose, Burstable only for a single replica. Redis whenever there is more than one replica, and spend through Redis from 1,000 requests a second or 10 replicas |
| AKS | The node size and range for the pods (2 a D4ds_v5, 5 a D8ds_v5, 12 a D16ds_v5), at least three nodes across zones, and outbound IPs with enough SNAT ports per node for every call slowed to the timeout |

It recommends **Container Apps** unless the traffic rules it out. Its ingress ends a request that
sends nothing for 240 seconds (measured: a silent call got a 504 at 241 s, while a call streaming
every 10 s ran its full 300 s), so calls that may take longer without streaming, or whose first
token comes later than that, go to **AKS**; so do peaks that need more than 30 replicas, teams that
already run AKS, and self-hosted GPU models. Each card shows that platform's plan, anything that
keeps it from running there, and a deploy button. The deploy form then prices exactly that plan in
the region you pick, and the region check uses its sizes.

The gateway CPU per call was measured with `backend/tests/litellm/litellm_stack/calibrate.py` on
LiteLLM configured as this app deploys it (Docker, a fake upstream): about 27 ms a call, about
0.75 ms per streamed token, and with OpenTelemetry about 0.11 ms more per streamed token for
every 1,000 prompt tokens, with one worker doing 26–46 calls a second flat out. **Streaming is
what makes a gateway big**: a 500-token stream costs about 15 times a whole answer. Azure's CPUs
may differ somewhat, and memory is estimated, so after deploying, load-test a pool at your peak
(the pool's **Load test**, or `scripts/benchmark.py`) to confirm the plan. Prices are retail list
prices from quiet to busy, without free grants, log ingestion, discounts and your model usage;
once the gateway exists, its panel shows this month's actual spend.

A gateway that must not be reachable from the internet works on both, but private networking
isn't automated here: on Container Apps, put the environment in a virtual network afterwards (or
adapt the template); on AKS, use an internal load balancer or private ingress.

## 4. Pick the deployments to pool

Open the **Deployments** tab. It lists every Azure OpenAI / Foundry deployment in the gateway's
subscription, grouped by model. (Deployments in other subscriptions: set
`POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS` in `backend/.env`.) Click **Create pool from this model**
on the model you want. To pool only some of a model's deployments, tick them and choose
**Import & create pool**.

![Deployments tab: gpt-5.6-luna deployed in three regions, with Create pool from this model](images/onboarding-3-deployments.png)

Nothing deployed yet? **Deploy a model** creates a deployment in one of your Foundry resources,
then lists it here.

## 5. Publish the pool

The pool opens with everything filled in and the defaults work:

- **APIM:** an ID, an API path that clients will call, the members and a circuit breaker.
- **LiteLLM:** an ID (here `pool-gpt-5-6-luna`), which is also the model name clients send, the
  members, and the cooldown, retry and timeout settings.

**Failover** sends all traffic to the primary and switches to a standby when it fails or runs out
of quota; choose **Balanced** to spread traffic over every member instead, or **Weighted** to set
the shares.

![Add pool, pre-filled in Failover mode with three members and the cooldown settings](images/onboarding-4-create-pool.png)

Click **Publish to APIM** (or **Publish to the gateway**). The app lists exactly what it will
write. Click it again to confirm. Changes take effect on the gateway immediately.

![Review and publish: the new pool and its three members](images/onboarding-5-publish.png)

## 6. Let the gateway call your models, and get a key

The pool opens on its **Test call** tab, which walks you through the rest:

1. **Grant all** gives the gateway's managed identity (APIM's, or the LiteLLM gateway's) the
   *Cognitive Services OpenAI User* role on each Foundry resource, so it calls your models
   without API keys. That takes a role allowed to assign roles on those resources (*Owner*, *User
   Access Administrator* or *Role Based Access Control Administrator*). Without one,
   **Permission check** in the top bar shows a ready-to-send request for your Azure admin. On
   APIM, setup first turns on the service's managed identity if it is off.
2. **Create a test key** gets a key that can call only this pool:
   - **APIM:** a subscription key that opens only the pool's API.
   - **LiteLLM:** a virtual key that can call only the pool's model names. LiteLLM keeps only a
     hash of it, so copy it now if you want to keep it: it's shown this once. **Add an alias**,
     above it, lets clients that already send another model name, such as `gpt-5.6-luna`, reach
     the pool.

![Test call tab: the endpoint and model names, Before you call, and Get a virtual key with Create a test key](images/onboarding-6-grant-and-key.png)

## 7. Send a test request

Click **Send request**. The request goes through the gateway and the pool, and you get the
model's answer with the member that served it (on LiteLLM, also how many times it tried other
members first). If you only just granted access, Azure can take up to 10 minutes to apply the
role; until then the page explains the error, so try again shortly.

![Try it here: 200, the call succeeded, answered by the pool's primary member pool-aoai-westus3-gpt-5-6-luna-dz](images/onboarding-7-test-call.png)

Your apps call the pool like any OpenAI endpoint. **Send a request**, further down, has the code
for curl, Python, JavaScript and PowerShell with your URL filled in. In Python, through APIM:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<your-apim>.azure-api.net/<api-path>",
    api_key="unused",  # The SDK needs a value; APIM sends its own token to Azure OpenAI
    default_headers={"Ocp-Apim-Subscription-Key": "<your-subscription-key>"},
    default_query={"api-version": "2025-04-01-preview"},
)
response = client.chat.completions.create(
    model="gpt-5.6-luna", messages=[{"role": "user", "content": "Hello"}]
)
print(response.choices[0].message.content)
```

Through LiteLLM (base URL `<gateway>/v1`, the virtual key, the pool ID or an alias as the model,
and no `api-version`):

```python
from openai import OpenAI

client = OpenAI(base_url="https://<your-gateway>/v1", api_key="<your-virtual-key>")

response = client.chat.completions.create(
    model="pool-gpt-5-6-luna",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

## Load-test the pool (optional)

**Load test**, under Try it here, simulates several users calling the pool at once: each sends a
request, waits for the answer, then sends the next. Pick **Smoke** (2 users, 30 s), **Typical**
(10 users, 2 min) or **Stress** (30 users, 3 min), or set the users, duration, ramp-up, a rate cap,
the prompt size, the output tokens and streaming under **Advanced settings**. The page estimates the
requests and tokens first; every request is billed by Foundry like real traffic and counts against
the deployments' rate limits. While it runs you see throughput, the success rate, latency p50/p95/
p99, time to first token, 429s and failovers, a chart per second and which members answered; at
the end a plain verdict (for example that the pool needs more capacity because every member
returned 429 at once), **Export JSON / CSV** and the same test as a terminal command. A run is
limited to 50 users, 10 minutes and 5,000 requests, one at a time, and only owners can start one.

On LiteLLM it uses the virtual key from step 1 and sends `max_tokens`; the members are the ones
LiteLLM names in `x-litellm-model-id`, and failovers come from `x-litellm-attempted-fallbacks`. On
APIM it calls the API chosen above with its subscription key and `api-version`, and sends
`max_completion_tokens`, because APIM forwards the body as it is and newer models refuse
`max_tokens`. APIM doesn't say which member answered, so the table shows the regions that answered
(Azure OpenAI's `x-ms-region`: members in one region count together), and its retries after a 429
don't show as failovers.

From a terminal, against this or any OpenAI-compatible endpoint (the key stays in an environment
variable, out of the shell history):

```powershell
$env:LITELLM_KEY = "<your-virtual-key>"
backend\.venv\Scripts\python scripts\benchmark.py --url https://<your-gateway>/v1 --model pool-gpt-5-6-luna --preset typical
#   --users 20 --duration 120 --ramp-up 10 --rate 15 --prompt long --max-tokens 256 --no-stream
#   --json result.json --csv requests.csv        (macOS/Linux: backend/.venv/bin/python, export LITELLM_KEY=...)

# APIM: the API's URL, the key in Ocp-Apim-Subscription-Key, api-version 2025-04-01-preview
$env:APIM_KEY = "<your-subscription-key>"
backend\.venv\Scripts\python scripts\benchmark.py --gateway apim --url https://<your-apim>.azure-api.net/<api path> --model gpt-5.6-luna
#   --api-version <version> --key-header <header>   (when the API uses others)
```

It prints a status line every second and the report at the end; Ctrl+C stops it (requests already
sent finish) and still prints the report. `--i-know` lifts the limits for the terminal only.

## 8. Watch it work

The **Metrics** tab shows requests, availability, the 429s that reached clients (on LiteLLM, also
how many a failover saved) and latency, plus each member's share of the traffic and tokens.
**Routing** simulates a member failing, so you can see the failover before it happens for real.

![Metrics tab: requests, availability, 429s, latency and each member's traffic share](images/onboarding-8-metrics.png)

That's it. Everything else (more pools, weights, session affinity or aliases, binding your own
APIs, custom endpoints, cleaning up) is in the same UI; [What it does](features.md#what-it-does) lists it
all.

> [!CAUTION]
> The app acts on Azure as **you**, on your **real** gateway (and, for LiteLLM, with its master
> key). Changes are live for every client at once.

## If something gets in the way

- **Permission check** in the top bar re-runs the setup checks. Whatever your own Azure rights
  can't fix becomes a request to send your admin: the role, the scope, an `az` command and a
  portal link.
- `python scripts/doctor.py` explains what's wrong with your machine or network (Python version,
  proxy, company TLS certificates, DNS) and how to fix it on your OS.
- Behind a proxy or on a locked-down network? See
  [Networks, proxies and certificates](development.md#networks-proxies-and-certificates).

## Try it without Azure

From a clone, `scripts/demo_server.py` runs the same UI against an
in-memory Azure, APIM and LiteLLM gateway with sample deployments, which is how the screenshots
in this guide were made. Nothing reaches Azure. It needs the backend's virtual environment (the start
script creates it) and the built UI (Node.js 22+):

```sh
cd frontend && npm ci && npm run build && cd ..
backend/.venv/Scripts/python scripts/demo_server.py    # bin/python on macOS and Linux; http://127.0.0.1:8010
```

It starts signed out, like a first run. `POST /demo/stage/{sign-in,ready,pool}?product=apim` (or
`litellm`) jumps to a point in onboarding (`pool` is a pool already in use, with an hour of
metrics); `--product` and `--stage` choose where it starts.
