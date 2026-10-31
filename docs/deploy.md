[← SteadyApiPool](../README.md)

**English** | [简体中文](deploy.zh-CN.md)

# Host it in Azure

Two ways to run the manager itself in Azure instead of on your computer: on App Service for a
team, in front of one gateway, or as a public multi-tenant website (APIM only).

## Deploy to Azure

This hosts the manager itself on App Service with Entra sign-in (Easy Auth), for a team, in front
of one gateway:

- **APIM:** an existing service, or a new one the deployment creates in its resource group when
  you don't name one.
- **LiteLLM:** an **existing** gateway. Bicep never creates or changes it, because its secrets are
  generated once by the app and a re-provision must not rotate them. So deploy the gateway first
  from the local app ([Get started](getting-started.md), steps 1–3), or on AKS.

Prerequisites: [azd](https://aka.ms/azd), Azure CLI and Node.js 20.19+ or 22.12+. You need
permission to create custom roles and role assignments (Owner or User Access Administrator) on
the gateway's resource group, and on each subscription scanned for deployments.

**1. Create the Entra app registration for sign-in** (once):

```sh
az ad app create --display-name "SteadyApiPool" \
  --sign-in-audience AzureADMyOrg --enable-id-token-issuance true
# Note the appId. Add the redirect URI after the first provision (step 3).
```

**2. Configure and provision** for one kind of gateway:

```sh
cd SteadyApiPool
azd env new pool-manager
azd env set ENTRA_CLIENT_ID <appId>
azd env set POOL_OWNERS "you@contoso.com"  # comma-separated UPNs or object IDs who may change things
```

For **APIM**:

```sh
# Use an existing APIM service...
azd env set APIM_NAME <apim-name>
azd env set APIM_RESOURCE_GROUP <apim-resource-group>
# azd env set APIM_SUBSCRIPTION_ID <id>   # only if APIM is in another subscription
# ...or set no APIM_NAME to create apim-pool-<suffix> in the azd resource group:
# azd env set APIM_SKU BasicV2            # BasicV2 (default), StandardV2 or Developer (30-40 min, no SLA)
# azd env set APIM_PUBLISHER_EMAIL you@contoso.com   # defaults to the first POOL_OWNERS entry
```

For **LiteLLM**:

```sh
# A gateway on Container Apps (its Container App name)...
azd env set GATEWAY_NAME <gateway-name>
azd env set GATEWAY_RESOURCE_GROUP <gateway-resource-group>   # default: the azd resource group
# azd env set GATEWAY_SUBSCRIPTION_ID <id>                     # only if it is in another subscription
# ...or a gateway elsewhere (AKS, any LiteLLM proxy with a database):
# azd env set GATEWAY_URL https://litellm.contoso.com
# azd env set GATEWAY_MASTER_KEY sk-...
# azd env set GATEWAY_PRINCIPAL_ID <object-id>        # optional: the identity it calls Foundry as
# azd env set GATEWAY_APP_INSIGHTS_ID <resource-id>   # optional: where its OpenTelemetry goes
```

The kind follows from what you set: `GATEWAY_NAME` (or `GATEWAY_URL`) means LiteLLM, anything
else APIM. `azd env set GATEWAY_PRODUCT apim` (or `litellm`) says it outright. Then:

```sh
azd provision --preview                    # review the what-if first
azd up
```

**3. Register the redirect URI** printed as `AUTH_REDIRECT_URI`:

```sh
az ad app update --id <appId> --web-redirect-uris "$(azd env get-value AUTH_REDIRECT_URI)"
```

Open `WEB_APP_URL` and sign in. Users listed in `POOL_OWNERS` may make changes and see keys. With
delegated Azure access (below), so may users whose own Azure roles already allow it: on APIM,
every APIM write the app makes; on LiteLLM, reading the gateway's master key (a Container App
secret), since they could change the gateway directly anyway. For a LiteLLM gateway outside
Container Apps, only owners can. Everyone else is read-only. To restrict who can sign in at all,
set *Assignment required* on the app's enterprise application.

### Delegated Azure access (optional, recommended)

