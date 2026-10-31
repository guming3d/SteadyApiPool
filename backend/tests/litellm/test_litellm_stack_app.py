"""A user's journey through the app's HTTP API against a real LiteLLM proxy
(tests/litellm/litellm_stack): opt-in, needs Docker, and runs in CI.

    docker compose -f tests/litellm/litellm_stack/docker-compose.yml up -d --wait
    .venv/Scripts/python -m pytest -q -m litellm

The app is wired as deps.py wires it for a gateway connected by URL, except that ARM is the
in-memory fake (members here are custom endpoints, which no Azure call checks): every request
the browser makes goes through the real routes, auth and CSRF checks, and every change lands in
the real proxy's database. test_litellm_stack.py checks the service layer the same way.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pool_manager.app import app
from pool_manager.config import Settings
from tests.fake_azure import FakeAzure
from tests.litellm.test_app import _client
from tests.litellm.test_litellm_stack import (
    GATEWAY,
    MASTER_KEY,
    REGIONS,
    _calls,
    _region,
    reset_gateway,
)

pytestmark = pytest.mark.litellm

WRITE = {"X-Pool-Manager": "1"}
# A new name each run: LiteLLM keeps a member's cooldown by its row ID, in memory, so the same
# rows created again would still be cooling down from the last run's failover.
RUN = uuid.uuid4().hex[:6]
POOL = f"it-app-pool-{RUN}"
ALIAS = f"it-app-alias-{RUN}"


@pytest.fixture
def client() -> Iterator[TestClient]:
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
    test_client = _client(FakeAzure(), settings, real_gateway=True)
    try:
        reset_gateway()
        for name in REGIONS:
            _region(name, 200)
        yield test_client
        _clean(test_client)
        reset_gateway()
    finally:
        for name in REGIONS:
            _region(name, 200)
        app.dependency_overrides.clear()


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, (
        f"{response.request.method} {response.url}: {response.text}"
    )
    return response.json() if response.content else None


def _clean(client: TestClient) -> None:
    """Removes what earlier runs left. The proxy reloads its models on a schedule, so a pool can
    still be listed for a moment after it went: retried for a while, gone pools skipped."""
    deadline = time.monotonic() + 30
    while True:
        left = [
            p["id"] for p in _ok(client.get("/api/litellm/pools")) if p["id"].startswith("it-app-")
        ]
        for pool_id in left:
            if client.get(f"/api/litellm/pools/{pool_id}/removal").status_code == 200:
                _remove(client, pool_id)
        if not left or time.monotonic() > deadline:
            return
        time.sleep(2)


def _remove(client: TestClient, pool_id: str) -> Any:
    plan = _ok(client.get(f"/api/litellm/pools/{pool_id}/removal"))
    confirm = {
        "pools": plan["pools"],
        "delete_aliases": [alias["name"] for alias in plan["delete_aliases"]],
        "unbind_aliases": [alias["name"] for alias in plan["unbind_aliases"]],
        "delete_keys": [key["alias"] or key["id"] for key in plan["delete_keys"]],
    }
    return _ok(client.post(f"/api/litellm/pools/{pool_id}/remove", json=confirm, headers=WRITE))


def _member(client: TestClient, region: str) -> str:
    member = f"it-app-region-{region}"
    body = {
        "title": f"Region {region}",
        # Seen from inside the stack's network.
        "url": f"http://region-{region}:8080/openai/deployments/gpt-4o-mini",
        "auth": {"type": "api_key", "secret": f"key-{region}"},
    }
    _ok(client.put(f"/api/litellm/backends/{member}", json=body, headers=WRITE))
    return member


def _pool(members: list[str], etag: str | None = None, **settings: Any) -> dict[str, Any]:
    return {
        "title": "Integration",
        "members": [
            {"backend_id": member, "priority": index, "weight": 1}
            for index, member in enumerate(members)
        ],
        "etag": etag,
        **settings,
    }


def _test_call(client: TestClient, key: str, model: str = POOL) -> dict[str, Any]:
    body = {"key": key, "body": {"model": model, "messages": [{"role": "user", "content": "hi"}]}}
    result: dict[str, Any] = _ok(
        client.post(f"/api/litellm/pools/{POOL}/test", json=body, headers=WRITE)
    )
    return result


def test_a_user_builds_uses_and_removes_a_failover_pool(client: TestClient) -> None:
    # The gateway answers, with its database: what setup and the gateway panel read.
    capabilities = _ok(client.get("/api/litellm/capabilities"))
    assert capabilities["gateway_url"] == GATEWAY

    # Members, then the pool: the editor's preview first, then the save.
    a, b = _member(client, "a"), _member(client, "b")
    assert _ok(client.post("/api/litellm/pools/model-check", json={"members": [a, b]}))["ok"]
    preview = _ok(client.post(f"/api/litellm/pools/{POOL}/preview", json=_pool([a, b])))
    assert preview["current"] is None and f"model_name: {POOL}" in preview["proposed"]
    pool = _ok(client.put(f"/api/litellm/pools/{POOL}", json=_pool([a, b]), headers=WRITE))
    assert [m["backend_id"] for m in pool["members"]] == [a, b]
    assert "order: 1" in _ok(client.get(f"/api/litellm/pools/{POOL}/config"))["yaml"]

    # The call guide: an alias, the endpoint, a test key and a test call.
    _ok(client.post(f"/api/litellm/pools/{POOL}/aliases", json={"name": ALIAS}, headers=WRITE))
    endpoint = _ok(client.get(f"/api/litellm/pools/{POOL}/endpoint"))
    assert endpoint["model_names"] == [POOL, ALIAS]
    key = _ok(client.post(f"/api/litellm/pools/{POOL}/keys", headers=WRITE))["key"]
    first = _test_call(client, key)
    assert first["status"] == 200, first
    assert first["headers"]["x-litellm-model-id"] == f"{POOL}--{a}"
    assert _test_call(client, key, ALIAS)["status"] == 200
    # Only the key's own pool names, and the owner-only key list never has the secret.
    keys = _ok(client.get(f"/api/litellm/pools/{POOL}/keys"))
    assert len(keys) == 1 and key not in str(keys)

    # Failover: region a fails, b answers, and a cools down.
    _region("a", 500)
    failed_over = _test_call(client, key)
    assert failed_over["status"] == 200, failed_over
    assert failed_over["headers"]["x-litellm-model-id"] == f"{POOL}--{b}"
    _region("a", 200)

    # Edit: balanced, longer cooldown, more retries. A stale ETag is refused.
    stale = pool["etag"]
    edited = _pool(
        [a, b],
        etag=stale,
        circuit_breaker={"allowed_fails": 1, "cooldown_seconds": 120},
        retries=2,
        timeout_seconds=300,
    )
    edited["members"][1]["priority"] = 0
    saved = _ok(client.put(f"/api/litellm/pools/{POOL}", json=edited, headers=WRITE))
    assert [(m["priority"], m["weight"]) for m in saved["members"]] == [(0, 1), (0, 1)]
    assert (saved["retries"], saved["timeout_seconds"]) == (2, 300)
    assert saved["circuit_breaker"] == {"allowed_fails": 1, "cooldown_seconds": 120}
    conflict = client.put(f"/api/litellm/pools/{POOL}", json=edited, headers=WRITE)
    assert conflict.status_code == 409

    # A short load test through the app; both members share it now.
    start = {
        "spec": {"users": 2, "duration_seconds": 5, "max_tokens": 16, "stream": True},
        "key": key,
    }
    run = _ok(client.post(f"/api/litellm/pools/{POOL}/benchmarks", json=start, headers=WRITE))
    deadline = time.monotonic() + 60
    while run["state"] == "running" and time.monotonic() < deadline:
        time.sleep(1)
        run = _ok(client.get(f"/api/litellm/pools/{POOL}/benchmarks/{run['id']}"))
    assert run["state"] == "done", run
    summary = run["summary"]
    assert summary["requests"] > 0 and summary["successes"] == summary["requests"], summary
    assert summary["ttft_p50_ms"] is not None
    # Region a may still be cooling down from its failure; every answer came from the pool.
    assert {m["member"] for m in summary["members"]} <= {f"{POOL}--{a}", f"{POOL}--{b}"}

    # Writes need the CSRF header, whoever asks.
    assert client.put(f"/api/litellm/pools/{POOL}", json=edited).status_code == 403

    # Removal: the plan, then exactly that. Rows, alias, key and member credentials go.
    removed = _remove(client, POOL)
    assert removed["pools"] == [POOL] and removed["deleted_aliases"] == [ALIAS]
    assert client.get(f"/api/litellm/pools/{POOL}").status_code == 404
    assert ALIAS not in {alias["name"] for alias in _ok(client.get("/api/litellm/aliases"))}
    gone = httpx.post(
        f"{GATEWAY}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": POOL, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert gone.status_code in {400, 401, 404}


def test_cleanup_takes_only_what_the_app_created(client: TestClient) -> None:
    a, b = _member(client, "a"), _member(client, "b")
    for pool_id in ("it-app-one", "it-app-two"):
        _ok(client.put(f"/api/litellm/pools/{pool_id}", json=_pool([a, b]), headers=WRITE))
    calls = _calls("b")

    plan = _ok(client.get("/api/litellm/cleanup"))
    assert {"it-app-one", "it-app-two"} <= set(plan["pools"])
    confirm = {
        "pools": plan["pools"],
        "delete_aliases": [alias["name"] for alias in plan["delete_aliases"]],
        "unbind_aliases": [alias["name"] for alias in plan["unbind_aliases"]],
        "delete_keys": [key["alias"] or key["id"] for key in plan["delete_keys"]],
    }
    wrong = client.post(
        "/api/litellm/cleanup", json={**confirm, "confirm_name": "nope"}, headers=WRITE
    )
    assert wrong.status_code == 400
    _ok(
        client.post(
            "/api/litellm/cleanup", json={**confirm, "confirm_name": "stack"}, headers=WRITE
        )
    )

    assert not [p for p in _ok(client.get("/api/litellm/pools")) if p["id"].startswith("it-app-")]
    assert _calls("b") == calls  # Cleanup sends no model calls.
