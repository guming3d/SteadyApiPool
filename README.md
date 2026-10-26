# APIM Backend Pool Manager

**English** | [简体中文](README.zh-CN.md)

A standalone web app for Azure API Management **native backend pools**: failover,
balanced and weighted distribution, circuit breakers and session affinity. It also lets you
bind a pool to an API.

```
apim-backend-pool/
├── backend/    FastAPI app (pool_manager) + tests; serves the built frontend
├── frontend/   React/Vite UI (built into backend/pool_manager/static)
├── tui/        Terminal UI (Textual), another client of the backend's /api
├── infra/      Bicep: App Service + Easy Auth + least-privilege APIM role
├── deploy/     The public app on Container Apps (its own azd project)
├── scripts/    Local start scripts (browser or -Tui), the demo server and the azd frontend build hook
├── docs/       Screenshots for this README
└── azure.yaml  azd project definition
```

**Online demo:** https://ca-pool-public-5xgy5gsq3leyq.ambitiousground-f590833b.swedencentral.azurecontainerapps.io/ (choose **Browse as guest (read-only)** to look around
without signing in, or sign in to manage your own APIM services).

New here? [Get started](#get-started) takes about ten minutes and needs only Python. To offer it
to other organizations as a website where everyone signs in with their own Azure account, see
[Public app on Azure Container Apps](#public-app-on-azure-container-apps).

## Get started

This guide runs the app on your own computer. You install Python and nothing else. You don't
need the Azure CLI, Node.js, a config file or APIM experience; the app checks what's missing
and fixes it with you. (To give your team a shared URL instead, see [Deploy to Azure](#deploy-to-azure).)

**What you need**

- **Python 3.11 or newer.** `winget install -e --id Python.Python.3.13` on Windows,
  `brew install python@3.13` on macOS, or see [Prerequisites per OS](#prerequisites-per-os).
- **An Azure account** that can manage an API Management service, or create one in a
  subscription (*Contributor*), and read your Azure OpenAI / Foundry resources (*Reader*).
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

![Setup page, step 1: Sign in with browser](docs/images/onboarding-1-sign-in.png)

### 3. Choose your APIM service, or create one

The app lists the API Management services in each subscription you can access and says whether
you may manage them. Click **Use this service**.

![Setup page, step 2: the APIM services in a subscription, each with Use this service](docs/images/onboarding-2-choose-apim.png)

No APIM service yet? Click **Create an APIM service**. Everything is filled in: a unique name,
a new resource group, a region and the recommended **Basic v2** tier. Check the publisher
email and click **Create and use**. The app waits until the service is ready, usually a few
minutes (the Developer tier takes 30–40). Azure bills the service from the moment it's created.

![Create an APIM service form, pre-filled with Basic v2](docs/images/onboarding-2b-create-apim.png)

A two-minute guided tour opens next. Take it or skip it; **Tour** in the top bar replays it.

### 4. Pick the deployments to pool

Open the **Deployments** tab. It lists every Azure OpenAI / Foundry deployment in the APIM
service's subscription, grouped by model. (Deployments in other subscriptions: set
`POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS` in `backend/.env`.) Click **Create pool from this model** on the model you want.
To pool only some of a model's deployments, tick them and choose **Import & create pool**.

![Deployments tab: gpt-5.6-luna deployed in three regions, with Create pool from this model](docs/images/onboarding-3-deployments.png)

Nothing deployed yet? **Deploy a model** creates a deployment in one of your Foundry resources,
then lists it here.

### 5. Publish the pool

The pool opens with everything filled in: an ID, an API path that clients will call, the
members and a circuit breaker. The defaults work. **Failover** sends all traffic to the primary
and switches to a standby when it fails or runs out of quota; choose **Balanced** to spread
traffic over every member instead.

![Add pool, pre-filled with three members in Failover mode and an API path](docs/images/onboarding-4-create-pool.png)

Click **Publish to APIM**. The app lists exactly what it will create. Click **Publish to APIM**
again to confirm.

![Review and publish: the pool, its three members and the new API](docs/images/onboarding-5-publish.png)

### 6. Let APIM call your models, and get a key

The pool opens on its **Test call** tab, which walks you through the rest:

1. **Grant all** gives APIM's managed identity the *Cognitive Services OpenAI User* role on
   each Foundry resource, so APIM can call your models without API keys. That takes a role
   allowed to assign roles on those resources (*Owner*, *User Access Administrator* or *Role
   Based Access Control Administrator*). Without one, **Permission check** in the top bar shows
   a ready-to-send request for your Azure admin.
2. **Create a test key for …** creates an APIM subscription key that opens only this API, and
   fills it in.

![Test call tab: Grant all, and Create a test key](docs/images/onboarding-6-grant-and-key.png)

### 7. Send a test request

Click **Send request**. The request goes through APIM and the pool, and you get the model's
answer, with the region that served it. If you only just granted access, Azure can take up to
10 minutes to apply the role; until then the page explains the error, so try again shortly.

![Try it here: 200, the call succeeded, served from Sweden Central](docs/images/onboarding-7-test-call.png)

Your apps call the pool like any OpenAI endpoint. **Send a request**, further down, has the code
for curl, Python, JavaScript and PowerShell with your URL filled in. In Python:

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

### 8. Watch it work

The **Metrics** tab shows requests, availability, the 429s that reached clients and latency,
plus each member's share of the traffic. **Routing** simulates a member failing, so you can see
the failover before it happens for real.

![Metrics tab: requests, availability, 429s, latency and each member's traffic share](docs/images/onboarding-8-metrics.png)

That's it. Everything else (more pools, weights, session affinity, binding your own APIs,
cleaning up) is in the same UI; [What it does](#what-it-does) lists it all.

> [!CAUTION]
> The app acts on Azure as **you**, against your **real** APIM service. Changes are live.

**If something gets in the way**

- **Permission check** in the top bar re-runs the setup checks. Whatever your own Azure rights
  can't fix becomes a request to send your admin: the role, the scope, an `az` command and a
  portal link.
- `python scripts/doctor.py` explains what's wrong with your machine or network (Python version,
  proxy, company TLS certificates, DNS) and how to fix it on your OS.
- Behind a proxy or on a locked-down network? See
  [Networks, proxies and certificates](#networks-proxies-and-certificates).

**Try it without Azure.** From a clone, `scripts/demo_server.py` runs the same UI against an
in-memory APIM with sample deployments, which is how the screenshots above were made. It needs
the backend's virtual environment (the start script creates it) and the built UI (Node.js 22+):

```sh
cd frontend && npm ci && npm run build && cd ..
backend/.venv/Scripts/python scripts/demo_server.py    # bin/python on macOS and Linux; http://127.0.0.1:8010
```

## What it does

| Area | What it does |
| --- | --- |
| **Setup wizard** | Checks sign-in, the APIM service, your permissions and APIM's access to Foundry. It fixes what your Azure rights allow and writes a ready-to-send admin request for the rest. Locally it also picks, creates or deletes the APIM service. |
| **Guided tour** | A two-minute walkthrough of the page for people new to APIM. It runs once after setup; **Tour** in the top bar replays it. |
| **Deployment discovery** | Lists the Azure OpenAI / Foundry deployments in your subscriptions, grouped by model, and imports the ones you select into a new or existing pool. |
| **Deploy a model** | Creates a pay-per-token OpenAI model deployment in a Foundry account, using your own Azure rights, so you don't need the Foundry portal. |
| **Members** | Edited inside their pool. Besides Foundry deployments, you can add any OpenAI-compatible endpoint. Members the app created are deleted once no pool uses them. |
| **Circuit breaker** | One rule per pool, written to every member backend this app created. |
| **Pools** | Create and edit `Pool` backends with 2–30 members in **Failover**, **Balanced** or **Weighted** mode, with optional session affinity. |
| **Cost** | Shows the APIM service's spend this month and its tier's list price. Open it from the APIM name in the top bar. |
| **Removing** | Removes a pool, or everything this app created on the service, after showing exactly what will go. Locally it can also soft-delete a service it created (restorable for 48 hours). |
| **Routing preview** | Shows how APIM routes traffic through the pool and simulates failures, breaker trips and recovery. |
| **Pool API** | Creating a pool also creates and binds an HTTP API for it, so clients have an endpoint right away. |
| **How to call** | Gets a subscription key, sends a test request from the page, and gives ready-to-copy client code. |
| **Metrics** | A dashboard of requests, availability, 429s and latency for the pool, plus traffic share and tokens per member, from Azure Monitor. |
| **API binding** | Points an existing API at a pool through its policy, with a diff preview first. Unbinding restores the original policy byte for byte. |
| **Same model** | Refuses to save a pool whose Foundry members serve different models. The model is read from Azure, not guessed from the deployment name. |
| **Public app** | A website on Container Apps where anyone signs in with their own Microsoft account and works with their own Azure rights; nothing is stored on the server. |
| **Safety** | Every update is ETag-conditional. The app only deletes what it created and only after you have seen it, never rewrites policy it didn't write, and leaves resources with a protected ID prefix (`POOL_MANAGER_PROTECTED_PREFIXES`) read-only. |

Defaults:
- **Circuit breaker:** trips on 1 failure among 429 / 408 / 5xx / connection failure / timeout within 60 s, trips for 60 s, and honours `Retry-After`.
- **Retry:** 2 attempts, 1 s interval, fast first retry.
- **Backend timeout:** 120 s.

### Discovered deployments

- **What is listed:** only `Succeeded` deployments in **OpenAI format** on `OpenAI` or `AIServices` accounts. Partner models (Anthropic, DeepSeek, xAI, …) use other inference paths and are skipped.
- **Member URL:** each imported member uses `https://<account>.openai.azure.com/openai/deployments/<deployment>`. Deployments of the same model with different names, such as `gpt-5.6-luna` and the DataZone `gpt-5.6-luna-2`, can therefore share one pool.
- **How clients call it:** APIM appends the operation path, so clients call `https://<apim>/<api-path>/chat/completions?api-version=…` with the header `Ocp-Apim-Subscription-Key`. The API the app creates for a pool is already shaped this way. If you bind your own API instead, it must expose `/chat/completions` (or a catch-all `/*`), not `/deployments/{id}/…`.
- **Auth:** members authenticate with the **APIM managed identity**, because most accounts disable key auth. The setup wizard and the selection bar check both prerequisites and fix them if you may:
  1. The APIM system-assigned identity must be on (**Turn on identity**).
  2. It needs **Cognitive Services OpenAI User** on each account (**Grant access**, or an admin request with the exact `az role assignment create` command).
  3. The API the app creates for a pool already sends the managed-identity token. When binding your own API, set the managed identity resource to `https://cognitiveservices.azure.com`.
- **Caching:** results are cached for 5 minutes. **Refresh** rescans, which takes about 15 s for 25 accounts.

## Deploy to Azure

Prerequisites: [azd](https://aka.ms/azd), Azure CLI and Node.js 20.19+ or 22.12+. You need
permission to create custom roles and role assignments (Owner or User Access Administrator)
on the APIM resource group. An existing APIM instance is optional: without `APIM_NAME`, the
deployment creates one in its resource group.

**1. Create the Entra app registration for sign-in** (once):

```sh
az ad app create --display-name "APIM Backend Pool Manager" \
  --sign-in-audience AzureADMyOrg --enable-id-token-issuance true
# Note the appId. Add the redirect URI after the first provision (step 3).
```

**2. Configure and provision:**

```sh
cd apim-backend-pool
azd env new pool-manager
# Use an existing APIM service...
azd env set APIM_NAME <apim-name>
azd env set APIM_RESOURCE_GROUP <apim-resource-group>
# azd env set APIM_SUBSCRIPTION_ID <id>   # only if APIM is in another subscription
# ...or leave APIM_NAME unset to create apim-pool-<suffix> in the azd resource group:
# azd env set APIM_SKU BasicV2            # BasicV2 (default), StandardV2 or Developer (30-40 min, no SLA)
# azd env set APIM_PUBLISHER_EMAIL you@contoso.com   # defaults to the first POOL_OWNERS entry
azd env set ENTRA_CLIENT_ID <appId>
azd env set POOL_OWNERS "you@contoso.com"  # comma-separated UPNs or object IDs who may change things
azd provision --preview                    # review the what-if first
azd up
```

**3. Register the redirect URI** printed as `AUTH_REDIRECT_URI`:

```sh
az ad app update --id <appId> --web-redirect-uris "$(azd env get-value AUTH_REDIRECT_URI)"
```

Open `WEB_APP_URL` and sign in. Users listed in `POOL_OWNERS` may make changes. With delegated
Azure access (below), so may users whose own Azure roles already allow every APIM write the app
makes. Everyone else is read-only. To restrict who can sign in at all, set *Assignment required*
on the app's enterprise application.

### Delegated Azure access (optional, recommended)

The app identity does all pool work and never gets permission to change role assignments. To let
the setup wizard **fix** access problems (turn on APIM's identity, grant it a role on Foundry) and
check what each user may do, Easy Auth also gets an Azure Resource Manager token for the signed-in
user. Fixes then run with that user's own Azure rights and show up as that user in the activity
log. Without it, the wizard still checks everything and writes the admin requests.

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
| API Management service `apim-pool-*` (only without `APIM_NAME`) | `BasicV2` by default (`APIM_SKU`), system-assigned identity for members, tagged `managed-by: apim-backend-pool`. It is billed from creation |
| Linux App Service plan (B1) + Web App (Python 3.12) | HTTPS only, TLS 1.2, FTP/SCM basic auth disabled, system-assigned identity |
| User-assigned identity `id-pool-auth-*` (only with delegated Azure access) | Federated credential for Easy Auth instead of a client secret. It has no Azure roles |
| App Service Authentication (Easy Auth) | Entra sign-in is required on every path except `/health` |
| Custom role *APIM Backend Pool Manager* on the APIM service | `Insights/metrics/read` for the Metrics tab, `backends/*`, `namedValues/*`, `apis/read,write,delete` (it deletes only APIs it created, when their pool is removed), `apis/operations/write` and `apis/policies/read,write`, plus `Authorization/permissions/read` so setup can check the app's own access |
| Custom role *APIM Backend Pool Deployment Reader* per discovery subscription | `Insights/metrics/read` for the Metrics tab, `CognitiveServices/accounts/read` and `accounts/deployments/read`, plus read-only `Authorization/permissions`, `roleAssignments` and `roleDefinitions` so setup can check APIM's role on each account. It has no `listKeys` and cannot change roles. Turn it off with `enableDeploymentDiscovery=false`, and choose subscriptions with `discoverySubscriptionIds` (the default is the APIM subscription) |

APIM's own identity needs extra RBAC in two cases:
- **Key Vault secrets:** if member secrets reference Key Vault, the APIM managed identity needs *Key Vault Secrets User* on the vault.
- **Managed identity to Foundry:** if members authenticate with managed identity, the APIM identity needs *Cognitive Services OpenAI User* (or any role that allows inference) on each member account. The setup wizard checks this and can grant it.

### Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `POOL_MANAGER_SUBSCRIPTION_ID` / `_RESOURCE_GROUP` / `_APIM_NAME` | required when deployed | Target APIM service. Locally, leave them out to choose or create the service on the setup page; set all three to fix it |
| `POOL_MANAGER_TENANT_ID` | from the setup page | Tenant of the APIM subscription, so local tokens are requested there |
| `POOL_MANAGER_OWNERS` | empty | Who may make changes |
| `POOL_MANAGER_RBAC_WRITERS` | `true` | Also let users make changes whose own Azure roles allow every APIM write the app makes. Needs delegated Azure access |
| `POOL_MANAGER_DELEGATED_ACCESS` | `false` | Set by `enableDelegatedAzureAccess`: Easy Auth provides the user's ARM token, so setup can fix access |
| `POOL_MANAGER_POOL_API_VERSION` | `2025-09-01-preview` | API version for pool reads and writes. With `2024-05-01`, session affinity is disabled because GA cannot carry `sessionAffinity` |
| `POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS` | APIM subscription | Comma-separated subscriptions scanned for deployments |
| `POOL_MANAGER_PROTECTED_PREFIXES` | empty | Comma-separated ID prefixes shown read-only, for resources another tool manages (e.g. `finops-`) |
| `POOL_MANAGER_ID_PREFIX` | `pool-` | Suggested prefix for new resources |
| `POOL_MANAGER_AUTH_MODE` | `easy_auth` | `disabled` is for local development; the app refuses to start that way on App Service |
| `POOL_MANAGER_SYSTEM_TRUST` | `true` | Locally, verify TLS against the OS certificate store (company CAs behind TLS inspection) |
| `AZURE_CLIENT_ID` | — | Use a user-assigned identity instead of system-assigned |

## Public app on Azure Container Apps

A deployment of it runs at https://ca-pool-public-5xgy5gsq3leyq.ambitiousground-f590833b.swedencentral.azurecontainerapps.io/.

`deploy/container-apps/` deploys the app as a public website: anyone with a work or school
account signs in with their own Microsoft Entra identity, and the app manages the APIM services
and Foundry deployments **their own Azure roles** allow. Nothing about a customer is configured
in advance, and the app has no Azure access of its own.

| | [Deploy to Azure](#deploy-to-azure) (App Service) | Public app (Container Apps) |
| --- | --- | --- |
| Who signs in | Users of your own tenant | Any work or school tenant (or an allow-list) |
| APIM service | One, fixed at deployment | Each user chooses or creates their own |
| Who calls Azure | The app's managed identity, with custom roles | The signed-in user, through a token exchanged on their behalf |
| Who may change things | `POOL_OWNERS`, or users whose roles allow it | Whatever the user's own Azure roles allow |

**How it works.** The browser signs in with MSAL.js (a multi-tenant app registration) and sends a
token for this app's API with each request. The backend verifies it (Entra's signing keys,
audience, an issuer that matches the token's tenant, the `access_as_user` scope), then exchanges
it on behalf of the user for an Azure Resource Manager token. Every Azure call (APIM, Foundry,
Azure Monitor, Cost Management) runs as that user, so Azure decides what is allowed and the
activity log names them. The app proves its own identity in that exchange with its user-assigned
managed identity, set up as a federated credential, so the app registration has no client secret.

**What is stored.** Nothing on the server: no passwords, tokens, keys or customer data, and no
state files. Azure tokens live in the user's in-memory session, expire within about an hour, and
are dropped when the user signs out (**Sign out** in the top bar), after 30 minutes without
requests, or when the session store is full. In the browser, MSAL keeps its tokens in
`sessionStorage` (gone when the tab closes), and the IDs of the chosen APIM service are kept in
`localStorage` for that account (removed on sign-out).

**Keeping users apart.** Every request is authenticated on its own, and sessions are keyed by the
verified tenant and object ID, never by anything the browser chooses. Each user (and each APIM
service they open) gets their own set of components, so every cache (deployments, metrics,
permissions, cost) belongs to one user; nothing one user loaded can answer another's request,
even in the same tenant. Each user is rate-limited (600 requests a minute by default). The test
call only goes to Azure's own gateway host names (`*.azure-api.net`), so the backend can't be
pointed at other hosts.

**Deploy it.** You need [azd](https://aka.ms/azd), the Azure CLI signed in (`az login`) to the
tenant that will own the app registration, Python 3, and rights to create app registrations and
role assignments in the target resource group (Owner, or Contributor plus User Access
Administrator). Docker and Node.js aren't needed: the image is built in Azure Container Registry.

```sh
cd deploy/container-apps
azd env new pool-public
# azd env set ALLOWED_TENANTS "<tenant-id>,<tenant-id>"   # optional: only these organizations
azd up
```

`azd up` runs `scripts/public-app-registration.py` before and after provisioning. Before, it
creates (or updates) the multi-tenant app registration and sets `ENTRA_CLIENT_ID`. After, it adds
the container app's `/redirect.html` as a redirect URI and the federated credential for its
managed identity. Then it provisions the Container Apps environment, registry, identity and Log
Analytics workspace, builds the image and deploys it. Open the `PUBLIC_APP_URL` it prints.

**Before other organizations use it:**
- Verify the publisher of the app registration (*Microsoft Entra > App registrations > Branding &
  properties*). Many tenants only let users consent to apps from verified publishers.
- The first sign-in from a new organization installs the app there (its enterprise application)
  when the user consents to *Azure Service Management* access as themselves; Entra may then ask
  once more for the app's own API. Where users can't consent (many companies, including
  Microsoft's own tenant, require admin approval for apps like this), an admin of that tenant
  consents once for everyone at
  `https://login.microsoftonline.com/<their-tenant-id>/adminconsent?client_id=<ENTRA_CLIENT_ID>`,
  which covers both permissions, and can withdraw it later under *Enterprise applications*.
  Until one of these happens, Entra answers sign-ins from that tenant with AADSTS500011 (the
  app isn't in that tenant) or a request for admin approval.
- Users still need Azure roles on what they manage, as in the portal: for example *API Management
  Service Contributor* on the APIM service and *Reader* on their Foundry resources.

**Guest view (optional).** To let visitors look around without signing in, point the deployment
at one APIM service before provisioning:

```sh
azd env set GUEST_APIM_NAME <apim-name>
azd env set GUEST_APIM_RESOURCE_GROUP <apim-resource-group>
# azd env set GUEST_APIM_SUBSCRIPTION_ID <id>   # only if it is in another subscription
azd provision && azd deploy
```

The sign-in page then offers **Browse as guest (read-only)**. Guests see that service and the
Foundry deployments in its subscription, read with the app's managed identity, which gets the
built-in *Reader* role on that subscription and nothing else: Reader can't change anything or list
keys and secrets. The backend also refuses guests every change, subscription keys, test calls,
setup fixes and choosing another service. Guests share one cached session and are rate-limited
(300 requests a minute per client address, 3,000 for all guests together). Anything in that
service and subscription, such as pool members' URLs and API policies, is visible to anyone with
the URL, so use a demo service.

**Limits.** Azure public cloud only. A sign-in covers one tenant; choosing a service in another
tenant signs in to that tenant. Deployment discovery scans the chosen service's subscription.
Services whose gateway uses a custom domain can be managed but not test-called from the app.

| Setting | Default | Purpose |
| --- | --- | --- |
| `POOL_MANAGER_AUTH_MODE` | `entra` (set by the template) | The public app's sign-in |
| `POOL_MANAGER_ENTRA_CLIENT_ID` | from the hook | The multi-tenant app registration |
| `POOL_MANAGER_FEDERATED_IDENTITY_CLIENT_ID` | from the template | The managed identity used as the app's federated credential |
| `POOL_MANAGER_ENTRA_ALLOWED_TENANTS` | empty (`ALLOWED_TENANTS`) | Tenant IDs whose users may sign in; empty = any work or school tenant |
| `POOL_MANAGER_SESSION_IDLE_SECONDS` | `1800` | A session (and its Azure tokens) ends this long after the user's last request |
| `POOL_MANAGER_MAX_SESSIONS` | `500` | Sessions kept per replica; the least recently used goes first |
| `POOL_MANAGER_USER_REQUESTS_PER_MINUTE` | `600` | Requests per user per minute |

## Run it locally

Locally the app runs on your machine and acts on Azure as **you**. All it needs is
**Python 3.11+**. Node.js is only needed to work on the browser UI, and the Azure CLI is optional.

**Quickest: the release package.** Download `apim-backend-pool-<version>.zip` from
[Releases](https://github.com/guming3d/apim-backend-pool-manager/releases). It has the browser
UI built in, so it needs no Node.js and no git. Unzip it, then:

```powershell
scripts\start-local.cmd                 # Windows; works whatever the PowerShell execution policy
```

```sh
scripts/start-local.sh                  # macOS, Linux, WSL
```

Open http://127.0.0.1:8000. The setup page signs you in, then lets you choose the APIM service
or create one (the choice is saved in `backend/.pool-manager.json`). To pin a service instead,
set `POOL_MANAGER_SUBSCRIPTION_ID`, `_RESOURCE_GROUP` and `_APIM_NAME` in `backend/.env`.

**From a clone**, the same scripts run the Vite dev server on `:5174` when Node.js 22+ is
installed. Without Node.js they serve the UI from `backend/pool_manager/static`, if it has been
built (`-Prebuilt` / `--prebuilt` forces that).

```powershell
.\scripts\start-local.ps1               # -Tui -Prebuilt -BackendPort 8001 -FrontendPort 5175 -SkipInstall -SkipChecks
```

```sh
scripts/start-local.sh                  # --tui --prebuilt --backend-port 8001 --frontend-port 5175 --skip-install --skip-checks
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
  device. In the TUI, press `d` on the Setup view.
- Sign-ins made in the app are **remembered** across restarts, in the OS's encrypted store (DPAPI
  on Windows, the Keychain on macOS, libsecret on Linux). `backend/.pool-manager-auth.json` holds
  only the account record. Where nothing can encrypt (Linux or WSL without a keyring), the app
  keeps the sign-in in memory and says so; it never writes tokens in plain text. **Sign out of
  this** forgets it.

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
  APIM gateway for the test call. The doctor tells a DNS failure, a timeout, an untrusted
  certificate and a proxy asking for credentials apart.

### Remote machines and WSL

Over SSH, sign in with a code and forward the port to open the UI on your own machine:
`ssh -L 8000:localhost:8000 <host>` (or `5174` with the dev server). Or use the terminal UI
(`--tui`), which needs no browser at all. In WSL, run `scripts/start-local.sh` inside WSL with a
Linux Python; `.gitattributes` keeps the shell scripts' LF line endings even in a Windows checkout.

### By hand

```sh
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # bin/pip on Linux/macOS
cp .env.example .env                       # nothing to change; choose APIM on the setup page
.venv/Scripts/python -m uvicorn pool_manager.app:app --reload           # serves static/ if built

cd ../frontend && npm ci && npm run dev    # http://localhost:5174, proxies /api to :8000
```

`python scripts/package-release.py` builds the release zip from a clone, after
`npm run build`. Pushing a `v*` tag publishes one (`.github/workflows/release.yml`).

### Terminal UI

The same app also runs in the terminal, for people who live there. `tui/` is a separate
[Textual](https://textual.textualize.io/) app in the style of Omarchy's TUIs: Tokyo Night colours,
rounded panes (the focused one in the accent colour), views as workspaces in the top bar and a
key-hint footer. It is only a client of the backend's `/api`, so every rule (ETags, protected
prefixes, the same-model check, who may write) still lives in one place.

```powershell
.\scripts\start-local.ps1 -Tui           # backend + TUI; Node.js isn't needed
```

```sh
scripts/start-local.sh --tui
# or by hand: cd tui && python -m venv .venv && .venv/bin/pip install -r requirements.txt
#             .venv/bin/python -m pool_tui              # reuses :8000 or starts backend/ itself
#             .venv/bin/python -m pool_tui --api http://127.0.0.1:8000
```

| Key | |
| --- | --- |
| `1` `2` `3` | Pools, Deployments, Setup |
| `n` `e` `D` | New, edit (mode, members, primary, weights, affinity), delete a pool |
| `a` `b` `u` | Create an API for the pool, bind an existing API, unbind |
| `t` | Try the API: pick or create a subscription key, send a request, read what the answer means |
| `m` | Monitor range (1h, 6h, 24h, 7d) for the pool's metrics and member table |
| `space` `i` `c` `R` | Deployments: select, import as members, import and create a pool, rescan Azure |
| `enter` `c` | Setup: run the fix under the cursor, choose the APIM service |
| `d` `o` | Setup: sign in with a code (no browser here, e.g. over SSH), sign out of the app's sign-in |
| `d` / `enter` | In choose APIM: delete a service this app created (type its name), restore one from Recently deleted |
| `?` `q` | Help, quit |

It covers the day-to-day operations. Creating an APIM service, the routing simulator and the
policy diff are in the web UI. The TUI's text is English only.

> [!CAUTION]
> Local development uses **your** Azure identity against a **real** APIM service. Changes are live.

## Tests

```sh
cd backend && .venv/Scripts/pip install pytest==9.1.1 ruff==0.15.22 mypy==2.3.0
.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy
cd ../frontend && npm test && npm run build
cd ../tui && .venv/Scripts/pip install -r requirements.txt -r ../backend/requirements.txt pytest==9.1.1 ruff==0.15.22 mypy==2.3.0
.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy
```

Backend tests run against an in-memory APIM (`tests/fake_apim.py`) that enforces ETags and
`If-Match` / `If-None-Match`. No Azure access is needed. The TUI tests drive the real backend
app wired to that same fake, through `httpx.ASGITransport`.