The app identity does the pool work and never gets permission to change role assignments. To let
the setup wizard **fix** access problems (turn on APIM's identity, grant the gateway's identity a
role on Foundry) and check what each user may do, Easy Auth also gets an Azure Resource Manager
token for the signed-in user. Fixes then run with that user's own Azure rights and show up as that
user in the activity log. Without it, the wizard still checks everything and writes the admin
requests, and the LiteLLM cost panel can't read actual spend.

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
| Linux App Service plan (B1) + Web App (Python 3.12) | HTTPS only, TLS 1.2, FTP/SCM basic auth disabled, system-assigned identity. With `GATEWAY_URL`, the LiteLLM gateway's master key is an app setting |
| User-assigned identity `id-pool-auth-*` (only with delegated Azure access) | Federated credential for Easy Auth instead of a client secret. It has no Azure roles |
| App Service Authentication (Easy Auth) | Entra sign-in is required on every path except `/health` |
| APIM: API Management service `apim-pool-*` (only without `APIM_NAME`) | `BasicV2` by default (`APIM_SKU`), system-assigned identity for members, tagged `managed-by: apim-backend-pool`. It is billed from creation |
| APIM: custom role *APIM Backend Pool Manager* on the service | `Insights/metrics/read` for the Metrics tab, `backends/*`, `namedValues/*`, `apis/read,write,delete` (it deletes only APIs it created, when their pool is removed), `apis/operations/write` and `apis/policies/read,write`, plus `Authorization/permissions/read` so setup can check the app's own access |
| LiteLLM: custom role *AI Gateway Pool Manager* on the gateway's Container App and its Application Insights component (Container Apps gateways only) | `Microsoft.App/containerApps/read` and `listSecrets/action` (the gateway's URL, identity and master key; every pool change then goes through LiteLLM's admin API), `Insights/components/read` and `Insights/logs/*/read` for the Metrics tab, `Insights/metrics/read` for the Gateway metrics tab's replicas, CPU and memory (its database's need *Monitoring Reader* on the PostgreSQL server and *Reader* on the resource group, which this role doesn't give), and `Authorization/permissions/read` so setup can check the app's own access. It can't change the Container App, its database or anything else |
| Custom role *APIM Backend Pool Deployment Reader* or *AI Gateway Pool Deployment Reader* per discovery subscription | `CognitiveServices/accounts/read` and `accounts/deployments/read`, `Insights/metrics/read` for the members' metrics, plus read-only `Authorization/permissions`, `roleAssignments` and `roleDefinitions` so setup can check the gateway's role on each account. It has no `listKeys` and cannot change roles. Turn it off with the Bicep parameter `enableDeploymentDiscovery=false`, and choose subscriptions with `discoverySubscriptionIds` (the default is the gateway's subscription) |

The gateway's own identity needs *Cognitive Services OpenAI User* (or any role that allows
inference) on each member account. The setup wizard checks this and, with delegated Azure access,
grants it. On APIM, if member secrets reference Key Vault, its identity also needs *Key Vault
Secrets User* on the vault.

### Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `POOL_MANAGER_SUBSCRIPTION_ID` / `_RESOURCE_GROUP` | required when deployed | Where the gateway is. Locally, leave the gateway settings out to choose, create or connect one on the setup page |
| `POOL_MANAGER_APIM_NAME` | empty | An APIM service, with the two above. Don't set it with `_GATEWAY_NAME` |
| `POOL_MANAGER_GATEWAY_NAME` | empty | A LiteLLM gateway's Container App, with the two above; or, with `_GATEWAY_URL`, the display name of a gateway elsewhere |
| `POOL_MANAGER_GATEWAY_URL` | empty | A LiteLLM gateway elsewhere (AKS, any LiteLLM proxy with a database). `_SUBSCRIPTION_ID` is then only where deployments are searched by default |
| `POOL_MANAGER_GATEWAY_MASTER_KEY` | empty | That gateway's master key. Required when deployed; locally the setup page can keep it encrypted instead |
| `POOL_MANAGER_GATEWAY_PRINCIPAL_ID` | empty | That gateway's managed identity (object ID), so setup can check and grant its Foundry access |
| `POOL_MANAGER_GATEWAY_APP_INSIGHTS_ID` | empty | The Application Insights component its OpenTelemetry goes to, for the Metrics tab |
| `POOL_MANAGER_POOL_API_VERSION` | `2025-09-01-preview` | APIM: API version for pool reads and writes. With `2024-05-01`, session affinity is disabled because GA cannot carry `sessionAffinity` |
| `POOL_MANAGER_TENANT_ID` | from the setup page | Tenant of the subscription, so local tokens are requested there |
| `POOL_MANAGER_OWNERS` | empty | Who may make changes |
| `POOL_MANAGER_RBAC_WRITERS` | `true` | Also let users make changes whose own Azure roles allow it (APIM: every write the app makes; LiteLLM: reading the master key). Needs delegated Azure access |
| `POOL_MANAGER_DELEGATED_ACCESS` | `false` | Set by `enableDelegatedAzureAccess`: Easy Auth provides the user's ARM token, so setup can fix access |
| `POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS` | the gateway's subscription | Comma-separated subscriptions scanned for deployments |
| `POOL_MANAGER_PROTECTED_PREFIXES` | empty | Comma-separated ID prefixes shown read-only, for resources another tool manages (e.g. `finops-`) |
| `POOL_MANAGER_ID_PREFIX` | `pool-` | Prefix for new pools, members and test keys |
| `POOL_MANAGER_AUTH_MODE` | `easy_auth` | `disabled` is for local development; the app refuses to start that way on App Service. `entra` is the [public app](#public-app-on-azure-container-apps), APIM only |
| `POOL_MANAGER_SYSTEM_TRUST` | `true` | Locally, verify TLS against the OS certificate store (company CAs behind TLS inspection) |
| `AZURE_CLIENT_ID` | — | Use a user-assigned identity instead of system-assigned |

## Public app on Azure Container Apps

A deployment of it runs at https://ca-pool-public-5xgy5gsq3leyq.ambitiousground-f590833b.swedencentral.azurecontainerapps.io/
(choose **Browse as guest (read-only)** to look around without signing in).

`deploy/container-apps/` deploys the app as a public website: anyone with a work or school
account signs in with their own Microsoft Entra identity, and the app manages the APIM services
and Foundry deployments **their own Azure roles** allow. Nothing about a customer is configured
in advance, and the app has no Azure access of its own. The public app works with **APIM only**:
a LiteLLM gateway would need each user's master key on the server, which this design avoids.

| | [Deploy to Azure](#deploy-to-azure) (App Service) | Public app (Container Apps) |
| --- | --- | --- |
| Who signs in | Users of your own tenant | Any work or school tenant (or an allow-list) |
| Gateway | One APIM service or LiteLLM gateway, fixed at deployment | Each user chooses or creates their own APIM service |
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
A load test runs on the replica that started it, so the ingress keeps each browser on one replica
(sticky sessions); if that replica stops, so does the test, and its results are gone.

| Setting | Default | Purpose |
| --- | --- | --- |
| `POOL_MANAGER_AUTH_MODE` | `entra` (set by the template) | The public app's sign-in |
| `POOL_MANAGER_ENTRA_CLIENT_ID` | from the hook | The multi-tenant app registration |
| `POOL_MANAGER_FEDERATED_IDENTITY_CLIENT_ID` | from the template | The managed identity used as the app's federated credential |
| `POOL_MANAGER_ENTRA_ALLOWED_TENANTS` | empty (`ALLOWED_TENANTS`) | Tenant IDs whose users may sign in; empty = any work or school tenant |
| `POOL_MANAGER_SESSION_IDLE_SECONDS` | `1800` | A session (and its Azure tokens) ends this long after the user's last request |
| `POOL_MANAGER_MAX_SESSIONS` | `500` | Sessions kept per replica; the least recently used goes first |
| `POOL_MANAGER_USER_REQUESTS_PER_MINUTE` | `600` | Requests per user per minute |
