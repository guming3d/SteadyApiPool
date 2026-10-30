"""Choosing or creating the APIM service in the local setup wizard."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pool_manager import config
from pool_manager.apim.target import MANAGED_TAG, MANAGED_TAG_VALUE, ApimTargets
from pool_manager.app import app, get_apim_targets, get_targets
from pool_manager.config import Settings, read_saved_target
from pool_manager.target import TargetChooser
from tests.apim.fake_apim import SUBSCRIPTION, TENANT, FakeApim, FakeCredential
from tests.apim.test_app import WRITE, _client, _settings

NO_TARGET = {"subscription_id": "", "resource_group": "", "apim_name": ""}
NEW_SERVICE = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-new"
    "/providers/Microsoft.ApiManagement/service/apim-new"
)


class _Wizard:
    def __init__(self, apim: FakeApim, settings: Settings, state_file: Path) -> None:
        self.credential = FakeCredential()
        self.changes = 0
        self.state_file = state_file
        self.client = _client(apim, settings)
        chooser = TargetChooser(
            settings,
            self.credential,
            transport=apim.transport(),
            sleep=lambda _: None,
            state_file=state_file,
            on_change=self._changed,
        )
        services = ApimTargets(chooser)
        app.dependency_overrides[get_targets] = lambda: chooser
        app.dependency_overrides[get_apim_targets] = lambda: services

    def _changed(self) -> None:
        self.changes += 1

    def saved(self) -> dict[str, Any]:
        return dict(json.loads(self.state_file.read_text(encoding="utf-8"))["target"])


@pytest.fixture
def apim() -> FakeApim:
    return FakeApim()


@pytest.fixture
def wizard(apim: FakeApim, tmp_path: Path) -> Iterator[_Wizard]:
    yield _Wizard(apim, _settings(**NO_TARGET), tmp_path / "state.json")
    app.dependency_overrides.clear()


def _create(wizard: _Wizard, **overrides: Any) -> Any:
    body = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-new",
        "location": "eastus2",
        "name": "apim-new",
        "publisher_email": "dev@contoso.com",
        "publisher_name": "Contoso",
        **overrides,
    }
    return wizard.client.post("/api/apim/target/services", json=body, headers=WRITE)


def test_without_a_service_setup_asks_to_choose_one(wizard: _Wizard) -> None:
    report = wizard.client.get("/api/setup").json()

    assert report["can_operate"] is False
    assert [check["id"] for check in report["checks"]] == ["sign_in", "gateway_target"]
    assert (
        report["checks"][1] | {"status": "action", "fix": "select_gateway"} == report["checks"][1]
    )
    assert wizard.client.get("/api/target").json() == {
        "product": None,
        "gateway_name": None,
        "gateway_kind": None,
        "subscription_id": None,
        "resource_group": None,
        "gateway_url": None,
        "tenant_id": None,
        "source": "none",
        "can_change": True,
        "cluster_id": None,
        "provisioning": False,
    }
    response = wizard.client.get("/api/apim/pools")
    assert response.status_code == 409
    assert "choose, create or connect one" in response.json()["detail"]


def test_lists_tenants_subscriptions_and_services(wizard: _Wizard, apim: FakeApim) -> None:
    mine = apim.add_service("apim-mine", tags={MANAGED_TAG: MANAGED_TAG_VALUE})
    other = apim.add_service("apim-other")
    apim.callers["token"] = "dev"
    apim.allow("dev", f"/subscriptions/{SUBSCRIPTION}", "Microsoft.ApiManagement/service/*/read")
    apim.allow("dev", f"/subscriptions/{SUBSCRIPTION}", "Microsoft.ApiManagement/service/read")
    apim.allow("dev", mine, "Microsoft.ApiManagement/service/*")

    assert wizard.client.get("/api/target/tenants").json() == [
        {"tenant_id": TENANT, "display_name": "Contoso", "default_domain": "contoso.com"}
    ]
    subscriptions = wizard.client.get("/api/target/subscriptions", params={"tenant_id": TENANT})
    assert [item["subscription_id"] for item in subscriptions.json()] == [SUBSCRIPTION]
    # Tokens are requested in the tenant being listed.
    assert TENANT in wizard.credential.tenants

    services = wizard.client.get(
        "/api/apim/target/services", params={"tenant_id": TENANT, "subscription_id": SUBSCRIPTION}
    ).json()
    assert [(item["id"], item["managed"], item["can_manage"]) for item in services] == [
        (mine, True, True),
        (other, False, False),
    ]
    assert "Microsoft.ApiManagement/service/backends/write" in services[1]["missing"]


def test_create_options(wizard: _Wizard, apim: FakeApim) -> None:
    apim.add_resource_group("rg-ai")
    apim.providers[SUBSCRIPTION] = {"Microsoft.ApiManagement": "NotRegistered"}

    options = wizard.client.get(
        "/api/apim/target/create-options", params={"subscription_id": SUBSCRIPTION}
    ).json()

    assert options == {
        "resource_groups": [{"name": "rg-ai", "location": "eastus2"}],
        # No canary regions, no restricted entries, no Consumption; display names normalized.
        "locations": {
            "BasicV2": ["eastus2", "swedencentral"],
            "StandardV2": [],
            "Developer": ["eastus2"],
        },
        "provider_registered": False,
    }


def test_select_saves_the_service(wizard: _Wizard, apim: FakeApim) -> None:
    apim.add_service("APIM-Prod", group="RG-Apim")
    body = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-apim",
        "apim_name": "apim-prod",
    }

    response = wizard.client.put("/api/apim/target", json=body, headers=WRITE)

    assert response.status_code == 200, response.text
    # Saved with ARM's casing, so it matches the IDs ARM returns later.
    assert wizard.saved() == {
        "product": "apim",
        "subscription_id": SUBSCRIPTION,
        "resource_group": "RG-Apim",
        "apim_name": "APIM-Prod",
        "tenant_id": TENANT,
    }
    assert response.json()["source"] == "saved"
    assert wizard.changes == 1


def test_select_refuses_a_service_still_being_created(wizard: _Wizard, apim: FakeApim) -> None:
    """It doesn't replace the current service; force chooses it to follow it in setup."""
    apim.add_service("apim-new", properties={"provisioningState": "Activating"})
    apim.add_service("apim-broken", properties={"provisioningState": "Failed"})
    apim.add_service("apim-scaling", properties={"provisioningState": "Updating"})
    body = {"tenant_id": TENANT, "subscription_id": SUBSCRIPTION, "resource_group": "rg-apim"}

    new = wizard.client.put(
        "/api/apim/target", json={**body, "apim_name": "apim-new"}, headers=WRITE
    )
    broken = wizard.client.put(
        "/api/apim/target", json={**body, "apim_name": "apim-broken"}, headers=WRITE
    )

    assert (new.status_code, broken.status_code) == (424, 424)
    assert "still being created (Activating)" in new.json()["detail"]
    assert "apim-broken is Failed" in broken.json()["detail"]
    assert wizard.changes == 0
    # An existing service keeps working while Azure updates it.
    scaling = {**body, "apim_name": "apim-scaling"}
    assert wizard.client.put("/api/apim/target", json=scaling, headers=WRITE).status_code == 200
    forced = {**body, "apim_name": "apim-new", "force": True}
    assert wizard.client.put("/api/apim/target", json=forced, headers=WRITE).status_code == 200
    assert wizard.saved()["apim_name"] == "apim-new"


