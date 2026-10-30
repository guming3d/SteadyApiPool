"""The deployments scan outlives a gateway switch: it depends on the subscriptions, not the
gateway, so switching doesn't scan every Foundry account again."""

from __future__ import annotations

from pool_manager import deps
from pool_manager.arm import ArmClient
from pool_manager.config import Settings
from pool_manager.discovery import DeploymentDiscovery, ScanCache
from tests.apim.fake_apim import FakeCredential
from tests.fake_azure import FakeAzure


def _settings(**values: str) -> Settings:
    base = {"subscription_id": "sub", "resource_group": "rg", "auth_mode": "disabled"}
    return Settings(_env_file=None, **(base | values))  # type: ignore[call-arg]


def _discovery(azure: FakeAzure, scans: ScanCache, **values: str) -> DeploymentDiscovery:
    settings = _settings(**values)
    arm = ArmClient(settings, FakeCredential(), azure.transport(), sleep=lambda _: None)
    return DeploymentDiscovery(settings, arm, None, scans)


def _account_reads(azure: FakeAzure) -> int:
    return sum(r.url.path.endswith("/Microsoft.CognitiveServices/accounts") for r in azure.requests)


def test_a_switch_in_the_same_subscriptions_reuses_the_scan() -> None:
    azure = FakeAzure()
    azure.add_account("aoai-east", deployments=[("gpt6", "gpt-6", "OpenAI")])
    scans = ScanCache()

    before = _discovery(azure, scans, gateway_name="gw").list()
    after = _discovery(azure, scans, gateway_name="gw-other", resource_group="rg-2").list()

    assert [view.deployment for view in after] == [view.deployment for view in before] == ["gpt6"]
    assert _account_reads(azure) == 1
    # Other subscriptions are another scan; refresh scans again.
    _discovery(azure, scans, gateway_name="gw", discovery_subscriptions="sub,sub-2").list()
    _discovery(azure, scans, gateway_name="gw").list(refresh=True)
    assert _account_reads(azure) == 1 + 2 + 1


def test_forget_drops_the_shared_scan() -> None:
    azure = FakeAzure()
    scans = ScanCache()
    first = _discovery(azure, scans, gateway_name="gw")
    first.list()
    first.forget()
    _discovery(azure, scans, gateway_name="gw-other").list()
    assert _account_reads(azure) == 2


def test_resetting_the_target_keeps_the_scans() -> None:
    scans = deps._scans()
    deps.reset_target()
    assert deps._scans() is scans
