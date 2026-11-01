"""A user's first run with Azure API Management, against real Azure: create a BasicV2 service
from the setup wizard, turn on its managed identity and grant it Foundry access, pool the
Foundry deployments behind an API, call it (the circuit breaker moves past the member that
can't answer), load-test it, watch it, remove the pool (its API goes, the policy is the app's
no more) and delete the service (soft-deleted; teardown purges it). About 30 minutes."""

from __future__ import annotations

from typing import Any

import journeys
import pytest
from conftest import Journey, journey
from harness import az, purge_apim

trip = journey("apim")


@pytest.mark.incremental
class TestApim:
    def test_first_run_asks_for_a_gateway(self, trip: Journey) -> None:
        journeys.first_run_asks_for_a_gateway(trip)
        trip.state["pool"] = f"e2e-{trip.env.run_id}"
        trip.state["teardown"] = [lambda: purge_apim(trip.env, trip.gateway)]

    def test_creating_a_basicv2_service(self, trip: Journey) -> None:
        body = {
            "tenant_id": trip.env.tenant_id,
            "subscription_id": trip.env.subscription_id,
            "resource_group": trip.group,
            "location": trip.env.location,
            "name": trip.gateway,
            "sku": "BasicV2",
            "publisher_email": trip.env.publisher_email,
            "publisher_name": "SteadyApiPool e2e",
        }
        created = trip.api.post("/apim/target/services", body)
        assert created["name"] == trip.gateway and created["managed"] is True, created
        service = trip.api.wait(
            "the APIM service to be created",
            lambda: next(
                (
                    s
                    for s in trip.api.get(
                        "/apim/target/services",
                        tenant_id=trip.env.tenant_id,
                        subscription_id=trip.env.subscription_id,
                    )
                    if s["name"] == trip.gateway
                ),
                {"provisioning_state": "missing"},
            ),
            lambda found: str(found["provisioning_state"]).casefold() == "succeeded",
            timeout=3600,
            every=60,
            failed=lambda found: (
                "it failed" if str(found["provisioning_state"]).casefold() == "failed" else None
            ),
        )
        assert service["sku"] == "BasicV2"
        assert trip.api.get("/target")["product"] == "apim"

    def test_setup_turns_on_the_identity_and_grants_foundry(
        self, trip: Journey, foundry: list[dict[str, str]]
    ) -> None:
        trip.api.post("/apim/setup/apim-identity")
        report = journeys.setup_is_ready(trip, foundry)
        assert report["product"] == "apim"
        identity = az("apim", "show", "--resource-group", trip.group, "--name", trip.gateway)
        assert identity["identity"]["type"].startswith("SystemAssigned"), identity["identity"]

    def test_a_failover_pool_behind_an_api(
        self, trip: Journey, foundry: list[dict[str, str]]
    ) -> None:
        deployments = journeys.discovered(trip, foundry)
        members = journeys.import_members(trip, foundry)
        trip.state["members"] = members
        journeys.failover_pool(trip, "apim", members, deployments[0]["model_name"])
        pool = trip.state["pool"]
        api = trip.api.post(f"/apim/pools/{pool}/api", {"path": pool})
        trip.state["api"] = api["id"]
        policy = trip.api.get(f"/apim/apis/{api['id']}/policy")
        assert "apim-backend-pool:inbound:begin" in str(policy), policy

    def test_a_test_call_gets_past_the_member_that_cant_answer(self, trip: Journey) -> None:
        api = trip.state["api"]
        key = trip.api.post(f"/apim/apis/{api}/keys")
        secret = trip.api.post(f"/apim/apis/{api}/keys/{key['id']}/secret")["primary_key"]
        trip.state["key"] = secret
        result = journeys.answered(trip, lambda: self._call(trip))
        # APIM names no member; the answering deployment's region says it was Foundry.
        assert result["headers"].get("x-ms-region"), result["headers"]

    def test_a_short_load_test(self, trip: Journey) -> None:
        api = trip.state["api"]
        start = {
            "spec": {
                "users": 2,
                "duration_seconds": 20,
                "max_tokens": 16,
                "stream": True,
            },
            # APIM's body names it as its test call does (the API's subscription key).
            "subscription_key": trip.state["key"],
        }
        journeys.load_test(trip, f"/apim/apis/{api}/benchmarks", start)

    def test_the_monitor_and_gateway_tabs_show_it(self, trip: Journey) -> None:
        metrics = journeys.monitor_shows_traffic(trip, "apim")
        assert metrics["gateway"]["requests"] > 0
        journeys.gateway_panel(trip, "apim")

    def test_removing_the_pool_deletes_its_api(self, trip: Journey) -> None:
        api = trip.state["api"]
        removed = journeys.remove_pool(trip, "apim")
        assert api in str(removed), removed
        apis = {a["id"] for a in trip.api.get("/apim/apis")}
        assert api not in apis

    def test_deleting_the_service_soft_deletes_it(self, trip: Journey) -> None:
        select = {
            "tenant_id": trip.env.tenant_id,
            "subscription_id": trip.env.subscription_id,
            "resource_group": trip.group,
            "apim_name": trip.gateway,
        }
        check = trip.api.post("/apim/target/services/delete-check", select)
        assert check, check
        trip.api.post(
            "/apim/target/services/delete",
            {**select, "confirm_name": trip.gateway},
            expect=204,
        )
        assert trip.api.get("/target")["product"] is None
        deleted = trip.api.wait(
            "the service to be listed as deleted",
            lambda: trip.api.get(
                "/apim/target/deleted",
                tenant_id=trip.env.tenant_id,
                subscription_id=trip.env.subscription_id,
            ),
            lambda found: trip.gateway in str(found),
            timeout=1800,
            every=30,
        )
        assert trip.gateway in str(deleted)

    # -- helpers ---------------------------------------------------------------------------

    @staticmethod
    def _call(trip: Journey) -> dict[str, Any]:
        body = {
            "path": "chat/completions",
            "body": {"messages": journeys.CHAT, "max_completion_tokens": 16},
            "subscription_key": trip.state["key"],
        }
        result: dict[str, Any] = trip.api.post(f"/apim/apis/{trip.state['api']}/test", body)
        return result