def test_select_needs_the_write_header_and_an_existing_service(wizard: _Wizard) -> None:
    body = {"subscription_id": SUBSCRIPTION, "resource_group": "rg", "apim_name": "missing"}
    assert wizard.client.put("/api/apim/target", json=body).status_code == 403

    response = wizard.client.put("/api/apim/target", json=body, headers=WRITE)

    assert response.status_code == 404
    assert not wizard.state_file.exists()
    assert wizard.changes == 0


def test_select_without_a_tenant_reads_it_from_the_subscription(
    wizard: _Wizard, apim: FakeApim
) -> None:
    apim.add_service("apim-prod")
    body = {"subscription_id": SUBSCRIPTION, "resource_group": "rg-apim", "apim_name": "apim-prod"}

    assert wizard.client.put("/api/apim/target", json=body, headers=WRITE).status_code == 200
    assert wizard.saved()["tenant_id"] == TENANT


def test_create_registers_the_provider_and_creates_the_group(
    wizard: _Wizard, apim: FakeApim
) -> None:
    apim.providers[SUBSCRIPTION] = {"Microsoft.ApiManagement": "NotRegistered"}

    response = _create(wizard)

    assert response.status_code == 200, response.text
    assert response.json()["provisioning_state"] == "Activating"
    assert apim.providers[SUBSCRIPTION]["Microsoft.ApiManagement"] == "Registered"
    group = apim.resource_groups[f"/subscriptions/{SUBSCRIPTION}/resourcegroups/rg-new"]
    assert group["tags"] == {MANAGED_TAG: MANAGED_TAG_VALUE}
    put = next(
        request
        for request in apim.requests
        if request.method == "PUT" and request.url.path == NEW_SERVICE
    )
    assert put.headers["If-None-Match"] == "*"
    assert json.loads(put.content) == {
        "location": "eastus2",
        "sku": {"name": "BasicV2", "capacity": 1},
        "identity": {"type": "SystemAssigned"},
        "tags": {MANAGED_TAG: MANAGED_TAG_VALUE},
        "properties": {"publisherEmail": "dev@contoso.com", "publisherName": "Contoso"},
    }
    assert wizard.saved() == {
        "product": "apim",
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-new",
        "apim_name": "apim-new",
        "tenant_id": TENANT,
    }
    assert wizard.changes == 1


