"""Steps every live journey takes, whatever the gateway: what a user does in the app after the
gateway exists. Each takes the journey (conftest.Journey) and asserts as it goes.

Pools are three members: an endpoint that can't answer (a host that doesn't resolve) first,
then the two long-lived Foundry deployments. Every call to the pool therefore fails over for
real, without touching a real deployment: LiteLLM retries on the next member in the same call,
APIM's circuit breaker opens on the first failure and the next calls go to the next member.
"""

from __future__ import annotations

from typing import Any

from harness import Failure

DOWN = "e2e-down"
DOWN_HOST = "e2e-down.invalid"
CHAT = [{"role": "user", "content": "Reply with the single word: pong"}]


def id_of(item: Any) -> str:
    """A plan entry's name: a bare string, or a view with its name or ID."""
    if isinstance(item, str):
        return item
    return str(item.get("alias") or item.get("name") or item.get("id"))


# The LiteLLM gateways a journey deploys (E2E_GATEWAY_SIZE), and the replicas each plan has.
SIZES: dict[str, dict[str, Any]] = {
    "trial": {
        "peak_rpm": 60,
        "input_tokens": 500,
        "output_tokens": 100,
        "timeout_seconds": 120,
        "zone_redundant": False,
    },
    "small": {
        "peak_rpm": 600,
        "input_tokens": 500,
        "output_tokens": 100,
        "timeout_seconds": 120,
        "zone_redundant": False,
    },
}
REPLICAS = {"trial": 1, "small": 2}


def sizing(trip: Any) -> dict[str, Any]:
    size = trip.env.gateway_size
    if size not in SIZES:
        raise Failure(f"E2E_GATEWAY_SIZE must be one of {', '.join(SIZES)}, not {size}")
    return dict(SIZES[size])


def deploy_failure(status: dict[str, Any]) -> str | None:
    """Why a gateway's deployment failed, with every step that did: None while it hasn't."""
    if status["state"] != "failed":
        return None
    steps = "; ".join(
        f"{step['resource_type']}/{step['name']}: {step['error']}"
        for step in status.get("steps") or []
        if step.get("error")
    )
    return f"{status.get('error')} (steps: {steps or 'none'})"


def pick_region(trip: Any, create: Any) -> str:
    """The region check, as a user meets it: the configured region if it fits, else the first of
    the regions the check offers (subscriptions differ in where they may create PostgreSQL)."""
    trip.state["location"] = trip.env.location
    found = trip.api.post("/litellm/target/gateways/preflight", create(trip))
    if found["problems"]:
        offered = found.get("open_regions") or []
        assert offered, f"no region fits: {found['problems']}"
        print(f"\n{trip.env.location} doesn't fit ({found['problems'][0]}); using {offered[0]}")
        trip.state["location"] = offered[0]
        found = trip.api.post("/litellm/target/gateways/preflight", create(trip))
    assert found["problems"] == [], found
    assert found["location"] == trip.state["location"]
    region: str = trip.state["location"]
    return region


def first_run_asks_for_a_gateway(trip: Any) -> None:
    assert trip.api.get("/config")["mode"] == "local"
    report = trip.api.get("/setup")
    assert report["mode"] == "local" and report["product"] is None, report
    assert report["can_operate"] is False
    sign_in = next(check for check in report["checks"] if check["kind"] == "sign_in")
    assert sign_in["status"] == "ok", sign_in
    subscriptions = trip.api.get("/target/subscriptions", tenant_id=trip.env.tenant_id)
    assert trip.env.subscription_id in {s["subscription_id"] for s in subscriptions}, subscriptions


def setup_is_ready(trip: Any, foundry: list[dict[str, str]]) -> dict[str, Any]:
    """Grants the gateway's identity Foundry access (setup's fix, with the user's rights), then
    waits for setup to have nothing left to do."""
    accounts = [ref["account_id"] for ref in foundry]
    trip.api.post("/setup/foundry-access/grant", {"account_ids": accounts})

    def problems(report: dict[str, Any]) -> list[str]:
        return [
            f"{check['id']}: {check['status']} {check.get('detail') or ''}".strip()
            for check in report["checks"]
            if check["status"] not in {"ok", "skipped"}
        ]

    report: dict[str, Any] = trip.api.wait(
        "setup to be ready",
        lambda: trip.api.get("/setup"),
        lambda found: bool(found["can_operate"]) and not problems(found),
        timeout=900,
        every=20,
        failed=lambda found: "; ".join(p for p in problems(found) if ": blocked" in p) or None,
    )
    return report


