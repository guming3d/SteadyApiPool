# Testing

[中文](testing.zh-CN.md)

Four layers, from the fastest to the most real. The first three run on every pull request and
every push to `main` and `unified` (`.github/workflows/ci.yml`); the live Azure journeys run
nightly, weekly or by hand (`.github/workflows/e2e-azure.yml`).

| Layer | What it proves | Where it runs | Time | Cost |
| --- | --- | --- | --- | --- |
| Unit and API tests (`backend/tests`, `frontend/tests`) | Every route, rule and invariant against in-memory Azure, APIM and LiteLLM fakes | CI, locally | ~1 min | none |
| 1. Browser journeys (`e2e/browser`) | The real UI, clicked through as users do, on the real backend wired to the fakes | CI, locally | ~1 min | none |
| 2. Real LiteLLM (`backend/tests/litellm/test_litellm_stack*.py`) | The app's API and service layer against the pinned LiteLLM image, Postgres and two fake regions that can fail | CI, locally with Docker | ~1 min | none |
| 3. Live Azure (`e2e/azure`) | A user's whole first run on real Azure: deploying the gateway, setup, Foundry, pools, failover, monitoring, cost, resize, removal, deletion | GitHub Actions on a schedule, locally | 25-60 min a journey | see below |

## Unit and API tests

```sh
cd backend && .venv/Scripts/python -m pytest -q      # bin/python on macOS and Linux
cd frontend && npm test
```

See [development.md](development.md) for the fakes and the conventions.

## 1. Browser journeys

Playwright drives Chromium through the built UI served by `scripts/demo_server.py`, which runs
the real FastAPI app against the in-memory Azure, APIM service and LiteLLM gateway. Each test
resets the demo to a known stage (`POST /demo/stage/{sign-in,ready,pool}`), so tests don't
depend on one another.

```sh
cd frontend && npm ci && npm run build && cd ..
cd e2e/browser
npm ci && npm run install-browsers
npm test                 # npm run test:headed to watch; npx playwright show-report after a failure
```

| Test | Journey |
| --- | --- |
| `onboarding` (LiteLLM, APIM) | Signed out, sign in, choose the kind of gateway, pick the existing one, land on Pools |
| `litellm-gateway`: deploy | **Deploy a gateway** with the sizer's plan, the region check, the deployment's progress, the app opening on the new gateway |
| `litellm-gateway`: panel | Cost, **Resize for new traffic** (the changes, the confirmation), **Switch or create** |
| `litellm-gateway`: metrics | A pool's Metrics tab and the Gateway metrics tab |
| `litellm-pools`: create | A pool from a model, failover order, review and publish, Foundry access granted, a test key and a test call naming the member that answered |
| `litellm-pools`: edit and remove | Cooldown and retries edited and read back after a reload, then the pool removed with its plan |
| `apim`: create | A pool with an API, the policy view, a test key and a test call |
| `apim`: in use | Metrics, gateway load, cost, switching |
| `shell` | Light and dark mode, the language switch, both kept across a reload |

It needs the backend's virtual environment (`backend/.venv`), or `E2E_PYTHON` pointing at a
Python with `backend/requirements.txt` installed; `E2E_PORT` moves the demo server off 8010.
On a failure, CI keeps the HTML report, traces, screenshots and videos as an artifact.

## 2. Real LiteLLM

```sh
cd backend
docker compose -f tests/litellm/litellm_stack/docker-compose.yml up -d --wait
.venv/Scripts/python -m pytest -q -m litellm
docker compose -f tests/litellm/litellm_stack/docker-compose.yml down -v
```

The stack is LiteLLM (the image the app deploys, configured the same way), its Postgres, and two
fake Azure OpenAI regions whose answers `POST /control` changes (200, 429, 500). The tests
check what only the real proxy can show:

- `test_litellm_stack.py`: the rows, credentials, aliases and keys the app writes are accepted;
  failover by `order`, cooldown, key scoping, `x-litellm-model-id`; and that a consumer can't
  send a pool's call to another host (`api_base`, `base_url`, `azure_ad_token`, also nested in
  `extra_body` or `metadata`), which with `enable_azure_ad_token_refresh` would carry the
  gateway's token there.
- `test_litellm_stack_app.py`: a user's journey through the app's HTTP API, as the browser makes
  it: members, the editor's preview, a pool, an alias, a test key and call, a failover, an edit
  (and a stale ETag refused), a load test, the CSRF check, removal; and cleanup taking only what
  the app created.

Each test starts by clearing everything named `it-*` through LiteLLM's admin API, so a failed
run can't fail the next one.

## 3. Live Azure