def test_create_uses_an_existing_group_as_is(wizard: _Wizard, apim: FakeApim) -> None:
    apim.add_resource_group("rg-new")

    assert _create(wizard, sku="Developer").status_code == 200
    assert not any(
        request.method == "PUT" and request.url.path.casefold().endswith("/resourcegroups/rg-new")
        for request in apim.requests
    )


def test_create_never_touches_existing_services(wizard: _Wizard, apim: FakeApim) -> None:
    apim.add_resource_group("rg-new")
    apim.add_service("apim-new", group="rg-new")
    apim.taken_names.add("apim-elsewhere")

    exists = _create(wizard)
    taken = _create(wizard, name="apim-elsewhere")

    assert exists.status_code == 409
    assert "already exists" in exists.json()["detail"]
    assert taken.status_code == 409
    assert "already in use" in taken.json()["detail"]
    assert not any(request.method == "PUT" for request in apim.requests)
    assert not wizard.state_file.exists()


def test_create_reports_missing_rights(wizard: _Wizard, apim: FakeApim) -> None:
    apim.callers["token"] = "dev"
    apim.allow("dev", f"/subscriptions/{SUBSCRIPTION}", "*/read")

    response = _create(wizard)

    assert response.status_code == 403
    assert response.json()["detail"].startswith("Your Azure account cannot create")


def test_create_validates_the_request(wizard: _Wizard) -> None:
    assert _create(wizard, name="1-starts-with-digit").status_code == 422
    assert _create(wizard, sku="Consumption").status_code == 422
    assert _create(wizard, publisher_email="nobody").status_code == 422


def test_new_service_is_pending_until_activated(apim: FakeApim) -> None:
    apim.service_provisioning = "Activating"
    client = _client(apim, _settings())
    try:
        report = client.get("/api/setup").json()
    finally:
        app.dependency_overrides.clear()

    check = report["checks"][-1]
    expected = {"id": "gateway_target", "status": "pending", "detail": "Activating"}
    assert check | expected == check
    # The wizard shows how long it has been running, from ARM's systemData rather than
    # properties.createdAtUtc, which is 0001-01-01 until the service is up.
    assert check["started_at"] == "2026-10-25T10:00:00.1234567Z"
    assert report["can_operate"] is False


def test_failed_service_needs_attention_but_is_not_assumed_unusable(apim: FakeApim) -> None:
    apim.service_provisioning = "Failed"
    client = _client(apim, _settings())
    try:
        report = client.get("/api/setup").json()
    finally:
        app.dependency_overrides.clear()

    checks = {check["id"]: check for check in report["checks"]}
    assert (
        checks["gateway_target"] | {"status": "action", "detail": "Failed"}
        == checks["gateway_target"]
    )
    assert checks["gateway_target"]["started_at"] is None
    # A failed update leaves the service readable, so the rest of the checks still run.
    assert checks["gateway_access"]["status"] == "ok"
    assert report["can_operate"] is True
    assert report["ready"] is False


