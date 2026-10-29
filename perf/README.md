# The benchmark

How the numbers in [docs/benchmarks.md](../docs/benchmarks.md) were measured, and how to measure
them again. Everything here creates billable Azure resources and sends real, billed calls to
Foundry for the real-model part. Delete the resource groups when you're done.

## What it measures

- **Gateway capacity, with a stand-in model.** The pool's members are three stand-in Azure
  OpenAI deployments (`mock/mock_openai.py`, Container Apps `mock-a`, `mock-b`, `mock-c` on
  their own host names, like three Foundry accounts). They answer like a model of a known
  speed: the first token after 1 s plus 0.1 s per 1,000 prompt tokens, then 60 tokens a
  second, one token a chunk when streamed. A real model's quota would run out long before a
  gateway does; with the stand-in, what slows down is the gateway.
- **Failover under load.** The mocks' control app makes `mock-a` answer 429, then 500, then
  nothing at all, on a schedule, while the load goes on.
- **The real model.** The same calls straight to one gpt-6-luna deployment and through each
  gateway's pool of four, as the load passes the first deployment's quota.

The load comes from a VM in the gateway's region (`loadgen.py`): open loop, so requests start on
schedule whether or not earlier ones have answered, as independent clients do, from several
processes. A step passes when at most 1% of its requests failed, it delivered at least 95% of the
load offered, and its p95 latency stayed within 1.25 × the same call sent straight to the mock.
A step where the VM itself ran above 85% CPU doesn't count.

## Call shapes

| Shape | Input tokens | Output tokens | Streamed |
|---|---|---|---|
| `short` | 500 | 100 | no |
| `chat` | 2,000 | 500 | yes |
| `rag` | 8,000 | 300 | yes |
| `agent` (an agent loop's step) | 50,000 | 400 | no |

## Running it

Needs the Azure CLI signed in, Python 3.11+ and the app running locally (it sets up the
gateways under test the way a user would). The VM is driven through Run Command and files go
through the control app's relay (token in `perf/.relay-token`), so no SSH or storage account is
needed.

```sh
# A rig in the gateway's region (the VM, the mocks, a registry for the mock image)
az group create -n rg-perf -l <region>
az deployment group create -g rg-perf -f perf/infra/rig.bicep \
  -p sshPublicKey="$(cat ~/.ssh/id_ed25519.pub)" allowedSshIp=<your IP> deployMocks=false \
     relayToken="$(python -c 'import secrets; print(secrets.token_urlsafe(32))' | tee perf/.relay-token)"
az acr build -r <registry> -t mock-openai:2 perf/mock
az deployment group create ... deployMocks=true mockImage=<registry>.azurecr.io/mock-openai:2
# perf/rig.json (or rig-<name>.json with PERF_RIG=<name>) from the outputs, then:
python perf/ctl.py init

# The gateways: create them in the app, then the pools, keys and targets
python perf/gateways.py apim <resource group> <service> apim-basic
python perf/gateways.py litellm <resource group> <gateway> aca

# Plans in perf/plans/*.json (targets, ramps, failovers), then
python perf/ctl.py upload
python perf/ctl.py start <plan>.json <run-id>
python perf/ctl.py status <run-id>
python perf/ctl.py fetch <run-id>
python perf/report.py perf/results/<run-id> ... --out perf/report.json
```

`loadgen.py` also runs on its own against any OpenAI-compatible endpoint (`--help`).