Each journey is a module of ordered steps, run by an app started from a temporary copy of
`backend/pool_manager` (its own state file and key store, never the checkout's), signed in as
the Azure CLI is. The steps call only the app's HTTP API, exactly as the UI would, plus the
Azure CLI to create the run's resource group and to check what the app built. When a step
fails, the rest of that journey is skipped; teardown deletes the resource group whatever
happened.

| Journey (`e2e/azure/`) | Steps | Time | Cost a run |
| --- | --- | --- | --- |
| `test_litellm_aca.py` | First run, sizer plan and region check, deploy a trial gateway on Container Apps, check what was deployed (HTTPS origin, draining, ingress), setup grants Foundry access, discovery and import, a pool whose first member can't answer, a test call that fails over to Foundry, an alias, a load test, the Monitor and Gateway metrics tabs, cost, a resize plan that changes nothing, removal, deleting the gateway | ~25 min | under $1 |
| `test_apim.py` | First run, create a BasicV2 service, turn on its identity and grant it Foundry, a pool behind an API (the policy carries the app's markers), a key and a test call past the member that can't answer (the circuit breaker), a load test, Monitor and Gateway metrics, cost, removal (the API goes), soft-deleting the service (teardown purges it) | ~30-60 min | about $1 |
| `test_litellm_aks.py` | First run, the AKS plan and region check, deploying the cluster, the install through Run Command and HTTPS with Let's Encrypt, the cluster's settings and its admin role for the deploying identity, setup, a pool answering over the cluster's HTTPS address with a failover, a resize plan, removal and deletion | 45-60 min | a few dollars |

Every pool has three members: an endpoint on a host that doesn't resolve, first, then the two
long-lived Foundry deployments. So every call really fails over, without breaking a real
deployment: LiteLLM retries on the next member in the same call; APIM's circuit breaker opens on
the first failure and the next calls go to the next member.

### Once: the Foundry accounts and the GitHub identity

Signed in with `az login` as someone who may create resources and app registrations:

```sh
python scripts/e2e-azure-setup.py --dry-run foundry     # what it would do
python scripts/e2e-azure-setup.py foundry               # two accounts, a gpt-4.1-mini each
python scripts/e2e-azure-setup.py identity --repo <owner>/<repo>
```

`foundry` creates resource group `e2e-foundry` with an AI Services account in each of two
regions (`--regions`, default `eastus2 swedencentral`), each with a GlobalStandard deployment
`e2e-chat` of the same model (`--model`, `--version`). Pay-per-token: idle, they cost nothing.
It prints `E2E_SUBSCRIPTION_ID`, `E2E_FOUNDRY_ACCOUNTS` and `E2E_FOUNDRY_DEPLOYMENT`.

`identity` creates the app registration GitHub Actions signs in as, with no secret: a federated
credential for the repository's `azure-e2e` environment, by name (`repo:<owner>/<repo>:...`) and,
for a repository with GitHub's immutable OIDC subjects (`repo:<owner>@<id>/<repo>@<id>:...`, read
from GitHub's API with the `gh` CLI), by ID too; a login that fails with `AADSTS700213` names the
subject GitHub sent. In the subscription it gets
*Contributor*, and *Role Based Access Control Administrator* limited by a condition to the two
roles the app assigns (*Cognitive Services OpenAI User*, *Azure Kubernetes Service RBAC Cluster
Admin*), so it can't grant anything else. It prints `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and
`AZURE_SUBSCRIPTION_ID`.

Then, in GitHub: **Settings > Environments > New environment** `azure-e2e`. Limit its
deployment branches (for example to `main` and `unified`) so a pull request can't run it, and
add the six values above as environment **variables** (none is a secret). Optionally set
`E2E_LOCATION` (default `swedencentral`) and `E2E_GATEWAY_SIZE`.

**Where a subscription may deploy.** Subscriptions differ: some may not create PostgreSQL in a
region at all (the app's region check says so before anything is created, and the LiteLLM
journeys then take the first region it offers), and some may not create a *Burstable* server
though the region lists it (Azure answers `SkuNotAvailable` only when the server is created; no
read-only check shows it). For those, set `E2E_GATEWAY_SIZE=small`: the LiteLLM journeys then
deploy up to two replicas with a General Purpose database and Redis instead of the one-replica
trial (`trial`, the default), which costs a little more per run and also covers the production
path.

### Running it

In GitHub: **Actions > E2E (live Azure) > Run workflow**, pick the journeys, a region, and
whether to keep the resources. On its own it runs Container Apps and APIM nightly and AKS on
Sundays; each journey is a job of its own, one at a time per journey.

Locally, signed in with `az login`:

```sh
export E2E_LIVE=1 E2E_SUBSCRIPTION_ID=<id> E2E_FOUNDRY_ACCOUNTS=<id>,<id> E2E_FOUNDRY_DEPLOYMENT=e2e-chat
# optional: E2E_TENANT_ID, E2E_LOCATION, E2E_GATEWAY_SIZE=small, E2E_KEEP=1 to leave the resources for a look
cd backend && .venv/Scripts/python -m pytest ../e2e/azure/test_litellm_aca.py
```

Without `E2E_LIVE=1` every step is skipped: nothing is created by accident (CI collects them that
way on every push).

### Cleaning up

A run deletes its resource group (`e2e-<journey>-<run>`) in teardown and purges the APIM
service it soft-deleted. If a run is cancelled or its runner dies, the workflow's janitor job,
and `python scripts/e2e-azure-janitor.py --subscription <id> [--dry-run]` by hand, deletes the
groups tagged `steadyapipool-e2e` older than six hours and purges deleted APIM services named
`e2eapim…`. It never touches anything else, `e2e-foundry` included.

### Signing in for an hour and more

The app gets every Azure token from the Azure CLI. In GitHub Actions the CLI's federated
sign-in only lasts as long as the workflow's OIDC token, a few minutes, while a journey takes an
hour. The harness (`harness.AzureLogin`) therefore asks GitHub for a new OIDC token and signs the
CLI in again every four minutes; the app picks it up on its next token request. Locally your own
`az login` is used as it is.

## Adding to it

- A UI change: add or change a journey in `e2e/browser/tests`, selecting by role, label and the
  English text users see (no test IDs). Keep each test starting from a stage.
- A change to what the app writes to LiteLLM: a step in `test_litellm_stack_app.py` (through the
  app's API) or `test_litellm_stack.py` (the service layer).
- A new user operation on Azure: a step in the journey it belongs to, through the app's API, and
  a check with the Azure CLI of what it changed. A step that waits on Azure uses `Api.wait`, with
  a timeout and a `failed` reason, never a fixed sleep.