def test_service_from_the_environment_is_fixed(apim: FakeApim, tmp_path: Path) -> None:
    wizard = _Wizard(apim, _settings(), tmp_path / "state.json")
    try:
        target = wizard.client.get("/api/target").json()
        body = {"subscription_id": SUBSCRIPTION, "resource_group": "rg", "apim_name": "apim"}
        response = wizard.client.put("/api/apim/target", json=body, headers=WRITE)
        checks = {check["id"]: check for check in wizard.client.get("/api/setup").json()["checks"]}
    finally:
        app.dependency_overrides.clear()

    assert (target["source"], target["can_change"]) == ("environment", False)
    assert response.status_code == 409
    assert checks["gateway_target"]["fix"] is None


def test_deployed_app_cannot_choose(apim: FakeApim, tmp_path: Path) -> None:
    wizard = _Wizard(apim, _settings(auth_mode="easy_auth"), tmp_path / "state.json")
    headers = {"X-MS-CLIENT-PRINCIPAL-NAME": "dev@contoso.com"}
    try:
        response = wizard.client.get("/api/target/tenants", headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


# -- configuration ----------------------------------------------------------------------


def test_settings_use_the_saved_service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    config.save_target(
        "apim",
        {"subscription_id": SUBSCRIPTION, "resource_group": "rg", "apim_name": "apim"},
        state,
    )
    monkeypatch.setattr(config, "STATE_FILE", state)
    monkeypatch.setenv("POOL_MANAGER_AUTH_MODE", "disabled")
    for name in ("SUBSCRIPTION_ID", "RESOURCE_GROUP", "APIM_NAME", "TENANT_ID"):
        monkeypatch.delenv(f"POOL_MANAGER_{name}", raising=False)
    monkeypatch.chdir(tmp_path)  # No .env here.
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
    finally:
        config.get_settings.cache_clear()

    assert (settings.subscription_id, settings.apim_name) == (SUBSCRIPTION, "apim")
    assert settings.can_choose_target is True


def test_saved_target_needs_every_field(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"target": {"subscription_id": SUBSCRIPTION}}), encoding="utf-8")
    assert read_saved_target(state) is None
    state.write_text("not json", encoding="utf-8")
    assert read_saved_target(state) is None
    assert read_saved_target(tmp_path / "missing.json") is None


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"apim_name": ""}, "Set all of"),
        ({**NO_TARGET, "auth_mode": "easy_auth"}, "Set POOL_MANAGER_SUBSCRIPTION_ID"),
    ],
)
def test_partial_or_missing_target_is_rejected(values: dict[str, str], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        _settings(**values).validate_runtime()


# -- deleting and restoring ---------------------------------------------------------------


def _mine(apim: FakeApim, name: str = "apim-mine", managed: bool = True) -> dict[str, str]:
    service_id = apim.add_service(name, tags={MANAGED_TAG: MANAGED_TAG_VALUE} if managed else {})
    apim.service_contents[service_id.casefold()] = {
        "backends": [
            {"name": "pool-a", "properties": {"type": "Pool"}},
            {"name": "member-1", "properties": {"type": "Single"}},
            {"name": "member-2", "properties": {"type": "Single"}},
        ],
        "apis": [{"name": "api-a"}, {"name": "api-a;rev=2"}],
    }
    return {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-apim",
        "apim_name": name,
    }


def test_delete_check_lists_what_would_be_lost(wizard: _Wizard, apim: FakeApim) -> None:
    body = _mine(apim)

    check = wizard.client.post("/api/apim/target/services/delete-check", json=body, headers=WRITE)

    assert check.status_code == 200, check.text
    assert check.json() | {"pools": 1, "members": 2, "apis": 1, "current": False} == check.json()


def test_only_services_this_app_created_can_be_deleted(wizard: _Wizard, apim: FakeApim) -> None:
    body = _mine(apim, "apim-theirs", managed=False)

    check = wizard.client.post("/api/apim/target/services/delete-check", json=body, headers=WRITE)
    delete = wizard.client.post(
        "/api/apim/target/services/delete",
        json={**body, "confirm_name": "apim-theirs"},
        headers=WRITE,
    )

    assert check.status_code == delete.status_code == 403
    assert "Only APIM services this app created" in delete.json()["detail"]
    assert not any(r.method == "DELETE" for r in apim.requests)


def test_delete_needs_the_name_typed_again(wizard: _Wizard, apim: FakeApim) -> None:
    body = _mine(apim)

    no_header = wizard.client.post(
        "/api/apim/target/services/delete", json={**body, "confirm_name": "apim-mine"}
    )
    wrong = wizard.client.post(
        "/api/apim/target/services/delete", json={**body, "confirm_name": "apim-min"}, headers=WRITE
    )

    assert no_header.status_code == 403
    assert wrong.status_code == 400
    assert "apim-mine" in wrong.json()["detail"]
    assert not any(r.method == "DELETE" for r in apim.requests)


def test_delete_soft_deletes_and_restore_brings_it_back(wizard: _Wizard, apim: FakeApim) -> None:
    body = _mine(apim)

    deleted = wizard.client.post(
        "/api/apim/target/services/delete",
        json={**body, "confirm_name": "APIM-Mine"},
        headers=WRITE,
    )
    assert deleted.status_code == 204, deleted.text
    params = {"tenant_id": TENANT, "subscription_id": SUBSCRIPTION}
    assert wizard.client.get("/api/apim/target/services", params=params).json() == []
    listed = wizard.client.get("/api/apim/target/deleted", params=params).json()
    assert [(d["name"], d["location"], d["resource_group"]) for d in listed] == [
        ("apim-mine", "eastus2", "rg-apim")
    ]
    assert listed[0]["scheduled_purge_date"] == "2026-10-26T08:00:00Z"
    # Not the current service, so the saved choice is untouched.
    assert wizard.changes == 0

    restore = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "location": "eastus2",
        "name": "apim-mine",
    }
    restored = wizard.client.post("/api/apim/target/deleted/restore", json=restore, headers=WRITE)

    assert restored.status_code == 200, restored.text
    assert (restored.json()["resource_group"], restored.json()["managed"]) == ("rg-apim", True)
    put = next(
        r
        for r in reversed(apim.requests)
        if r.method == "PUT" and r.url.path.endswith("/apim-mine")
    )
    assert json.loads(put.content)["properties"]["restore"] is True
    assert [
        s["name"] for s in wizard.client.get("/api/apim/target/services", params=params).json()
    ] == ["apim-mine"]
    assert wizard.client.get("/api/apim/target/deleted", params=params).json() == []
    again = wizard.client.post("/api/apim/target/deleted/restore", json=restore, headers=WRITE)
    assert again.status_code == 404


