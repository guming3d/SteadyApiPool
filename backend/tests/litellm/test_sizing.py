"""Sizing a gateway from its traffic: the arithmetic, the platform rules, and the plan's way into
the templates, the region check and the price."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pool_manager.app import app
from pool_manager.litellm import sizing
from pool_manager.litellm.gateway_template import (
    TRUSTED_PROXY_RANGES,
    InstallSize,
    aca_parameters,
    aks_parameters,
    litellm_config,
    parse_sizing_tag,
    sizing_tag,
)
from pool_manager.litellm.models import GatewaySizing
from pool_manager.litellm.sizing import ACA_MAX_REPLICAS, node_in_family, node_pods, plan, plans
from tests.fake_azure import SUBSCRIPTION, TENANT, FakeAzure
from tests.litellm.test_app import PRODUCTION, TRIAL, WRITE
from tests.litellm.test_target import NO_TARGET, _settings, _Wizard


def _sizing(**values: Any) -> GatewaySizing:
    return GatewaySizing.model_validate({**PRODUCTION, **values})


def _codes(notes: list[Any]) -> list[str]:
    return [note.code for note in notes]


# -- the arithmetic ------------------------------------------------------------------------


def test_the_worked_example() -> None:
    # 6,000 RPM, 2,000 tokens in, 500 out, not streamed, a standard model.
    found = plan(_sizing())

    assert found.platform == "aca"
    assert (found.rps, found.tpm) == (100.0, 15_000_000)
    # 1 s to the first token, 0.2 s reading the prompt, 500 tokens at 60 a second.
    assert found.call_seconds == pytest.approx(9.53, abs=0.01)
    assert (found.in_flight, found.in_flight_slow) == (954, 12_000)
    # 27 ms a call and 0.25 ms for the bodies (measured, calibrate.py).
    assert found.cpu_ms == pytest.approx(27.25)
    assert found.replica_rps == pytest.approx(25.7, abs=0.1)
    assert (found.bound_by, found.peak_replicas) == ("cpu", 4)
    # 25 % headroom and one replica for a zone: 3.89 x 1.25 -> 5, +1.
    assert (found.min_replicas, found.max_replicas) == (2, 6)
    # Container Apps adds a replica past the calls each holds at the peak (954 over 4), or past
    # 60 % of its one worker's core: 27 % of LiteLLM's 2 vCPU and the collector's 0.25.
    assert (found.scale_concurrency, found.scale_cpu_percent) == (239, 27)
    # 6 replicas x 10 connections x 2 while a revision rolls over.
    assert (found.postgres_sku, found.postgres_connections) == ("Standard_D2ds_v5", 120)
    assert found.redis_sku == "Balanced_B0" and not found.redis_buffer
    assert found.bandwidth_mbps == pytest.approx(16.0)
    assert _codes(found.reasons) == ["platform_simple"]
    assert _codes(found.warnings) == ["foundry_quota"]
    assert found.warnings[0].values == {"tpm": 15_000_000, "rpm": 6000}


def test_streaming_dominates_the_gateways_cpu() -> None:
    # The same traffic streamed: each of the 500 chunks costs 0.75 ms, and 0.11 ms more per
    # 1,000 prompt tokens with OpenTelemetry (measured).
    found = plans(_sizing(streaming=True))

    assert found.aca.cpu_ms == pytest.approx(27.25 + 500 * (0.75 + 2 * 0.11))
    assert found.aca.replica_rps == pytest.approx(1.4, abs=0.05)
    assert found.aca.peak_replicas == 74
    # More than a Container Apps gateway is planned for.
    assert found.recommended == "aks"
    assert _codes(found.aks.reasons) == ["platform_scale"]


def test_a_trial_is_one_small_replica_without_redis() -> None:
    found = plan(GatewaySizing.model_validate(TRIAL))

    assert (found.min_replicas, found.max_replicas, found.profile) == (1, 1, "dev")
    assert (found.replica_cpu, found.replica_memory_gib) == (1.0, 2.0)
    assert (found.postgres_sku, found.postgres_tier) == ("Standard_B1ms", "Burstable")
    assert found.redis_sku is None
    assert "single_replica" in _codes(found.warnings)
    # 60 % of a core is 48 % of 1 vCPU and the collector's 0.25 (one replica: it never scales).
    assert found.scale_cpu_percent == 48


def test_zone_redundancy_keeps_two_replicas_and_one_spare() -> None:
    redundant = plan(_sizing(peak_rpm=60))
    single = plan(_sizing(peak_rpm=60, zone_redundant=False))

    assert (redundant.min_replicas, redundant.max_replicas) == (2, 2)
    assert (single.min_replicas, single.max_replicas) == (1, 1)
    # Production sizes for anything with two replicas.
    assert redundant.postgres_tier == "GeneralPurpose" and redundant.redis_sku


def test_headroom_raises_only_the_maximum() -> None:
    none = plan(_sizing(headroom_percent=0))
    double = plan(_sizing(headroom_percent=100))

    assert none.min_replicas == double.min_replicas == 2
    assert (none.max_replicas, double.max_replicas) == (5, 9)


def test_streaming_costs_gateway_cpu() -> None:
    streamed = plan(_sizing(output_tokens=2000, streaming=True))
    whole = plan(_sizing(output_tokens=2000))

    assert streamed.cpu_ms > whole.cpu_ms
    assert streamed.replica_rps < whole.replica_rps


def test_long_calls_with_large_prompts_are_bound_by_the_calls_a_replica_holds() -> None:
    # 100k-token prompts hold megabytes each, for the two minutes a reasoning model takes.
    found = plan(
        _sizing(
            peak_rpm=600,
            input_tokens=100_000,
            output_tokens=4000,
            model_speed="reasoning",
            timeout_seconds=600,
            streaming=False,
        )
    )

    assert found.bound_by == "in_flight"
    assert found.replica_in_flight < 1000


def test_the_response_time_overrides_the_speed_preset() -> None:
    preset = plan(_sizing(model_speed="reasoning"))
    measured = plan(_sizing(model_speed="reasoning", response_seconds=3))

    assert preset.call_seconds > 20
    assert measured.call_seconds == 3
    assert measured.in_flight < preset.in_flight


def test_a_call_longer_than_its_timeout_is_flagged() -> None:
    found = plan(_sizing(output_tokens=10_000, timeout_seconds=60))

    note = next(note for note in found.warnings if note.code == "call_exceeds_timeout")
    assert note.values["timeout"] == 60


def test_the_database_grows_with_the_replicas_then_lowers_the_pool() -> None:
    sizes = [plan(_sizing(peak_rpm=rpm), "aks").postgres_sku for rpm in (6000, 60_000)]
    assert sizes == ["Standard_D2ds_v5", "Standard_D4ds_v5"]
    big = plan(_sizing(peak_rpm=600_000), "aks")
    assert big.postgres_sku == "Standard_D16ds_v5"
    assert big.db_pool < 10
    assert "pool_lowered" in _codes(big.warnings)
    assert big.postgres_connections <= big.postgres_limit


# -- where it runs ---------------------------------------------------------------------------


def test_silent_calls_past_the_ingress_limit_go_to_aks() -> None:
    # Measured: Container Apps' ingress ends a request that sends nothing for 240 s.
    found = plans(_sizing(timeout_seconds=300, streaming=False))

    assert found.recommended == "aks"
    assert "aca_timeout" in _codes(found.aca.problems)
    # A plan that can't run doesn't say it can be deployed anyway.
    assert found.aca.reasons == []
    assert _codes(found.aks.reasons) == ["platform_timeout"]
    assert found.aks.problems == []
    # A route long enough for a call and its retry.
    assert found.aks.route_timeout == 660


def test_streamed_calls_may_run_past_the_ingress_limit() -> None:
    found = plans(_sizing(timeout_seconds=600, streaming=True, peak_rpm=600))

    assert found.recommended == "aca"
    assert found.aca.problems == []
    # The other platform only says it isn't the recommended one.
    assert _codes(found.aks.reasons) == ["platform_chosen"]


def test_a_first_token_later_than_the_ingress_limit_goes_to_aks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model that thinks for over four minutes before it streams anything.
    monkeypatch.setitem(sizing.SPEEDS, "reasoning", (250.0, 40.0))
    found = plans(_sizing(model_speed="reasoning", timeout_seconds=900))

    assert found.recommended == "aks"
    assert "aca_timeout" in _codes(found.aca.problems)


def test_more_replicas_than_container_apps_is_planned_for_go_to_aks() -> None:
    found = plans(_sizing(peak_rpm=60_000))

    assert found.aca.max_replicas > ACA_MAX_REPLICAS
    assert found.recommended == "aks"
    assert _codes(found.aks.reasons) == ["platform_scale"]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [("kubernetes_team", "platform_team"), ("self_hosted_models", "platform_gpu")],
)
def test_a_kubernetes_team_or_gpu_models_choose_aks(answer: str, reason: str) -> None:
    found = plans(_sizing(**{answer: True}))

    assert found.recommended == "aks"
    assert reason in _codes(found.aks.reasons)
    # Container Apps still works; choosing it says so.
    assert found.aca.problems == []
    assert _codes(found.aca.reasons)[0] == "platform_chosen"


def test_private_networking_is_noted() -> None:
    assert "private_manual" in _codes(plan(_sizing(private_network=True)).reasons)
    assert "private_aks" in _codes(
        plan(_sizing(private_network=True, kubernetes_team=True)).reasons
    )


def test_aks_nodes_and_outbound_ports() -> None:
    small = plan(_sizing(), "aks").aks
    assert small is not None
    # Six pods, two a D4ds_v5 node: three nodes across zones, the Standard tier.
    assert (small.node_size, small.node_min, small.node_max, small.tier) == (
        "Standard_D4ds_v5",
        3,
        3,
        "Standard",
    )
    # 12,000 calls slowed to the timeout over three nodes: one IP's ports shared by four.
    assert (small.ports_needed, small.outbound_ips, small.outbound_ports) == (4000, 1, 16000)

    big = plan(_sizing(peak_rpm=30_000), "aks").aks
    assert big is not None
    assert big.node_size == "Standard_D8ds_v5"
    assert big.node_max <= 10
    assert big.outbound_ports >= big.ports_needed or big.outbound_ips > 1

    trial = plan(GatewaySizing.model_validate(TRIAL), "aks").aks
    assert trial is not None
    assert (trial.node_min, trial.node_max, trial.tier) == (1, 1, "Free")


# -- into the templates ----------------------------------------------------------------------


def test_the_plan_becomes_the_templates_parameters() -> None:
    sizing = _sizing()
    found = plan(sizing, "aca")
    values = {
        key: item["value"] for key, item in aca_parameters("gw", "eastus2", sizing, found).items()
    }

    assert (values["minReplicas"], values["maxReplicas"]) == (2, 6)
    assert (values["scaleConcurrency"], values["scaleCpuPercent"]) == (239, 27)
    assert (values["replicaCpu"], values["replicaMemory"]) == ("2.00", "4Gi")
    assert (values["postgresSku"], values["postgresTier"]) == ("Standard_D2ds_v5", "GeneralPurpose")
    assert values["redisSku"] == "Balanced_B0"
    assert "request_timeout: 120" in values["litellmConfig"]
    assert parse_sizing_tag(values["sizing"]) == sizing

    aks = plan(sizing, "aks")
    values = {
        key: item["value"]
        for key, item in aks_parameters(
            name="gw",
            location="eastus2",
            sizing=sizing,
            plan=aks,
            deployer="me",
            zones=["1", "2"],
            postgres_password="x",
        ).items()
    }
    assert (values["nodeMin"], values["nodeMax"], values["clusterTier"]) == (3, 3, "Standard")
    assert values["zones"] == ["1", "2"]
    assert InstallSize.parse(values["install"]) == InstallSize(2, 6, 120, 10, 600)
    assert aks.scale_cpu_percent == 60


def test_tags_from_before_the_plan_fall_back() -> None:
    assert parse_sizing_tag(None) is None
    assert parse_sizing_tag("rpm=10") is None
    assert InstallSize.parse(None, "production") == InstallSize(2, 10)
    assert InstallSize.parse("", "dev") == InstallSize()
    # A cluster installed before spend could go through Redis.
    old = InstallSize.parse("min=2;max=6;timeout=120;pool=10;route=600")
    assert old == InstallSize(2, 6, 120, 10, 600) and not old.buffer
    assert InstallSize.parse(InstallSize(2, 6, buffer=True).tag()).buffer
    assert len(sizing_tag(_sizing(response_seconds=12.5))) <= 256


def test_the_config_follows_the_plan() -> None:
    config = litellm_config(redis=False, timeout=300, db_pool=4)
    assert config["litellm_settings"]["request_timeout"] == 300
    assert config["general_settings"]["database_connection_pool_limit"] == 4


def test_the_config_follows_litellms_production_checklist() -> None:
    config = litellm_config(redis=True)
    general, settings = config["general_settings"], config["litellm_settings"]

    assert general["proxy_batch_write_at"] == 60
    # Failed calls go to the logs and spans, not the spend table.
    assert general["disable_error_logs"] is True
    assert general["trusted_proxy_ranges"] == TRUSTED_PROXY_RANGES
    assert settings["json_logs"] is True
    assert config["router_settings"]["routing_strategy"] == "simple-shuffle"
    # Spend goes through Redis only at LiteLLM's threshold, and only with Redis.
    assert "use_redis_transaction_buffer" not in general
    assert "use_redis_transaction_buffer" not in litellm_config(redis=False, buffer=True)
    buffered = litellm_config(redis=True, buffer=True)["general_settings"]
    assert buffered["use_redis_transaction_buffer"] is True


def test_spend_goes_through_redis_at_litellms_threshold() -> None:
    sizing = _sizing(peak_rpm=60_000)
    found = plan(sizing, "aks")

    assert found.redis_sku and found.redis_buffer
    assert "redis_buffer" in _codes(found.warnings)
    aca = plan(sizing, "aca")
    values = {
        key: item["value"] for key, item in aca_parameters("gw", "eastus2", sizing, aca).items()
    }
    assert "use_redis_transaction_buffer: true" in values["litellmConfig"]
    assert InstallSize.of(found).buffer


# -- through the app -------------------------------------------------------------------------


@pytest.fixture
def azure() -> FakeAzure:
    return FakeAzure()


@pytest.fixture
def wizard(azure: FakeAzure, tmp_path: Path) -> Iterator[_Wizard]:
    yield _Wizard(azure, _settings(**NO_TARGET), tmp_path)
    app.dependency_overrides.clear()


def _body(**overrides: Any) -> dict[str, Any]:
    return {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-new",
        "location": "eastus2",
        "name": "gw-new",
        "sizing": PRODUCTION,
        **overrides,
    }


def test_the_plan_route_reads_nothing(wizard: _Wizard, azure: FakeAzure) -> None:
    before = len(azure.requests)

    response = wizard.client.post(
        "/api/litellm/target/gateways/plan", json=PRODUCTION, headers=WRITE
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recommended"] == "aca"
    assert body["aca"]["max_replicas"] == 6 and body["aks"]["aks"]["node_min"] == 3
    assert len(azure.requests) == before


def test_the_preflight_prices_the_plan_in_the_region(wizard: _Wizard) -> None:
    response = wizard.client.post(
        "/api/litellm/target/gateways/preflight", json=_body(), headers=WRITE
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan"]["max_replicas"] == 6
    kinds = [item["kind"] for item in body["list_price"]["items"]]
    assert kinds == ["container_apps", "postgres", "redis"]
    assert body["list_price"]["items"][0]["detail"] == "2.25 vCPU, 4.5 GiB x 2-6 replicas"
    assert body["problems"] == []


def test_a_plan_that_cant_run_there_is_refused(wizard: _Wizard, azure: FakeAzure) -> None:
    silent = {**PRODUCTION, "timeout_seconds": 300, "streaming": False}

    check = wizard.client.post(
        "/api/litellm/target/gateways/preflight", json=_body(sizing=silent), headers=WRITE
    ).json()
    response = wizard.client.post(
        "/api/litellm/target/gateways", json=_body(sizing=silent), headers=WRITE
    )

    assert "240 s" in check["problems"][0]
    assert response.status_code == 409
    assert "Nothing was created" in response.json()["detail"]
    assert not azure.arm_deployments


def test_the_deployment_carries_the_plan(wizard: _Wizard, azure: FakeAzure) -> None:
    assert (
        wizard.client.post("/api/litellm/target/gateways", json=_body(), headers=WRITE).status_code
        == 200
    )

    deployment = next(iter(azure.arm_deployments.values()))
    parameters = deployment["parameters"]
    assert (parameters["minReplicas"], parameters["maxReplicas"]) == (2, 6)
    assert parameters["redisSku"] == "Balanced_B0"
    assert parse_sizing_tag(parameters["sizing"]) == GatewaySizing.model_validate(PRODUCTION)


def test_a_node_size_has_its_twin_in_every_family() -> None:
    assert node_in_family("Standard_D8ds_v5", 1) == "Standard_D8ds_v7"
    assert node_in_family("Standard_D16ds_v7", 2) == "Standard_D16ds_v6"
    assert node_in_family("Standard_E8s_v5", 1) == "Standard_E8s_v5"
    assert node_pods("Standard_D8ds_v7") == node_pods("Standard_D8ds_v5") == 5
    assert node_pods("Standard_E8s_v5") is None
