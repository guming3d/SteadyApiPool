"""Pools on a LiteLLM gateway: the service layer against in-memory LiteLLM and ARM fakes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from pool_manager.arm import ArmClient
from pool_manager.config import Settings
from pool_manager.discovery import DeploymentDiscovery
from pool_manager.errors import ServiceError
from pool_manager.litellm.api_test import LiteLLMTester
from pool_manager.litellm.gateway import GatewayConnection
from pool_manager.litellm.models import (
    AliasWrite,
    ApiTestRequest,
    CircuitBreaker,
    CleanupConfirm,
    MemberAuth,
    MemberBackendWrite,
    PoolWrite,
    RemovalConfirm,
)
from pool_manager.litellm.resources import MANAGED_MARKER
from pool_manager.litellm.service import LiteLLMService
from pool_manager.models import DeploymentImport, DeploymentRef, PoolMemberWrite
from tests.fake_azure import GATEWAY_APP, FakeAzure, FakeCredential
from tests.litellm.fake_litellm import MASTER_KEY, URL, FakeLiteLLM


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "subscription_id": "sub",
        "resource_group": "rg",
        "gateway_name": "gw",
        "auth_mode": "disabled",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@dataclass
class Wired:
    apim: FakeAzure
    lite: FakeLiteLLM
    service: LiteLLMService
    discovery: DeploymentDiscovery
    tester: LiteLLMTester


def _wire(settings: Settings | None = None, apim: FakeAzure | None = None) -> Wired:
    settings = settings or _settings()
    apim = apim or FakeAzure()
    lite = FakeLiteLLM()
    arm = ArmClient(settings, FakeCredential(), apim.transport(), sleep=lambda _: None)
    gateway = GatewayConnection(settings, arm, transport=lite.transport(), sleep=lambda _: None)
    service = LiteLLMService(settings, arm, gateway)
    discovery = DeploymentDiscovery(settings, arm, service)
    tester = LiteLLMTester(settings, service, transport=lite.transport())
    return Wired(apim, lite, service, discovery, tester)


@pytest.fixture
def w() -> Wired:
    return _wire()


def _seed(apim: FakeAzure, model: str = "gpt-6") -> list[str]:
    east = apim.add_account("aoai-east", deployments=[("gpt6", model, "OpenAI")])
    west = apim.add_account(
        "aoai-west", location="westus3", deployments=[("gpt6", model, "OpenAI")]
    )
    return [east, west]


def _import(w: Wired, *accounts: str, deployment: str = "gpt6") -> list[str]:
    result = w.discovery.import_deployments(
        DeploymentImport(
            deployments=[DeploymentRef(account_id=a, deployment=deployment) for a in accounts]
        ),
        "alice",
    )
    return result.backend_ids


def _pool(members: list[str], etag: str | None = None, **extra: Any) -> PoolWrite:
    return PoolWrite(
        title="GPT-6",
        members=[
            PoolMemberWrite(backend_id=member, priority=index, weight=1)
            for index, member in enumerate(members)
        ],
        etag=etag,
        **extra,
    )


def _saved(w: Wired) -> tuple[list[str], str]:
    ids = _import(w, *_seed(w.apim))
    pool = w.service.put_pool("gpt6", _pool(ids), "alice")
    assert pool.etag
    return ids, pool.etag


def _bodies(lite: FakeLiteLLM, method: str, suffix: str) -> list[Any]:
    return [
        json.loads(r.content)
        for r in lite.requests
        if r.method == method and r.url.path.endswith(suffix)
    ]


# -- the gateway connection -------------------------------------------------------------------


def test_the_master_key_is_read_from_the_container_app(w: Wired) -> None:
    w.service.list_pools()

    calls = [r for r in w.apim.requests if r.url.path.startswith(GATEWAY_APP)]
    assert [r.method for r in calls] == ["GET", "POST"]
    assert calls[1].url.path == f"{GATEWAY_APP}/listSecrets"
    admin = w.lite.requests[0]
    assert str(admin.url).startswith(URL)
    assert admin.headers["Authorization"] == f"Bearer {MASTER_KEY}"


def test_without_list_secrets_the_gateway_cant_be_managed(w: Wired) -> None:
    w.apim.allow("app", GATEWAY_APP, "Microsoft.App/containerApps/read")

    with pytest.raises(ServiceError) as raised:
        w.service.list_pools()

    assert raised.value.status == 403
    assert "listSecrets" in raised.value.message


def test_an_external_gateway_uses_its_url_and_key() -> None:
    settings = _settings(
        resource_group="",
        gateway_url=URL,
        gateway_master_key=MASTER_KEY,
        gateway_principal_id="aks-mi",
    )
    w = _wire(settings)

    assert w.service.list_pools() == []
    caps = w.service.capabilities()
    assert caps.gateway_kind == "external"
    assert caps.gateway_principal_id == "aks-mi"
    assert caps.gateway_url == URL
    assert caps.litellm_version == "1.106.4"
    assert not [r for r in w.apim.requests if "containerApps" in r.url.path]


def test_capabilities_describe_the_container_app(w: Wired) -> None:
    caps = w.service.capabilities()

    assert caps.gateway_kind == "aca"
    assert caps.gateway_resource_id == GATEWAY_APP
    assert caps.gateway_principal_id == "gateway-mi"
    assert caps.gateway_url == URL


# -- pools ----------------------------------------------------------------------------------


def test_a_pool_is_one_row_per_member(w: Wired) -> None:
    ids, _ = _saved(w)

    rows = w.lite.pool("gpt6")
    assert [row["model_info"]["id"] for row in rows] == [f"gpt6--{ids[0]}", f"gpt6--{ids[1]}"]
    first = rows[0]
    assert first["litellm_params"] == {
        "model": "azure/gpt6",
        "api_base": "https://aoai-east.openai.azure.com",
        "order": 1,
        "weight": 1,
        "num_retries": 1,
        "timeout": 120,
    }
    # No key: the gateway calls Foundry as its managed identity.
    assert "api_key" not in first["litellm_params"]
    assert first["model_info"]["allowed_fails"] == 0
    assert first["model_info"]["cooldown_time"] == 60
    metadata = first["model_info"]["pool_manager"]
    assert metadata["managed_by"] == MANAGED_MARKER
    assert metadata["pool"] == {"title": "GPT-6", "description": None, "aliases": []}
    assert metadata["member"]["auth"] == "managed_identity"
    assert rows[1]["litellm_params"]["order"] == 2


def test_a_saved_pool_reads_back(w: Wired) -> None:
    ids, etag = _saved(w)

    pool = w.service.get_pool("gpt6")
    assert pool.title == "GPT-6"
    assert [(m.backend_id, m.priority, m.weight) for m in pool.members] == [
        (ids[0], 0, 1),
        (ids[1], 1, 1),
    ]
    assert pool.circuit_breaker == CircuitBreaker()
    assert pool.retries == 1
    assert pool.timeout_seconds == 120
    assert pool.managed
    assert pool.etag == etag
    members = {view.id: view for view in w.service.list_backends()}
    assert members[ids[0]].pools == ["gpt6"]
    assert members[ids[0]].provider == "azure"
    assert members[ids[0]].model == "gpt6"


def test_saving_drops_adds_and_changes_rows(w: Wired) -> None:
    ids, etag = _saved(w)
    third = w.apim.add_account("aoai-third", deployments=[("gpt6", "gpt-6", "OpenAI")])
    new = _import(w, third)[0]

    write = PoolWrite(
        members=[
            PoolMemberWrite(backend_id=ids[1], priority=0, weight=3),
            PoolMemberWrite(backend_id=new, priority=0, weight=1),
        ],
        circuit_breaker=CircuitBreaker(allowed_fails=2, cooldown_seconds=30),
        retries=0,
        etag=etag,
    )
    pool = w.service.put_pool("gpt6", write, "alice")

    assert [m.backend_id for m in pool.members] == [ids[1], new]
    assert pool.title == "GPT-6"  # Kept when the write leaves it out.
    assert pool.circuit_breaker == CircuitBreaker(allowed_fails=2, cooldown_seconds=30)
    assert pool.retries == 0
    rows = {row["model_info"]["id"]: row for row in w.lite.pool("gpt6")}
    assert set(rows) == {f"gpt6--{ids[1]}", f"gpt6--{new}"}
    assert rows[f"gpt6--{ids[1]}"]["litellm_params"]["weight"] == 3
    assert rows[f"gpt6--{new}"]["model_info"]["cooldown_time"] == 30
    # New rows are written before the old one goes, so the pool never runs empty.
    order = [(r.method, r.url.path) for r in w.lite.requests if r.method in {"POST", "PATCH"}][-3:]
    assert [path for _, path in order] == [
        "/model/new",
        f"/model/gpt6--{ids[1]}/update",
        "/model/delete",
    ]


def test_saves_are_conditional(w: Wired) -> None:
    ids, etag = _saved(w)

    with pytest.raises(ServiceError) as exists:
        w.service.put_pool("gpt6", _pool(ids), "alice")
    assert exists.value.status == 409
    w.lite.pool("gpt6")[0]["litellm_params"]["weight"] = 5  # Someone else changed it.
    with pytest.raises(ServiceError) as stale:
        w.service.put_pool("gpt6", _pool(ids, etag=etag), "alice")
    assert stale.value.status == 409
    with pytest.raises(ServiceError) as gone:
        w.service.put_pool("other", _pool(ids, etag=etag), "alice")
    assert gone.value.status == 404


def test_members_must_serve_the_same_model(w: Wired) -> None:
    east, _ = _seed(w.apim)
    mini = w.apim.add_account("aoai-mini", deployments=[("gpt6", "gpt-6-mini", "OpenAI")])
    ids = _import(w, east, mini)

    check = w.service.check_models(ids)
    assert not check.ok
    assert check.model_names == ["gpt-6", "gpt-6-mini"]
    with pytest.raises(ServiceError) as raised:
        w.service.put_pool("gpt6", _pool(ids), "alice")
    assert raised.value.status == 409
    assert w.lite.rows == []


def test_unknown_members_are_refused(w: Wired) -> None:
    with pytest.raises(ServiceError) as raised:
        w.service.put_pool("gpt6", _pool(["nope-1", "nope-2"]), "alice")

    assert raised.value.status == 400


def test_pools_this_app_didnt_write_are_read_only(w: Wired) -> None:
    w.lite.add_row("legacy", "azure/gpt6", "https://aoai-east.openai.azure.com", id="l1")
    w.lite.add_row("legacy", "azure/gpt6", "https://aoai-west.openai.azure.com", id="l2")

    pool = w.service.get_pool("legacy")
    assert not pool.managed
    assert [m.backend_id for m in pool.members] == ["l1", "l2"]
    with pytest.raises(ServiceError) as raised:
        w.service.put_pool("legacy", _pool(["l1", "l2"], etag=pool.etag), "alice")
    assert raised.value.status == 409
    members = {view.id: view for view in w.service.list_backends()}
    assert members["l1"].url == "https://aoai-east.openai.azure.com/openai/deployments/gpt6"
    assert members["l1"].auth_type == "custom"
    assert not members["l1"].managed


def test_protected_pools_are_read_only() -> None:
    w = _wire(_settings(protected_prefixes="finops-"))
    ids = _import(w, *_seed(w.apim))

    with pytest.raises(ServiceError) as raised:
        w.service.put_pool("finops-gpt6", _pool(ids), "alice")

    assert raised.value.status == 403


def test_an_api_key_member_keeps_its_key_in_a_credential(w: Wired) -> None:
    east, _ = _seed(w.apim)
    member = _import(w, east)[0]
    w.service.put_backend(
        "custom-openai",
        MemberBackendWrite(
            url="https://api.partner.example/v1",  # type: ignore[arg-type]
            model="gpt-6",
            auth=MemberAuth(type="api_key", secret="partner-key"),  # type: ignore[arg-type]
        ),
        "alice",
    )

    w.service.put_pool("mixed", _pool([member, "custom-openai"]), "alice")

    assert w.lite.credentials["custom-openai-credential"]["credential_values"] == {
        "api_key": "partner-key"
    }
    row = next(r for r in w.lite.pool("mixed") if r["model_info"]["id"] == "mixed--custom-openai")
    assert row["litellm_params"]["model"] == "openai/gpt-6"
    assert row["litellm_params"]["litellm_credential_name"] == "custom-openai-credential"
    assert "partner-key" not in json.dumps(w.lite.pool("mixed"))

    # Another pool reuses the member without its key, which can't be read back.
    second = w.service.put_pool("mixed-2", _pool(["custom-openai", member]), "alice")
    assert [m.backend_id for m in second.members] == ["custom-openai", member]

    # The credential goes with the last row using it.
    for pool_id in ("mixed", "mixed-2"):
        plan = w.service.pool_removal_plan(pool_id)
        w.service.remove_pool(
            pool_id,
            RemovalConfirm(pools=plan.pools, delete_aliases=[], unbind_aliases=[], delete_keys=[]),
            "alice",
        )
    assert w.lite.credentials == {}


def test_an_openai_compatible_member_needs_a_model(w: Wired) -> None:
    with pytest.raises(ServiceError) as raised:
        w.service.put_backend(
            "custom",
            MemberBackendWrite(url="https://api.partner.example/v1"),  # type: ignore[arg-type]
            "alice",
        )

    assert raised.value.status == 400


def test_changing_a_member_rewrites_its_rows(w: Wired) -> None:
    ids, _ = _saved(w)

    w.service.put_backend(
        ids[0],
        MemberBackendWrite(
            title="East (renamed)",
            url="https://aoai-east.openai.azure.com/openai/deployments/gpt6",  # type: ignore[arg-type]
            auth=MemberAuth(type="managed_identity"),
        ),
        "alice",
    )

    assert w.service.get_backend(ids[0]).title == "East (renamed)"
    pool = w.service.get_pool("gpt6")
    assert [m.priority for m in pool.members] == [0, 1]


def test_members_in_pools_cant_be_deleted(w: Wired) -> None:
    ids, _ = _saved(w)
    third = w.apim.add_account("aoai-third", deployments=[("gpt6", "gpt-6", "OpenAI")])
    pending = _import(w, third)[0]

    with pytest.raises(ServiceError) as raised:
        w.service.delete_backend(ids[0], "alice")
    assert raised.value.status == 409
    w.service.delete_backend(pending, "alice")
    assert pending not in {view.id for view in w.service.list_backends()}


def test_imports_match_members_by_url(w: Wired) -> None:
    ids, _ = _saved(w)

    views = {view.account_name: view for view in w.discovery.list()}
    assert views["aoai-east"].backend_id == ids[0]
    assert _import(w, views["aoai-east"].account_id) == [ids[0]]


def test_the_config_view_hides_secrets(w: Wired) -> None:
    ids, etag = _saved(w)

    config = w.service.config("gpt6").yaml
    assert config is not None
    assert "model_name: gpt6" in config
    assert "model: azure/gpt6" in config
    assert "order: 2" in config
    preview = w.service.preview_pool(
        "gpt6",
        PoolWrite(
            members=[
                PoolMemberWrite(backend_id=ids[0], priority=0, weight=1),
                PoolMemberWrite(backend_id=ids[1], priority=0, weight=1),
            ],
            etag=etag,
        ),
    )
    assert preview.current == config
    assert "order: 2" not in preview.proposed
    assert len(w.lite.pool("gpt6")) == 2  # Nothing written.


# -- aliases ------------------------------------------------------------------------------------


def test_aliases_route_other_names_to_the_pool(w: Wired) -> None:
    _saved(w)

    endpoint = w.service.add_alias("gpt6", AliasWrite(name="gpt-4o").name, "alice")

    assert endpoint.model_names == ["gpt6", "gpt-4o"]
    assert endpoint.base_url == f"{URL}/v1"
    assert w.lite.router["model_group_alias"] == {"gpt-4o": "gpt6"}
    assert w.service.get_pool("gpt6").aliases == ["gpt-4o"]
    assert [a.managed for a in w.service.list_aliases()] == [True]

    endpoint = w.service.remove_alias("gpt6", "gpt-4o", "alice")
    assert endpoint.model_names == ["gpt6"]
    assert w.lite.router["model_group_alias"] == {}
    assert w.service.get_pool("gpt6").aliases == []


def test_an_alias_cant_shadow_a_pool_or_another_alias(w: Wired) -> None:
    ids, _ = _saved(w)
    w.service.put_pool("gpt6-b", _pool(list(reversed(ids))), "alice")
    w.lite.router["model_group_alias"] = {"taken": "elsewhere"}

    for name in ("gpt6-b", "taken"):
        with pytest.raises(ServiceError) as raised:
            w.service.add_alias("gpt6", name, "alice")
        assert raised.value.status == 409
    with pytest.raises(ServiceError) as conflict:
        w.service.put_pool("taken", _pool(ids), "alice")
    assert conflict.value.status == 409


# -- removal ------------------------------------------------------------------------------------


def _confirm(plan: Any, **extra: Any) -> dict[str, Any]:
    return {
        "pools": plan.pools,
        "delete_aliases": [a.name for a in plan.delete_aliases],
        "unbind_aliases": [a.name for a in plan.unbind_aliases],
        "delete_keys": [k.alias or k.id for k in plan.delete_keys],
        **extra,
    }


def test_removing_a_pool_takes_its_aliases_and_test_key(w: Wired) -> None:
    _saved(w)
    w.service.add_alias("gpt6", "gpt-4o", "alice")
    w.lite.router["model_group_alias"]["by-hand"] = "gpt6"
    w.tester.create_key("gpt6", "alice")

    plan = w.service.pool_removal_plan("gpt6")
    assert [a.name for a in plan.delete_aliases] == ["gpt-4o"]
    assert [a.name for a in plan.unbind_aliases] == ["by-hand"]
    assert [k.alias for k in plan.delete_keys] == ["pool-test-gpt6"]
    assert w.lite.rows  # Nothing removed yet.

    result = w.service.remove_pool("gpt6", RemovalConfirm(**_confirm(plan)), "alice")

    assert result.pools == ["gpt6"]
    assert result.deleted_keys == ["pool-test-gpt6"]
    assert w.lite.rows == []
    assert w.lite.router["model_group_alias"] == {}
    assert w.lite.keys == {}


def test_removal_refuses_what_wasnt_reviewed(w: Wired) -> None:
    _saved(w)
    plan = w.service.pool_removal_plan("gpt6")
    w.service.add_alias("gpt6", "late", "alice")

    with pytest.raises(ServiceError) as raised:
        w.service.remove_pool("gpt6", RemovalConfirm(**_confirm(plan)), "alice")

    assert raised.value.status == 409
    assert "late" in raised.value.message
    assert len(w.lite.pool("gpt6")) == 2


def test_cleanup_takes_only_this_apps_pools(w: Wired) -> None:
    _saved(w)
    w.lite.add_row("legacy", "azure/gpt6", "https://aoai-east.openai.azure.com", id="l1")

    plan = w.service.cleanup_plan()
    assert plan.pools == ["gpt6"]
    assert plan.kept_pools == ["legacy"]
    with pytest.raises(ServiceError):
        w.service.cleanup(CleanupConfirm(**_confirm(plan, confirm_name="wrong")), "alice")

    w.service.cleanup(CleanupConfirm(**_confirm(plan, confirm_name="GW")), "alice")
    assert [row["model_name"] for row in w.lite.rows] == ["legacy"]


# -- keys and the test call --------------------------------------------------------------------


def test_the_test_key_is_scoped_to_the_pool(w: Wired) -> None:
    _saved(w)
    w.service.add_alias("gpt6", "gpt-4o", "alice")

    first = w.tester.create_key("gpt6", "alice")
    assert first.view.models == ["gpt6", "gpt-4o"]
    assert first.view.managed and first.view.scope == "pool"
    second = w.tester.create_key("gpt6", "alice")
    assert second.key != first.key
    assert list(w.lite.keys) == [second.view.id]  # The old one is gone.

    w.lite.keys["other"] = {"token": "other", "key": "sk-x", "key_alias": "x", "models": ["m"]}
    w.lite.keys["all"] = {"token": "all", "key": "sk-y", "key_alias": "y", "models": []}
    keys = w.tester.keys("gpt6")
    assert [(k.id, k.scope) for k in keys] == [(second.view.id, "pool"), ("all", "all")]


def test_a_test_call_says_which_member_answered(w: Wired) -> None:
    ids, _ = _saved(w)
    key = w.tester.create_key("gpt6", "alice").key
    w.lite.failing[f"gpt6--{ids[0]}"] = 429

    result = w.tester.call(
        "gpt6",
        ApiTestRequest(key=key, body={"messages": [{"role": "user", "content": "hi"}]}),  # type: ignore[arg-type]
        "alice",
    )

    assert result.status == 200
    assert result.url == f"{URL}/v1/chat/completions"
    assert result.headers["x-litellm-model-id"] == f"gpt6--{ids[1]}"
    assert result.headers["x-litellm-attempted-fallbacks"] == "1"
    assert "x-litellm-response-cost" not in result.headers
    sent = json.loads(w.lite.requests[-1].content)
    assert sent["model"] == "gpt6"
    assert w.lite.requests[-1].headers["Authorization"] == f"Bearer {key}"


def test_a_test_call_only_sends_the_pools_names(w: Wired) -> None:
    _saved(w)

    with pytest.raises(ServiceError) as raised:
        w.tester.call(
            "gpt6",
            ApiTestRequest(key="sk-1", body={"model": "elsewhere"}),  # type: ignore[arg-type]
            "alice",
        )
    assert raised.value.status == 400
    with pytest.raises(ServiceError) as no_key:
        w.tester.call("gpt6", ApiTestRequest(body={}), "alice")
    assert no_key.value.status == 400


def test_foundry_accounts_are_those_of_managed_identity_members(w: Wired) -> None:
    east, west = _seed(w.apim)
    _import(w, east, west)

    assert w.service.foundry_accounts() == [east, west]


def test_the_admin_api_errors_become_service_errors(w: Wired) -> None:
    w.lite.master_key = "rotated"

    with pytest.raises(ServiceError) as raised:
        w.service.list_pools()

    assert raised.value.status == 502
    assert "master key" in raised.value.message


def test_an_unreachable_gateway_is_a_bad_gateway() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    settings = _settings()
    apim = FakeAzure()
    arm = ArmClient(settings, FakeCredential(), apim.transport(), sleep=lambda _: None)
    gateway = GatewayConnection(settings, arm, transport=httpx.MockTransport(refuse))
    service = LiteLLMService(settings, arm, gateway)

    with pytest.raises(ServiceError) as raised:
        service.list_pools()

    assert raised.value.status == 502
