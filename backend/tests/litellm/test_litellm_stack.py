"""The service layer against a real LiteLLM proxy (tests/litellm/litellm_stack): opt-in,
needs Docker.

    docker compose -f tests/litellm/litellm_stack/docker-compose.yml up -d --wait
    .venv/Scripts/python -m pytest -q -m litellm

Members are API-key members on two fake Azure OpenAI regions (the stack has no managed
identity), so this checks what the fakes can't: that LiteLLM takes the rows, credentials,
aliases and keys this app writes, and routes the way the pool says.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from pool_manager.arm import ArmClient
from pool_manager.config import Settings
from pool_manager.litellm.api_test import LiteLLMTester
from pool_manager.litellm.gateway import GatewayConnection
from pool_manager.litellm.models import (
    ApiTestRequest,
    CircuitBreaker,
    MemberAuth,
    MemberBackendWrite,
    PoolWrite,
    RemovalConfirm,
)
from pool_manager.litellm.service import LiteLLMService
from pool_manager.models import PoolMemberWrite
from tests.fake_azure import FakeAzure, FakeCredential

pytestmark = pytest.mark.litellm

GATEWAY = os.environ.get("POOL_MANAGER_TEST_GATEWAY", "http://localhost:14000")
MASTER_KEY = "sk-test-master-key-0123456789"
REGIONS = {"a": "http://localhost:18081", "b": "http://localhost:18082"}
POOL = "it-pool"


def _region(name: str, status: int) -> None:
    httpx.post(f"{REGIONS[name]}/control", json={"status": status, "reset": True}, timeout=10)


def _calls(name: str) -> int:
    return int(httpx.get(f"{REGIONS[name]}/stats", timeout=10).json()["calls"])


@pytest.fixture
def service() -> Iterator[tuple[LiteLLMService, LiteLLMTester]]:
    try:
        httpx.get(f"{GATEWAY}/health/readiness", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(f"No LiteLLM stack at {GATEWAY}")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        auth_mode="disabled",
        subscription_id="sub",
        gateway_name="stack",
        gateway_url=GATEWAY,
        gateway_master_key=MASTER_KEY,
    )
    arm = ArmClient(settings, FakeCredential(), FakeAzure().transport(), sleep=lambda _: None)
    service = LiteLLMService(settings, arm, GatewayConnection(settings, arm))
    tester = LiteLLMTester(settings, service)
    _clean(service)
    for name in REGIONS:
        _region(name, 200)
    yield service, tester
    _clean(service)


def _clean(service: LiteLLMService) -> None:
    for pool in service.list_pools():
        if pool.id.startswith("it-"):
            plan = service.pool_removal_plan(pool.id)
            service.remove_pool(pool.id, RemovalConfirm(**_confirm(plan)), "test")


def _confirm(plan: Any) -> dict[str, Any]:
    return {
        "pools": plan.pools,
        "delete_aliases": [a.name for a in plan.delete_aliases],
        "unbind_aliases": [a.name for a in plan.unbind_aliases],
        "delete_keys": [k.alias or k.id for k in plan.delete_keys],
    }


def _members(service: LiteLLMService) -> list[str]:
    ids = []
    for name in REGIONS:
        member = f"it-region-{name}"
        service.put_backend(
            member,
            MemberBackendWrite(
                title=f"Region {name}",
                # Seen from inside the stack's network.
                url=f"http://region-{name}:8080/openai/deployments/gpt-4o-mini",  # type: ignore[arg-type]
                auth=MemberAuth(type="api_key", secret=f"key-{name}"),  # type: ignore[arg-type]
            ),
            "test",
        )
        ids.append(member)
    return ids


def _call(tester: LiteLLMTester, key: str, model: str = POOL) -> dict[str, Any]:
    result = tester.call(
        POOL,
        ApiTestRequest(  # type: ignore[arg-type]
            key=key, body={"model": model, "messages": [{"role": "user", "content": "hi"}]}
        ),
        "test",
    )
    return result.model_dump()


def test_a_failover_pool_on_a_real_gateway(service: tuple[LiteLLMService, LiteLLMTester]) -> None:
    pools, tester = service
    a, b = _members(pools)
    pool = pools.put_pool(
        POOL,
        PoolWrite(
            title="Integration",
            members=[
                PoolMemberWrite(backend_id=a, priority=0, weight=1),
                PoolMemberWrite(backend_id=b, priority=1, weight=1),
            ],
            circuit_breaker=CircuitBreaker(allowed_fails=0, cooldown_seconds=30),
        ),
        "test",
    )
    assert [m.backend_id for m in pool.members] == [a, b]
    assert pool.circuit_breaker == CircuitBreaker(allowed_fails=0, cooldown_seconds=30)

    pools.add_alias(POOL, "it-alias", "test")
    key = tester.create_key(POOL, "test").key

    # Healthy: the primary answers, under the pool's name and its alias.
    first = _call(tester, key)
    assert first["status"] == 200, first["body"]
    assert first["headers"]["x-litellm-model-id"] == f"{POOL}--{a}"
    assert _call(tester, key, "it-alias")["status"] == 200

    # The primary throttles: the same request is answered by the standby.
    _region("a", 429)
    failed_over = _call(tester, key)
    assert failed_over["status"] == 200, failed_over["body"]
    assert failed_over["headers"]["x-litellm-model-id"] == f"{POOL}--{b}"
    # Cooling down: the next requests skip the primary without trying it.
    before = httpx.get(f"{REGIONS['a']}/stats", timeout=10).json()["failed"]
    assert _call(tester, key)["headers"]["x-litellm-model-id"] == f"{POOL}--{b}"
    assert httpx.get(f"{REGIONS['a']}/stats", timeout=10).json()["failed"] == before

    # The key is scoped to the pool's names.
    other = httpx.post(
        f"{GATEWAY}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "something-else", "messages": []},
        timeout=30,
    )
    assert other.status_code in {400, 401, 403}

    # Removing the pool takes its rows, alias, test key and member credentials.
    plan = pools.pool_removal_plan(POOL)
    assert [x.name for x in plan.delete_aliases] == ["it-alias"]
    pools.remove_pool(POOL, RemovalConfirm(**_confirm(plan)), "test")
    assert POOL not in {p.id for p in pools.list_pools()}
    assert "it-alias" not in {x.name for x in pools.list_aliases()}
    time.sleep(1)
    revoked = httpx.post(
        f"{GATEWAY}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": POOL, "messages": []},
        timeout=30,
    )
    assert revoked.status_code == 401
    credentials = httpx.get(
        f"{GATEWAY}/credentials", headers={"Authorization": f"Bearer {MASTER_KEY}"}, timeout=10
    ).json()
    names = {item["credential_name"] for item in credentials.get("credentials") or []}
    assert not {f"{a}-credential", f"{b}-credential"} & names


def test_saving_a_pool_again_changes_rows_in_place(
    service: tuple[LiteLLMService, LiteLLMTester],
) -> None:
    pools, _ = service
    a, b = _members(pools)
    write = PoolWrite(
        members=[
            PoolMemberWrite(backend_id=a, priority=0, weight=1),
            PoolMemberWrite(backend_id=b, priority=1, weight=1),
        ]
    )
    pool = pools.put_pool(POOL, write, "test")
    balanced = write.model_copy(
        update={
            "members": [
                PoolMemberWrite(backend_id=a, priority=0, weight=3),
                PoolMemberWrite(backend_id=b, priority=0, weight=1),
            ],
            "retries": 2,
            "etag": pool.etag,
        }
    )
    saved = pools.put_pool(POOL, balanced, "test")

    assert [(m.priority, m.weight) for m in saved.members] == [(0, 3), (0, 1)]
    assert saved.retries == 2
    assert "order: 1" in (pools.config(POOL).yaml or "")
    assert saved.etag != pool.etag
