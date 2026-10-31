"""A user's first run with a LiteLLM gateway on Azure Container Apps, against real Azure:
deploy it from the setup wizard, fix setup, pool the Foundry deployments, call the pool (with a
failover), load-test it, watch it in the Monitor tab, plan a resize, remove the pool and delete
the gateway. About 25 minutes; under a dollar."""

from __future__ import annotations

from typing import Any

import journeys
import pytest
from conftest import Journey, journey
from harness import az

trip = journey("aca")


@pytest.mark.incremental
class TestLiteLLMOnContainerApps:
    def test_first_run_asks_for_a_gateway(self, trip: Journey) -> None:
        journeys.first_run_asks_for_a_gateway(trip)
        trip.state["pool"] = f"e2e-{trip.env.run_id}"

    def test_the_sizer_plans_and_the_region_check_passes(self, trip: Journey) -> None:
        plans = trip.api.post("/litellm/target/gateways/plan", journeys.sizing(trip))
        assert plans["recommended"] == "aca", plans
        expected = journeys.REPLICAS[trip.env.gateway_size]
        assert plans["aca"]["max_replicas"] == expected and not plans["aca"]["problems"], plans
        journeys.pick_region(trip, self._create)

    def test_deploying_the_gateway_succeeds(self, trip: Journey) -> None:
        created = trip.api.post("/litellm/target/gateways", self._create(trip))
        assert created["name"] == trip.gateway
        status = trip.api.wait(
            "the gateway's deployment",
            lambda: trip.api.get(
                "/litellm/target/deploy-status",
                tenant_id=trip.env.tenant_id,
                subscription_id=trip.env.subscription_id,
                resource_group=trip.group,
                name=trip.gateway,
            ),
            lambda found: found["state"] == "succeeded",
            timeout=2400,
            every=30,
            failed=lambda found: journeys.deploy_failure(found),
        )
        assert status["state"] == "succeeded"
        target = trip.api.get("/target")
        assert (target["product"], target["gateway_name"]) == (
            "litellm",
            trip.gateway,
        ), target

    def test_what_was_deployed_is_what_the_plan_said(self, trip: Journey) -> None:
        app = az(
            "containerapp",
            "show",
            "--resource-group",
            trip.group,
            "--name",
            trip.gateway,
        )
        assert app["tags"]["managed-by"] == "ai-gateway-pool"
        env = {
            e["name"]: e.get("value") for e in app["properties"]["template"]["containers"][0]["env"]
        }
        fqdn = app["properties"]["configuration"]["ingress"]["fqdn"]
        # The production checklist and the review's fixes, on the real Container App.
        assert env["PROXY_BASE_URL"] == f"https://{fqdn}"
        assert env["GRACEFUL_SHUTDOWN_TIMEOUT"] == "240"
        assert app["properties"]["template"]["terminationGracePeriodSeconds"] == 270
        assert app["properties"]["configuration"]["ingress"]["allowInsecure"] is False

    def test_setup_fixes_foundry_access(self, trip: Journey, foundry: list[dict[str, str]]) -> None:
        report = journeys.setup_is_ready(trip, foundry)
        assert report["product"] == "litellm"
        capabilities = trip.api.get("/litellm/capabilities")
        assert capabilities["gateway_kind"] == "aca" and capabilities["gateway_url"]

    def test_a_failover_pool_of_the_foundry_deployments(
        self, trip: Journey, foundry: list[dict[str, str]]
    ) -> None:
        deployments = journeys.discovered(trip, foundry)
        members = journeys.import_members(trip, foundry)
        trip.state["members"] = members
        pool = journeys.failover_pool(trip, "litellm", members, deployments[0]["model_name"])
        config = trip.api.get(f"/litellm/pools/{pool['id']}/config")
        assert f"model_name: {pool['id']}" in config["yaml"]

    def test_a_test_call_fails_over_to_foundry(self, trip: Journey) -> None:
        pool = trip.state["pool"]
        key = trip.api.post(f"/litellm/pools/{pool}/keys")["key"]
        trip.state["key"] = key
        result = journeys.answered(trip, lambda: self._call(trip, pool))
        answered_by = result["headers"].get("x-litellm-model-id", "")
        assert answered_by in {f"{pool}--{m}" for m in trip.state["members"]}, result
        assert journeys.DOWN not in answered_by

    def test_an_alias_reaches_the_pool(self, trip: Journey) -> None:
        pool = trip.state["pool"]
        alias = f"{pool}-alias"
        endpoint = trip.api.post(f"/litellm/pools/{pool}/aliases", {"name": alias})
        assert endpoint["model_names"] == [pool, alias] and endpoint["base_url"].startswith(
            "https://"
        )
        assert self._call(trip, pool, model=alias)["status"] == 200

    def test_a_short_load_test(self, trip: Journey) -> None:
        pool = trip.state["pool"]
        start = {
            "spec": {
                "users": 2,
                "duration_seconds": 20,
                "max_tokens": 16,
                "stream": True,
            },
            "key": trip.state["key"],
        }
        run = journeys.load_test(trip, f"/litellm/pools/{pool}/benchmarks", start)
        assert run["summary"]["ttft_p50_ms"] is not None

    def test_the_monitor_and_gateway_tabs_show_it(self, trip: Journey) -> None:
        metrics = journeys.monitor_shows_traffic(trip, "litellm")
        assert metrics["gateway"]["success"] > 0
        journeys.gateway_panel(trip, "litellm")

    def test_a_resize_is_planned_without_changing_anything(self, trip: Journey) -> None:
        before = az(
            "containerapp",
            "show",
            "--resource-group",
            trip.group,
            "--name",
            trip.gateway,
        )
        plan = trip.api.post(
            "/litellm/gateway/resize/plan", {**journeys.sizing(trip), "peak_rpm": 3000}
        )
        assert (
            plan["etag"] and plan["sized_for"]["peak_rpm"] == journeys.sizing(trip)["peak_rpm"]
        ), plan
        after = az(
            "containerapp",
            "show",
            "--resource-group",
            trip.group,
            "--name",
            trip.gateway,
        )
        assert (
            before["properties"]["latestRevisionName"] == after["properties"]["latestRevisionName"]
        )

    def test_removing_the_pool_takes_its_alias_and_key(self, trip: Journey) -> None:
        removed = journeys.remove_pool(trip, "litellm")
        assert removed["deleted_aliases"] == [f"{trip.state['pool']}-alias"], removed

    def test_deleting_the_gateway_removes_its_resources(self, trip: Journey) -> None:
        select = {
            "tenant_id": trip.env.tenant_id,
            "subscription_id": trip.env.subscription_id,
            "resource_group": trip.group,
            "gateway_name": trip.gateway,
        }
        check = trip.api.post("/litellm/target/gateways/delete-check", select)
        assert check["current"] is True and check["resources"], check
        trip.api.post(
            "/litellm/target/gateways/delete",
            {**select, "confirm_name": trip.gateway},
            expect=204,
        )
        assert trip.api.get("/target")["product"] is None
        # (az takes no --tag with --resource-group: filtered here.)
        left = [
            r
            for r in az("resource", "list", "--resource-group", trip.group)
            if (r.get("tags") or {}).get("ai-gateway") == trip.gateway
        ]
        # Asked for; the environment can take a while, and the group goes in teardown.
        assert all(r["type"] != "Microsoft.App/containerApps" for r in left), left

    # -- helpers ---------------------------------------------------------------------------

    @staticmethod
    def _create(trip: Journey) -> dict[str, Any]:
        return {
            "tenant_id": trip.env.tenant_id,
            "subscription_id": trip.env.subscription_id,
            "resource_group": trip.group,
            "location": trip.state.get("location", trip.env.location),
            "name": trip.gateway,
            "sizing": journeys.sizing(trip),
            "platform": "aca",
        }

    @staticmethod
    def _call(trip: Journey, pool: str, model: str | None = None) -> dict[str, Any]:
        body = {
            "key": trip.state["key"],
            "body": {
                "model": model or pool,
                "messages": journeys.CHAT,
                "max_tokens": 16,
            },
        }
        result: dict[str, Any] = trip.api.post(f"/litellm/pools/{pool}/test", body)
        return result