def discovered(trip: Any, foundry: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The Foundry deployments as discovery lists them (it scans the subscription)."""
    wanted = {(ref["account_id"].casefold(), ref["deployment"]) for ref in foundry}

    def ours(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [d for d in found if (d["account_id"].casefold(), d["deployment"]) in wanted]

    found = trip.api.wait(
        "discovery to list the Foundry deployments",
        lambda: ours(trip.api.get("/deployments", refresh="true")),
        lambda listed: len(listed) == len(wanted),
        timeout=300,
    )
    models = {d["model_name"] for d in found}
    assert len(models) == 1, f"the Foundry deployments serve different models: {models}"
    return list(found)


def import_members(trip: Any, foundry: list[dict[str, str]]) -> list[str]:
    imported = trip.api.post("/deployments/import", {"deployments": foundry})
    members = list(imported["backend_ids"])
    assert len(members) == 2, imported
    return members


def failover_pool(trip: Any, product: str, members: list[str], model: str) -> dict[str, Any]:
    """The pool: the endpoint that can't answer first, then the two Foundry members."""
    if product == "litellm":
        down = {
            "title": "Can't answer (e2e)",
            "url": f"https://{DOWN_HOST}/v1",
            "model": model,
            "auth": {"type": "none"},
        }
    else:
        down = {"title": "Can't answer (e2e)", "url": f"https://{DOWN_HOST}/openai"}
    trip.api.put(f"/{product}/backends/{DOWN}", down)
    order = [DOWN, *members]
    check = trip.api.post(f"/{product}/pools/model-check", {"members": order})
    assert check["ok"], check
    pool_id = trip.state["pool"]
    body = {
        "title": "E2E failover",
        "members": [
            {"backend_id": member, "priority": index, "weight": 1}
            for index, member in enumerate(order)
        ],
    }
    pool: dict[str, Any] = trip.api.put(f"/{product}/pools/{pool_id}", body)
    assert [m["backend_id"] for m in pool["members"]] == order, pool
    return pool


def answered(trip: Any, call: Any, *, timeout: float = 900) -> dict[str, Any]:
    """Calls until the pool answers 200. The first minutes can refuse: Azure takes a while to
    apply the role the gateway's identity was just granted, and APIM's breaker needs a failure
    on the endpoint that can't answer before it moves on."""
    result: dict[str, Any] = trip.api.wait(
        "the pool to answer a test call",
        call,
        lambda found: found["status"] == 200,
        timeout=timeout,
        every=20,
    )
    return result


def load_test(trip: Any, path: str, start: dict[str, Any]) -> dict[str, Any]:
    """A short load test through the app (billed like real traffic: two users, 20 s)."""
    run = trip.api.post(path, start)
    done: dict[str, Any] = trip.api.wait(
        "the load test to finish",
        lambda: trip.api.get(f"{path}/{run['id']}"),
        lambda found: found["state"] != "running",
        timeout=180,
        every=5,
    )
    assert done["state"] == "done", done
    summary = done["summary"]
    assert summary["requests"] > 0 and summary["successes"] > 0, summary
    return done


def monitor_shows_traffic(trip: Any, product: str) -> dict[str, Any]:
    """The Monitor tab: the gateway's numbers reach Azure Monitor / Application Insights a few
    minutes after the calls."""
    pool_id = trip.state["pool"]
    metrics: dict[str, Any] = trip.api.wait(
        "the Monitor tab to show the pool's traffic",
        lambda: trip.api.get(f"/{product}/pools/{pool_id}/metrics", range="1h", refresh="true"),
        lambda found: bool(found.get("gateway")) and found["gateway"]["requests"] > 0,
        timeout=1200,
        every=60,
        failed=lambda found: (
            found.get("gateway_error") if found.get("gateway_error") == "forbidden" else None
        ),
    )
    return metrics


def gateway_panel(trip: Any, product: str) -> None:
    """The Gateway metrics tab and the gateway panel's cost."""
    load = trip.api.get(f"/{product}/metrics", range="1h")
    assert "traffic" in load and "series" in load, load
    cost = trip.api.get(f"/{product}/cost")
    if cost["list_price"] is None:
        raise Failure(f"no list price for the gateway: {cost}")


def remove_pool(trip: Any, product: str) -> dict[str, Any]:
    """Removal: the read-only plan, then exactly that plan."""
    pool_id = trip.state["pool"]
    plan = trip.api.get(f"/{product}/pools/{pool_id}/removal")
    assert not plan.get("blocked"), plan
    if product == "litellm":
        confirm = {
            "pools": plan["pools"],
            "delete_aliases": [id_of(a) for a in plan["delete_aliases"]],
            "unbind_aliases": [id_of(a) for a in plan["unbind_aliases"]],
            "delete_keys": [id_of(k) for k in plan["delete_keys"]],
        }
    else:
        confirm = {
            "pools": plan["pools"],
            "delete_apis": [id_of(a) for a in plan["delete_apis"]],
            "unbind_apis": [id_of(a) for a in plan["unbind_apis"]],
        }
    result: dict[str, Any] = trip.api.post(f"/{product}/pools/{pool_id}/remove", confirm)
    assert result["pools"] == [pool_id], result
    assert trip.api.status("GET", f"/{product}/pools/{pool_id}") == 404
    return result
