"""Removing a pool with its APIs, and cleaning up everything the app created in a service."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pool_manager.app import app
from pool_manager.policy import DEFAULT_POLICY
from tests.fake_apim import SERVICE, FakeApim
from tests.test_app import WRITE, _client, _member, _pool_body, _settings

# An API policy someone wrote by hand; unbinding must give it back byte for byte.
ORIGINAL = DEFAULT_POLICY.replace(
    "<outbound>",
    '<outbound>\n        <set-header name="x-a" exists-action="override">'
    "<value>1</value></set-header>",
)


@pytest.fixture
def apim() -> FakeApim:
    return FakeApim()


@pytest.fixture
def client(apim: FakeApim) -> Iterator[TestClient]:
    yield _client(apim, _settings())
    app.dependency_overrides.clear()


def _pool(client: TestClient, pool_id: str, members: list[str]) -> None:
    for member in members:
        if client.get(f"/api/backends/{member}").status_code == 404:
            _member(client, member)
    response = client.put(f"/api/pools/{pool_id}", json=_pool_body(members), headers=WRITE)
    assert response.status_code == 200, response.text


def _bind(client: TestClient, api_id: str, pool_id: str) -> None:
    response = client.put(f"/api/apis/{api_id}/binding", json={"pool_id": pool_id}, headers=WRITE)
    assert response.status_code == 200, response.text


def _confirm(plan: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "pools": plan["pools"],
        "delete_apis": [api["id"] for api in plan["delete_apis"]],
        "unbind_apis": [api["id"] for api in plan["unbind_apis"]],
        **extra,
    }


def _seed(client: TestClient, apim: FakeApim) -> None:
    """p1 has an API this app created and a hand-made one bound to it; p2 shares pool-b."""
    _pool(client, "p1", ["pool-a", "pool-b"])
    _pool(client, "p2", ["pool-b", "pool-c"])
    created = client.post("/api/pools/p1/api", json={"path": "chat"}, headers=WRITE)
    assert created.status_code == 200, created.text
    apim.add_api("legacy", "legacy", ORIGINAL)
    _bind(client, "legacy", "p1")
    apim.add_api("other", "other", DEFAULT_POLICY)


def test_pool_removal_plan_lists_what_goes(client: TestClient, apim: FakeApim) -> None:
    _seed(client, apim)
    before = len(apim.requests)
    plan = client.get("/api/pools/p1/removal").json()

    assert plan["pools"] == ["p1"]
    assert [(api["id"], api["pool_id"]) for api in plan["delete_apis"]] == [("p1", "p1")]
    assert [api["id"] for api in plan["unbind_apis"]] == ["legacy"]
    # pool-b is still in p2.
    assert plan["delete_members"] == ["pool-a"]
    assert (plan["kept_pools"], plan["blocked"]) == ([], [])
    # Reading the plan writes nothing.
    assert all(request.method == "GET" for request in apim.requests[before:])


def test_removing_a_pool_takes_its_apis_and_unused_members(
    client: TestClient, apim: FakeApim
) -> None:
    _seed(client, apim)
    plan = client.get("/api/pools/p1/removal").json()
    assert client.post("/api/pools/p1/remove", json=_confirm(plan)).status_code == 403  # CSRF

    response = client.post("/api/pools/p1/remove", json=_confirm(plan), headers=WRITE)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "pools": ["p1"],
        "deleted_apis": ["p1"],
        "unbound_apis": ["legacy"],
        "deleted_members": ["pool-a"],
    }
    # The API this app created is gone with its policy and operation.
    assert "p1" not in apim.apis
    assert "p1" not in apim.policies
    assert not [key for key in apim.operations if key[0] == "p1"]
    delete = next(
        r for r in apim.requests if r.method == "DELETE" and r.url.path.endswith("/apis/p1")
    )
    assert delete.headers["If-Match"] not in {"", "*"}
    assert delete.url.params["deleteRevisions"] == "true"
    # The hand-made API stays, with its own policy back.
    assert apim.policies["legacy"] == ORIGINAL
    assert "other" in apim.apis
    assert "p1" not in apim.backends
    assert "pool-a" not in apim.backends
    assert {"p2", "pool-b", "pool-c"} <= set(apim.backends)


def test_removal_refuses_what_was_not_reviewed(client: TestClient, apim: FakeApim) -> None:
    _seed(client, apim)
    plan = client.get("/api/pools/p1/removal").json()
    # Bound after the review: removing now would unbind something the user never saw.
    apim.add_api("late", "late", DEFAULT_POLICY)
    _bind(client, "late", "p1")

    response = client.post("/api/pools/p1/remove", json=_confirm(plan), headers=WRITE)
    assert response.status_code == 409
    assert "late" in response.json()["detail"]
    assert {"p1", "legacy", "late"} <= set(apim.apis)
    assert "p1" in apim.backends


def test_read_only_apis_block_removal(apim: FakeApim) -> None:
    client = _client(apim, _settings(protected_prefixes="other-"))
    try:
        _pool(client, "p1", ["pool-a", "pool-b"])
        apim.add_api("other-chat", "t-chat", DEFAULT_POLICY)
        _bind(client, "other-chat", "p1")

        plan = client.get("/api/pools/p1/removal").json()
        assert plan["blocked"] and "other-chat" in plan["blocked"][0]
        response = client.post("/api/pools/p1/remove", json=_confirm(plan), headers=WRITE)
        assert response.status_code == 409
        assert "p1" in apim.backends
    finally:
        app.dependency_overrides.clear()


def test_a_failed_step_says_what_was_already_done(client: TestClient, apim: FakeApim) -> None:
    _seed(client, apim)
    plan = client.get("/api/pools/p1/removal").json()
    # The app's role predates apis/delete.
    apim.allow("app", SERVICE, "Microsoft.ApiManagement/service/backends/*")

    response = client.post("/api/pools/p1/remove", json=_confirm(plan), headers=WRITE)
    assert response.status_code == 502
    assert "Already done: unbound legacy." in response.json()["detail"]
    assert "p1" in apim.apis
    assert "p1" in apim.backends


def test_cleanup_removes_only_what_the_app_created(client: TestClient, apim: FakeApim) -> None:
    _seed(client, apim)
    # An API this app created for p2 that was unbound earlier is left over.
    assert client.post("/api/pools/p2/api", json={"path": "p2"}, headers=WRITE).status_code == 200
    assert client.delete("/api/apis/p2/binding", headers=WRITE).status_code == 204
    # A pool made elsewhere keeps its member, even one this app created.
    _member(client, "pool-d")
    apim.add_backend(
        "hand-pool",
        {
            "type": "Pool",
            "pool": {"services": [{"id": f"{SERVICE}/backends/pool-d", "priority": 1}]},
        },
    )
    _member(client, "orphan")  # Created here, in no pool.

    plan = client.get("/api/cleanup").json()
    assert plan["pools"] == ["p1", "p2"]
    assert plan["kept_pools"] == ["hand-pool"]
    assert sorted(api["id"] for api in plan["delete_apis"]) == ["p1", "p2"]
    assert [api["id"] for api in plan["unbind_apis"]] == ["legacy"]
    assert sorted(plan["delete_members"]) == ["orphan", "pool-a", "pool-b", "pool-c"]

    wrong = client.post("/api/cleanup", json=_confirm(plan, confirm_name="nope"), headers=WRITE)
    assert wrong.status_code == 400
    assert "p1" in apim.backends

    response = client.post("/api/cleanup", json=_confirm(plan, confirm_name="APIM"), headers=WRITE)
    assert response.status_code == 200, response.text
    assert sorted(response.json()["pools"]) == ["p1", "p2"]
    assert set(apim.apis) == {"legacy", "other"}
    assert apim.policies["legacy"] == ORIGINAL
    assert set(apim.backends) == {"hand-pool", "pool-d"}
