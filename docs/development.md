[← SteadyApiPool](../README.md)

**English** | [简体中文](development.zh-CN.md)

# Run it locally and develop

## Project layout

```
SteadyApiPool/
├── backend/    FastAPI app (pool_manager: shared code, apim/ and litellm/) + tests; serves the
│               built frontend
├── frontend/   React/Vite UI (src/apim, src/litellm and the shared rest; built into
│               backend/pool_manager/static)
├── infra/      Bicep: the LiteLLM gateway on Container Apps and AKS (infra/gateway), and the App
│               Service hosting of this app with Easy Auth and least-privilege roles
├── deploy/     The public multi-tenant app on Container Apps (APIM only; its own azd project)
├── scripts/    Local start scripts, the doctor, the demo server, the load test, release
│               packaging and the gateway template build
├── docs/       Screenshots for this README
└── azure.yaml  azd project definition (App Service hosting)
```

## Run it locally

Locally the app runs on your machine and acts on Azure as **you**. All it needs is
**Python 3.11+**. Node.js is only needed to work on the browser UI, and the Azure CLI is optional.

**Quickest: the release package.** Download `SteadyApiPool-<version>.zip` from
[Releases](https://github.com/guming3d/SteadyApiPool/releases). It has the browser
UI and the gateway templates built in, so it needs no Node.js and no git. Unzip it, then:

```powershell
scripts\start-local.cmd                 # Windows; works whatever the PowerShell execution policy
```

```sh
scripts/start-local.sh                  # macOS, Linux, WSL
```

Open http://127.0.0.1:8000. The setup page signs you in, then lets you choose the kind of gateway
and the gateway, create one or connect one (the choice is saved in `backend/.pool-manager.json`,
never with a master key; a connected LiteLLM gateway's key is kept OS-encrypted in
`backend/.pool-manager-gateways.bin`). To pin a gateway instead, set `POOL_MANAGER_SUBSCRIPTION_ID`
and `_RESOURCE_GROUP` with `_APIM_NAME` or `_GATEWAY_NAME` (or `_GATEWAY_NAME` and
`_GATEWAY_URL`) in `backend/.env`; `backend/.env.example` shows each.

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
  gateway's URL (the test call, and LiteLLM's admin API), `https://api.loganalytics.azure.com`
  for a LiteLLM gateway's Metrics tab and `https://prices.azure.com` for list prices. The doctor
  tells a DNS failure, a timeout, an untrusted certificate and a proxy asking for credentials
  apart.

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

After changing `infra/gateway/litellm-aca.bicep` or `litellm-aks.bicep`, run
`python scripts/build-gateway-template.py` (needs the Azure CLI with Bicep) to recompile
`backend/pool_manager/litellm/templates/litellm-{aca,aks}.json`, which is what the app deploys.
CI runs it with `--check`.

> [!CAUTION]
> Local development uses **your** Azure identity and your **real** gateway. Changes are live.

## Tests

```sh
cd backend && .venv/Scripts/pip install pytest==9.1.1 ruff==0.15.22 mypy==2.3.0
.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy
cd ../frontend && npm test && npm run build
```

Backend tests run against an in-memory Azure (`tests/fake_azure.py`: ARM, template deployments,
permissions and roles, Foundry, Azure Monitor, Log Analytics, Cost Management and Retail Prices),
an in-memory APIM on top of it (`tests/apim/fake_apim.py`, which enforces ETags and `If-Match` /
`If-None-Match`) and an in-memory LiteLLM proxy (`tests/litellm/fake_litellm.py`, its admin API
and chat completions, modelled on v1.106). No Azure access is needed.

An opt-in integration test runs the LiteLLM service layer against a **real** LiteLLM proxy with
PostgreSQL and two fake Azure OpenAI regions in Docker (`tests/litellm/litellm_stack/`). It checks that
LiteLLM takes the rows, credentials, aliases and keys the app writes, and routes the way the pool
says:

```sh
cd backend
docker compose -f tests/litellm/litellm_stack/docker-compose.yml up -d --wait
.venv/Scripts/python -m pytest -q -m litellm
```
