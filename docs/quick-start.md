[← SteadyApiPool](../README.md)

**English** | [简体中文](quick-start.zh-CN.md)

# Quick start

From sign-in to the first call through your pool, with a screenshot of each step. It continues
the README's [1. Download and start](../README.md#1-download-and-start), with the app running
at **http://127.0.0.1:8000**. Every option of every step is in the
[step-by-step guide](getting-started.md).

> [!CAUTION]
> The app acts on Azure as **you**, on your **real** gateway (and, for LiteLLM, with its master
> key). Changes are live for every client at once.

## 2. Sign in to Azure

Click **Sign in with browser**, or **Use a code instead** on a machine without a browser. The app
remembers the sign-in.

![Setup page, step 1 of Sign in, AI gateway, Permissions and Gateway → Foundry: Sign in with browser, or Use a code instead](images/onboarding-1-sign-in.png)

## 3. Choose the gateway

Pick **LiteLLM gateway** or **Azure API Management** (you can switch later), then **Use this
gateway** or **Use this service** on one you have.

![Setup page, step 2: LiteLLM gateway or Azure API Management, with the LiteLLM gateways in a subscription, each with Use this gateway, plus Deploy a gateway and Connect a gateway](images/onboarding-2-choose-gateway.png)

No APIM service yet? **Create an APIM service** fills everything in (the Basic v2 tier) and waits
until the service is ready, usually a few minutes.

![Setup page, step 2 with Azure API Management chosen: the APIM services in a subscription, each with Use this service, plus Create an APIM service](images/onboarding-2-choose-apim.png)

![Create an APIM service form, pre-filled with Basic v2](images/onboarding-2b-create-apim.png)

No LiteLLM gateway yet? **Deploy a gateway** sizes it for your peak traffic, recommends Azure
Container Apps or AKS and shows how it got there, then deploys it (5–25 minutes). See
[Sizing the gateway](getting-started.md#sizing-the-gateway) for the numbers behind it, or
**Connect a gateway** to use a LiteLLM proxy you already run.

![Size the gateway for your traffic: peak RPM, tokens, timeout and streaming, with the Container Apps plan recommended and How we got this open, beside the AKS plan](images/onboarding-2b-deploy-gateway.png)

## 4. Pick the deployments to pool

Open the **Deployments** tab and click **Create pool from this model**. Nothing deployed yet?
**Deploy a model** creates one.

![Deployments tab: gpt-5.6-luna deployed in three regions, with Create pool from this model](images/onboarding-3-deployments.png)

## 5. Publish the pool

The pool opens filled in. **Failover** sends all traffic to the primary and switches to a standby
when it fails; **Balanced** and **Weighted** spread it instead.

![Add pool, pre-filled in Failover mode with three members and the cooldown settings](images/onboarding-4-create-pool.png)

Click **Publish to APIM** (or **Publish to the gateway**), review exactly what the app will write,
and confirm.

![Review and publish: the new pool and its three members](images/onboarding-5-publish.png)

## 6. Let the gateway call your models, and get a key

On the **Test call** tab, **Grant all** lets the gateway's managed identity call your Foundry
resources without API keys, and **Create a test key** gets a key that can call only this pool.

![Test call tab: the endpoint and model names, Before you call, and Get a virtual key with Create a test key](images/onboarding-6-grant-and-key.png)

## 7. Send a test request

Click **Send request** to get the model's answer and the member that served it. **Send a request**,
further down, has the client code for curl, Python, JavaScript and PowerShell.

![Try it here: 200, the call succeeded, answered by the pool's primary member pool-aoai-westus3-gpt-5-6-luna-dz](images/onboarding-7-test-call.png)

## 8. Watch it work

The **Metrics** tab shows requests, availability, 429s and latency, and each member's share of the
traffic. **Routing** simulates a member failing.

![Metrics tab: requests, availability, 429s, latency and each member's traffic share](images/onboarding-8-metrics.png)

## If you're stuck

**Permission check** in the top bar re-runs the setup checks and writes a request for your
Azure admin for what your rights can't fix, and `python scripts/doctor.py` explains machine and
network problems. More in [If something gets in the way](getting-started.md#if-something-gets-in-the-way).