def test_deleting_the_current_service_forgets_it(apim: FakeApim, tmp_path: Path) -> None:
    target = {
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-apim",
        "apim_name": "apim-mine",
    }
    state = tmp_path / "state.json"
    config.save_target("apim", target, state)
    wizard = _Wizard(apim, _settings(**NO_TARGET).with_saved_target(target), state)
    body = _mine(apim)
    try:
        check = wizard.client.post(
            "/api/apim/target/services/delete-check", json=body, headers=WRITE
        )
        deleted = wizard.client.post(
            "/api/apim/target/services/delete",
            json={**body, "confirm_name": "apim-mine"},
            headers=WRITE,
        )
    finally:
        app.dependency_overrides.clear()

    assert check.json()["current"] is True
    assert deleted.status_code == 204
    assert not state.exists()
    assert wizard.changes == 1


def test_a_fixed_or_deployed_service_cannot_be_deleted_here(apim: FakeApim, tmp_path: Path) -> None:
    body = _mine(apim)
    for settings, status in ((_settings(), 409), (_settings(auth_mode="easy_auth"), 400)):
        wizard = _Wizard(apim, settings, tmp_path / "state.json")
        wizard.client.headers.update({"X-MS-CLIENT-PRINCIPAL-NAME": "dev@contoso.com"})
        try:
            response = wizard.client.post(
                "/api/apim/target/services/delete",
                json={**body, "confirm_name": "apim-mine"},
                headers=WRITE,
            )
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == status, (settings.auth_mode, response.text)
    assert "apim-mine" not in {r["name"] for r in apim.deleted_services.values()}
