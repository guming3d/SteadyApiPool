"""Sets up the gateways under test through the app's own API, as a user would in its UI.

    python perf/gateways.py apim <resource-group> <service> <label>
    python perf/gateways.py litellm <resource-group> <gateway> <label>

Run against a local app (``--app``, default http://127.0.0.1:8001) signed in to the rig's
subscription. It selects the gateway (waiting for an APIM service to finish provisioning, and
turning on its managed identity), then creates two pools the way the app does:

- ``perf-mock``: the three mocks (``mock-a`` first, then ``mock-b``, ``mock-c``), managed
  identity auth, the app's default breaker or cooldown and retries;
- ``perf-luna``: the gpt-6-luna deployments in ``LUNA`` in that order, after granting the
  gateway's identity *Cognitive Services OpenAI User* on their accounts.

It creates each pool's endpoint (APIM: an API per pool; LiteLLM: the pool is the model) and its
test key, puts the key on the load VM (``ctl.py key``) and prints the plan's ``targets`` entries.
Keys go only to the VM; nothing here writes one to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TENANT = "0554bec6-a796-4bcb-b8c4-313ad91e1aec"
SUBSCRIPTION = "b9da3a59-1b01-4211-b510-cdbc0790ed2c"
API_VERSION = "2025-04-01-preview"
_RG = f"/subscriptions/{SUBSCRIPTION}/resourceGroups"
# (account ID, deployment), in failover order: the nearest region first.
LUNA = [
    (
        f"{_RG}/minggu-2026/providers/Microsoft.CognitiveServices/accounts/openaiminggu",
        "gpt-6-luna",
    ),
    (
        f"{_RG}/minggu-2026/providers/Microsoft.CognitiveServices/accounts/control-plane-test",
        "gpt-6-luna",
    ),
    (
        f"{_RG}/rg-minggu-reid/providers/Microsoft.CognitiveServices/accounts/ai-minggu3566ai776846365708",
        "gpt-6-luna-dz",
    ),
    (
        f"{_RG}/minggu-2026/providers/Microsoft.CognitiveServices/accounts/minggu-luna-eu-22610",
        "gpt-6-luna-dz",
    ),
]
MOCKS = ["mock-a", "mock-b", "mock-c"]


class App:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body: Any = None, query: str = "") -> Any:
        url = f"{self.base}/api{path}{'?' + query if query else ''}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Pool-Manager", "1")
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise SystemExit(
                f"{method} {path}: {error.code} {error.read().decode(errors='replace')[:800]}"
            ) from None
        return json.loads(raw) if raw else None


def rig() -> dict[str, str]:
    name = os.environ.get("PERF_RIG")
    return dict(json.loads((HERE / (f"rig-{name}.json" if name else "rig.json")).read_text()))


def put_key(name: str, value: str) -> None:
    subprocess.run(
        [sys.executable, str(HERE / "ctl.py"), "key", name], input=value, text=True, check=True
    )


def mock_url(name: str) -> str:
    domain = rig()["mock_domain"]
    return f"https://{name}.{domain}/openai/deployments/gpt-6-luna"


def members(backend_ids: list[str]) -> list[dict[str, int | str]]:
    return [
        {"backend_id": backend, "priority": i, "weight": 1} for i, backend in enumerate(backend_ids)
    ]


def import_luna(app: App) -> list[str]:
    app.call("GET", "/deployments", query="refresh=true")
    refs = [{"account_id": account, "deployment": deployment} for account, deployment in LUNA]
    return list(app.call("POST", "/deployments/import", {"deployments": refs})["backend_ids"])


def grant(app: App) -> None:
    accounts = sorted({account for account, _ in LUNA})
    for check in app.call("POST", "/setup/foundry-access/grant", {"account_ids": accounts}):
        print("  grant:", check.get("id"), check.get("status"), (check.get("detail") or "")[:120])


def apim(app: App, resource_group: str, service: str, label: str) -> dict[str, Any]:
    target = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": resource_group,
        "apim_name": service,
    }
    while True:
        services = app.call(
            "GET",
            "/apim/target/services",
            query=f"subscription_id={SUBSCRIPTION}&tenant_id={TENANT}",
        )
        state = next((s["provisioning_state"] for s in services if s["name"] == service), None)
        print(f"{service}: {state}", flush=True)
        if state == "Succeeded":
            break
        time.sleep(60)
    app.call("PUT", "/apim/target", target)
    print("  identity:", app.call("POST", "/apim/setup/apim-identity").get("status"))
    for name in MOCKS:
        app.call(
            "PUT",
            f"/apim/backends/{name}",
            {"title": name, "url": mock_url(name), "auth": {"type": "managed_identity"}},
        )
    pools = {"perf-mock": MOCKS, "perf-luna": import_luna(app)}
    grant(app)
    gateway = app.call("GET", "/apim/capabilities")["gateway_url"].rstrip("/")
    targets: dict[str, Any] = {}
    for pool, backend_ids in pools.items():
        app.call("PUT", f"/apim/pools/{pool}", {"title": pool, "members": members(backend_ids)})
        api = app.call("POST", f"/apim/pools/{pool}/api", {"path": pool})
        key_view = app.call("POST", f"/apim/apis/{api['id']}/keys")
        secret = app.call("POST", f"/apim/apis/{api['id']}/keys/{key_view['id']}/secret")[
            "primary_key"
        ]
        name = f"{label}-{pool}"
        put_key(name, secret)
        targets[name] = {
            "url": f"{gateway}/{api['path']}/chat/completions",
            "query": f"api-version={API_VERSION}",
            "model": "gpt-6-luna",
            "key_source": f"file:{name}",
            "key_header": api["subscription_key_header"],
            "member_header": "x-ms-region",
            "timeout": 300,
        }
    return targets


def litellm(app: App, resource_group: str, gateway: str, label: str) -> dict[str, Any]:
    target = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": resource_group,
        "gateway_name": gateway,
    }
    app.call("PUT", "/litellm/target", target)
    for name in MOCKS:
        app.call(
            "PUT",
            f"/litellm/backends/{name}",
            {"title": name, "url": mock_url(name), "auth": {"type": "managed_identity"}},
        )
    pools = {"perf-mock": MOCKS, "perf-luna": import_luna(app)}
    grant(app)
    targets: dict[str, Any] = {}
    for pool, backend_ids in pools.items():
        existing = {p["id"] for p in app.call("GET", "/litellm/pools")}
        if pool not in existing:
            body = {"title": pool, "members": members(backend_ids)}
            app.call("PUT", f"/litellm/pools/{pool}", body)
        endpoint = app.call("GET", f"/litellm/pools/{pool}/endpoint")
        secret = app.call("POST", f"/litellm/pools/{pool}/keys")["key"]
        name = f"{label}-{pool}"
        put_key(name, secret)
        targets[name] = {
            "url": f"{endpoint['base_url'].rstrip('/')}/chat/completions",
            "model": pool,
            "key_source": f"file:{name}",
            "key_header": "Authorization",
            "member_header": "x-litellm-model-id",
            "timeout": 300,
        }
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("product", choices=["apim", "litellm"])
    parser.add_argument("resource_group")
    parser.add_argument("name")
    parser.add_argument("label")
    parser.add_argument("--app", default="http://127.0.0.1:8001")
    args = parser.parse_args(argv)
    app = App(args.app)
    setup = apim if args.product == "apim" else litellm
    targets = setup(app, args.resource_group, args.name, args.label)
    out = HERE / "plans" / f"targets-{args.label}.json"
    out.write_text(json.dumps(targets, indent=2))
    print(json.dumps(targets, indent=2))
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
