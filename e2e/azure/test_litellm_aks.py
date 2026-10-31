"""A user's first run with a LiteLLM gateway on a new AKS cluster, against real Azure: the
cluster, the install through Run Command, HTTPS with a Let's Encrypt certificate, then a pool
with a failover, a test call through the cluster's HTTPS address, a resize plan and deleting
it. 45-60 minutes and a few dollars: run weekly (docs/testing.md)."""

from __future__ import annotations

from typing import Any

import journeys
import pytest
from conftest import Journey, journey
from harness import az

trip = journey("aks")


@pytest.mark.incremental
class TestLiteLLMOnAks:
    def test_first_run_asks_for_a_gateway(self, trip: Journey) -> None:
        journeys.first_run_asks_for_a_gateway(trip)
        trip.state["pool"] = f"e2e-{trip.env.run_id}"

    def test_the_aks_plan_and_its_region_check(self, trip: Journey) -> None:
        plans = trip.api.post("/litellm/target/gateways/plan", journeys.sizing(trip))
        assert plans["aks"]["aks"]["node_min"] >= 1 and not plans["aks"]["problems"], plans
        journeys.pick_region(trip, self._create)

    def test_deploying_installs_litellm_behind_https(self, trip: Journey) -> None:
        created = trip.api.post("/litellm/target/gateways", self._create(trip))
        assert created["platform"] == "aks" and created["url"].startswith("https://"), created
        phases: list[str] = []

        def status() -> dict[str, Any]:
            found: dict[str, Any] = trip.api.get(
                "/litellm/target/deploy-status",
                tenant_id=trip.env.tenant_id,
                subscription_id=trip.env.subscription_id,
                resource_group=trip.group,
                name=trip.gateway,
            )
            if not phases or phases[-1] != found["phase"]:
                phases.append(found["phase"])
            return found

        done = trip.api.wait(
            "the AKS gateway's deployment, install and certificate",
            status,
            lambda found: found["state"] == "succeeded" and found["phase"] == "done",
            timeout=4500,
            every=30,
            failed=lambda found: (
                f"{found['phase']}: {found.get('error')}\n{found.get('log_tail')}"
                if found["state"] == "failed"
                else None
            ),
        )
        assert done["phase"] == "done"
        # Each phase in turn (a fast one may pass between two polls).
        assert phases[-1] == "done" and "cluster" in phases, phases
        trip.state["url"] = created["url"]

    def test_the_cluster_is_what_the_plan_said(self, trip: Journey) -> None:
        cluster = az("aks", "show", "--resource-group", trip.group, "--name", trip.gateway)
        assert cluster["aadProfile"]["enableAzureRbac"] is True
        assert cluster["disableLocalAccounts"] is True
        assert cluster["tags"]["managed-by"] == "ai-gateway-pool"
        # The deploying identity (a service principal in CI) got cluster admin.
        assignments = az(
            "role",
            "assignment",
            "list",
            "--scope",
            cluster["id"],
            "--role",
            "Azure Kubernetes Service RBAC Cluster Admin",
        )
        assert assignments, "no Azure Kubernetes Service RBAC Cluster Admin on the cluster"

    def test_setup_fixes_foundry_access(self, trip: Journey, foundry: list[dict[str, str]]) -> None:
        report = journeys.setup_is_ready(trip, foundry)
        assert report["product"] == "litellm"

    def test_a_failover_pool_answers_over_https(
        self, trip: Journey, foundry: list[dict[str, str]]
    ) -> None:
        deployments = journeys.discovered(trip, foundry)
        members = journeys.import_members(trip, foundry)
        journeys.failover_pool(trip, "litellm", members, deployments[0]["model_name"])
        pool = trip.state["pool"]
        key = trip.api.post(f"/litellm/pools/{pool}/keys")["key"]
        body = {
            "key": key,
            "body": {"model": pool, "messages": journeys.CHAT, "max_tokens": 16},
        }
        result = journeys.answered(trip, lambda: trip.api.post(f"/litellm/pools/{pool}/test", body))
        assert result["url"].startswith(trip.state["url"]), result["url"]
        answered_by = result["headers"].get("x-litellm-model-id", "")
        assert answered_by in {f"{pool}--{m}" for m in members}, result

    def test_a_resize_is_planned_on_the_cluster(self, trip: Journey) -> None:
        plan = trip.api.post(
            "/litellm/gateway/resize/plan", {**journeys.sizing(trip), "peak_rpm": 3000}
        )
        assert plan["platform"] == "aks" and plan["etag"], plan

    def test_removing_the_pool_and_deleting_the_gateway(self, trip: Journey) -> None:
        journeys.remove_pool(trip, "litellm")
        select = {
            "tenant_id": trip.env.tenant_id,
            "subscription_id": trip.env.subscription_id,
            "resource_group": trip.group,
            "gateway_name": trip.gateway,
        }
        trip.api.post(
            "/litellm/target/gateways/delete",
            {**select, "confirm_name": trip.gateway},
            expect=204,
        )
        assert trip.api.get("/target")["product"] is None

    @staticmethod
    def _create(trip: Journey) -> dict[str, Any]:
        return {
            "tenant_id": trip.env.tenant_id,
            "subscription_id": trip.env.subscription_id,
            "resource_group": trip.group,
            "location": trip.state.get("location", trip.env.location),
            "name": trip.gateway,
            "sizing": journeys.sizing(trip),
            "platform": "aks",
        }
