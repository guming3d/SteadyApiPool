"""Choosing, deploying, connecting and deleting the gateway in the local setup wizard."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pool_manager import config
from pool_manager.app import app
from pool_manager.config import Settings, read_saved_target
from pool_manager.litellm.gateway_template import LITELLM_IMAGE, aca_template
from pool_manager.secret_store import SecretStore
from tests.fake_azure import SUBSCRIPTION, TENANT, FakeAzure
from tests.litellm.fake_litellm import MASTER_KEY, URL
from tests.litellm.test_app import WRITE, _client, _settings

NO_TARGET = {"subscription_id": "", "resource_group": "", "gateway_name": ""}
DEPLOYMENT = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-new"
    "/providers/Microsoft.Resources/deployments/ai-gateway-gw-new"
)


class _Wizard:
    def __init__(self, azure: FakeAzure, settings: Settings, tmp_path: Path) -> None:
        self.state_file = tmp_path / "state.json"
        self.secrets = SecretStore(tmp_path / "keys.bin", persistent=False)
        self.client = _client(azure, settings, state_file=self.state_file, secrets=self.secrets)

    def saved(self) -> dict[str, Any]:
        return dict(json.loads(self.state_file.read_text(encoding="utf-8"))["target"])


@pytest.fixture
def azure() -> FakeAzure:
    fake = FakeAzure()
    fake.add_gateway("gw-dev", group="rg-ai-gw", subscription=SUBSCRIPTION)
    return fake


@pytest.fixture
def wizard(azure: FakeAzure, tmp_path: Path) -> Iterator[_Wizard]:
    yield _Wizard(azure, _settings(**NO_TARGET), tmp_path)
    app.dependency_overrides.clear()


def _create(wizard: _Wizard, **overrides: Any) -> Any:
    body = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-new",
        "location": "eastus2",
        "name": "gw-new",
        **overrides,
    }
    return wizard.client.post("/api/litellm/target/gateways", json=body, headers=WRITE)


def _select(name: str = "gw-dev", group: str = "rg-ai-gw") -> dict[str, Any]:
    return {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": group,
        "gateway_name": name,
    }


def test_without_a_gateway_setup_asks_to_choose_one(wizard: _Wizard) -> None:
    report = wizard.client.get("/api/setup").json()

    assert report["can_operate"] is False
    assert [check["id"] for check in report["checks"]] == ["sign_in", "gateway_target"]
    target = report["checks"][1]
    assert target | {"status": "action", "fix": "select_gateway"} == target
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


def test_lists_litellm_gateways(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.add_gateway("by-hand", group="rg-x", subscription=SUBSCRIPTION, managed=False)
    azure.add_gateway(
        "web", group="rg-x", subscription=SUBSCRIPTION, managed=False, image="nginx:1"
    )
    assert wizard.client.get("/api/target/tenants").json()[0]["tenant_id"] == TENANT

    gateways = wizard.client.get(
        "/api/litellm/target/gateways", params={"subscription_id": SUBSCRIPTION}
    ).json()

    assert [(g["name"], g["managed"], g["profile"]) for g in gateways] == [
        ("by-hand", False, None),
        ("gw-dev", True, "dev"),
    ]
    assert gateways[1]["url"] == "https://gw.example.azurecontainerapps.io"
    assert gateways[1]["can_manage"] is True


def test_listing_shows_who_cant_read_the_key(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.allow("app", f"/subscriptions/{SUBSCRIPTION}", "*/read")

    gateways = wizard.client.get(
        "/api/litellm/target/gateways", params={"subscription_id": SUBSCRIPTION}
    ).json()

    assert gateways[0]["can_manage"] is False
    assert gateways[0]["missing"] == ["Microsoft.App/containerApps/listSecrets/action"]


def test_create_options(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.add_resource_group("rg-one")
    azure.providers[SUBSCRIPTION] = {"Microsoft.DBforPostgreSQL": "NotRegistered"}

    options = wizard.client.get(
        "/api/litellm/target/create-options", params={"subscription_id": SUBSCRIPTION}
    ).json()

    assert options["resource_groups"] == [{"name": "rg-one", "location": "eastus2"}]
    assert options["locations"] == ["eastus2", "swedencentral"]  # No canary regions.
    assert options["unregistered_providers"] == ["Microsoft.DBforPostgreSQL"]


def test_select_saves_the_gateway(wizard: _Wizard) -> None:
    response = wizard.client.put("/api/litellm/target", json=_select(), headers=WRITE)

    assert response.status_code == 200, response.text
    assert response.json()["gateway_kind"] == "aca"
    assert (
        wizard.saved()
        | {
            "subscription_id": SUBSCRIPTION,
            "resource_group": "rg-ai-gw",
            "gateway_name": "gw-dev",
            "tenant_id": TENANT,
        }
        == wizard.saved()
    )
    assert "master" not in json.dumps(wizard.saved()).casefold()


def test_select_refuses_what_isnt_a_gateway(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.add_gateway(
        "web", group="rg-x", subscription=SUBSCRIPTION, managed=False, image="nginx:1"
    )

    assert wizard.client.put("/api/litellm/target", json=_select()).status_code == 403  # CSRF
    missing = wizard.client.put("/api/litellm/target", json=_select("nope"), headers=WRITE)
    web = wizard.client.put("/api/litellm/target", json=_select("web", "rg-x"), headers=WRITE)

    assert missing.status_code == 404
    assert web.status_code == 409


# -- deploying ------------------------------------------------------------------------------


def test_deploying_runs_the_template_with_new_secrets(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.providers[SUBSCRIPTION] = {"Microsoft.App": "NotRegistered"}

    response = _create(wizard, profile="production")

    assert response.status_code == 200, response.text
    assert response.json()["provisioning_state"] == "Deploying"
    # The provider was registered and the group created, tagged as this app's.
    assert azure.providers[SUBSCRIPTION]["Microsoft.App"] == "Registered"
    group = azure.resource_groups[f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-new".casefold()]
    assert group["tags"] == {"managed-by": "ai-gateway-pool"}
    deployment = azure.arm_deployments[DEPLOYMENT.casefold()]
    assert deployment["template"] == aca_template()
    parameters = deployment["parameters"]
    assert (parameters["name"], parameters["profile"], parameters["image"]) == (
        "gw-new",
        "production",
        LITELLM_IMAGE,
    )
    assert parameters["masterKey"].startswith("sk-") and len(parameters["masterKey"]) > 40
    assert parameters["saltKey"] != parameters["masterKey"]
    assert parameters["postgresPassword"].isalnum()
    assert "enable_azure_ad_token_refresh: true" in parameters["litellmConfig"]
    assert "redis_host: os.environ/REDIS_HOST" in parameters["litellmConfig"]
    assert "azuremonitor:" in parameters["collectorConfig"]
    # Selected at once; setup follows the deployment.
    assert wizard.saved()["gateway_name"] == "gw-new"


def test_a_dev_gateway_has_no_redis(wizard: _Wizard, azure: FakeAzure) -> None:
    assert _create(wizard).status_code == 200

    parameters = azure.arm_deployments[DEPLOYMENT.casefold()]["parameters"]
    assert parameters["profile"] == "dev"
    assert "redis" not in parameters["litellmConfig"]


def test_deploy_status_follows_the_deployment(wizard: _Wizard, azure: FakeAzure) -> None:
    params = {"subscription_id": SUBSCRIPTION, "resource_group": "rg-new", "name": "gw-new"}
    assert (
        wizard.client.get("/api/litellm/target/deploy-status", params=params).json()["state"]
        == "none"
    )
    _create(wizard)

    running = wizard.client.get("/api/litellm/target/deploy-status", params=params).json()
    assert running["state"] == "running"
    assert [(step["name"], step["state"]) for step in running["steps"]] == [
        ("psql-gw-new", "Running"),
        ("id-gw-new", "Succeeded"),
    ]
    azure.finish_deployment("gw-new")
    done = wizard.client.get("/api/litellm/target/deploy-status", params=params).json()
    assert done["state"] == "succeeded"


def test_a_failed_deployment_says_why(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.deployment_failure = "The server name psql-gw-new is already in use."
    _create(wizard)

    status = wizard.client.get(
        "/api/litellm/target/deploy-status",
        params={"subscription_id": SUBSCRIPTION, "resource_group": "rg-new", "name": "gw-new"},
    ).json()

    assert status["state"] == "failed"
    assert status["error"] == "Conflict: The server name psql-gw-new is already in use."
    assert status["steps"][0]["error"] == status["error"]


def test_a_region_without_postgres_is_refused_before_anything_is_created(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    azure.postgres_restricted = {
        "southcentralus": "Subscriptions are restricted from provisioning in this region.",
        "eastus2": "Provisioning is restricted in this region.",
    }

    response = _create(wizard, location="southcentralus")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "can't be created in southcentralus" in detail
    assert "Subscriptions are restricted from provisioning in this region." in detail
    # Open regions are offered; restricted ones aren't.
    assert "swedencentral" in detail and "eastus2" not in detail
    assert not azure.arm_deployments
    assert f"/subscriptions/{SUBSCRIPTION}/resourcegroups/rg-new" not in azure.resource_groups


def test_a_failed_deployment_can_be_cleaned_up(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.deployment_failure = "ParameterOutOfRange: The value of the 'Version' should be in: []."
    _create(wizard)
    # What the deployment created before it failed; no Container App.
    leftover = (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-new/providers"
        "/Microsoft.ManagedIdentity/userAssignedIdentities/id-gw-new"
    )
    azure.resources[leftover.casefold()] = {
        "id": leftover,
        "name": "id-gw-new",
        "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
        "tags": {"managed-by": "ai-gateway-pool", "ai-gateway": "gw-new"},
    }
    body = _select("gw-new", "rg-new")

    check = wizard.client.post(
        "/api/litellm/target/gateways/delete-check", json=body, headers=WRITE
    )
    deleted = wizard.client.post(
        "/api/litellm/target/gateways/delete",
        json={**body, "confirm_name": "gw-new"},
        headers=WRITE,
    )

    assert check.status_code == 200, check.text
    assert check.json()["deployed"] is False
    assert check.json()["resources"] == [
        "Microsoft.ManagedIdentity/userAssignedIdentities/id-gw-new"
    ]
    assert deleted.status_code == 204, deleted.text
    assert leftover.casefold() not in azure.resources
    assert DEPLOYMENT.casefold() not in azure.arm_deployments
    # The name can be deployed again.
    azure.deployment_failure = None
    assert _create(wizard).status_code == 200


def test_a_name_whose_resources_are_still_there_isnt_deployed_again(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    azure.add_resource_group("rg-new")
    leftover = (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-new/providers"
        "/Microsoft.App/managedEnvironments/cae-gw-new"
    )
    azure.resources[leftover.casefold()] = {
        "id": leftover,
        "name": "cae-gw-new",
        "type": "Microsoft.App/managedEnvironments",
        "tags": {"managed-by": "ai-gateway-pool", "ai-gateway": "gw-new"},
    }

    response = _create(wizard)

    assert response.status_code == 409
    assert "cae-gw-new" in response.json()["detail"]
    assert not azure.arm_deployments


def test_deploying_never_replaces_a_gateway(wizard: _Wizard, azure: FakeAzure) -> None:
    taken = _create(wizard, name="gw-dev", resource_group="rg-ai-gw")
    assert taken.status_code == 409
    assert _create(wizard).status_code == 200
    again = _create(wizard)
    assert again.status_code == 409
    assert "already being deployed" in again.json()["detail"]


def test_deploying_reports_missing_rights(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.allow("app", f"/subscriptions/{SUBSCRIPTION}", "*/read")

    response = _create(wizard)

    assert response.status_code == 403
    assert "cannot deploy this gateway" in response.json()["detail"]
    assert not azure.arm_deployments


def test_gateway_names_are_checked(wizard: _Wizard) -> None:
    for name in ("Upper", "a", "trailing-", "double--hyphen", "x" * 33):
        assert _create(wizard, name=name).status_code == 422, name


def test_a_deployed_gateway_reads_back(azure: FakeAzure, tmp_path: Path) -> None:
    wizard = _Wizard(azure, _settings(**NO_TARGET), tmp_path)
    try:
        _create(wizard)
        azure.finish_deployment("gw-new")
    finally:
        app.dependency_overrides.clear()
    # The wizard saved the choice; a restart picks it up.
    saved = read_saved_target(wizard.state_file)
    assert saved is not None and saved["gateway_name"] == "gw-new"
    client = _client(azure, _settings(**NO_TARGET).with_saved_target(saved))
    try:
        target = client.get("/api/target").json()
        caps = client.get("/api/litellm/capabilities").json()
    finally:
        app.dependency_overrides.clear()

    assert target["source"] == "saved"
    assert caps["gateway_url"] == "https://gw-new.example.azurecontainerapps.io"


# -- external gateways ------------------------------------------------------------------------


def _connect(wizard: _Wizard, **overrides: Any) -> Any:
    body = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "name": "aks-gateway",
        "url": URL,
        "master_key": MASTER_KEY,
        "principal_id": "00000000-0000-0000-0000-0000000000bb",
        **overrides,
    }
    return wizard.client.post("/api/litellm/target/connect", json=body, headers=WRITE)


def test_connecting_checks_the_key_and_keeps_it_out_of_the_state_file(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    response = _connect(wizard)

    assert response.status_code == 200, response.text
    assert response.json()["gateway_kind"] == "external"
    saved = wizard.saved()
    assert saved["gateway_url"] == URL
    assert saved["gateway_principal_id"] == "00000000-0000-0000-0000-0000000000bb"
    assert MASTER_KEY not in wizard.state_file.read_text(encoding="utf-8")
    assert wizard.secrets.get(URL) == MASTER_KEY


def test_connecting_refuses_a_wrong_key_or_plain_http(wizard: _Wizard, azure: FakeAzure) -> None:
    wrong = _connect(wizard, master_key="sk-wrong-key")
    insecure = _connect(wizard, url="http://gateway.example.com")
    with_path = _connect(wizard, url=f"{URL}/v1")

    assert wrong.status_code == 400 and "refused" in wrong.json()["detail"]
    assert insecure.status_code == 400 and "HTTPS" in insecure.json()["detail"]
    assert with_path.status_code == 400
    assert not wizard.state_file.exists()


def test_connecting_needs_a_database(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.litellm.db = "Not connected"

    response = _connect(wizard)

    assert response.status_code == 409
    assert "STORE_MODEL_IN_DB" in response.json()["detail"]


def test_a_connected_gateway_works_with_its_stored_key(azure: FakeAzure, tmp_path: Path) -> None:
    wizard = _Wizard(azure, _settings(**NO_TARGET), tmp_path)
    try:
        _connect(wizard)
    finally:
        app.dependency_overrides.clear()
    saved = read_saved_target(wizard.state_file)
    assert saved is not None
    client = _client(azure, _settings(**NO_TARGET).with_saved_target(saved), secrets=wizard.secrets)
    try:
        assert client.get("/api/litellm/pools").json() == []
        caps = client.get("/api/litellm/capabilities").json()
        assert client.post("/api/litellm/target/disconnect", headers=WRITE).status_code == 204
    finally:
        app.dependency_overrides.clear()

    assert caps["gateway_kind"] == "external"
    assert caps["gateway_principal_id"] == "00000000-0000-0000-0000-0000000000bb"
    assert wizard.secrets.get(URL) is None


def test_the_aks_bundle(wizard: _Wizard) -> None:
    bundle = wizard.client.get(
        "/api/litellm/target/aks-bundle",
        params={
            "subscription_id": SUBSCRIPTION,
            "resource_group": "rg-aks",
            "cluster": "aks-prod",
            "name": "gw-aks",
            "location": "eastus2",
        },
    ).json()

    files = {item["path"]: item["content"] for item in bundle["files"]}
    assert set(files) == {"values.yaml", "deploy.sh", "README.md"}
    assert "repository: ghcr.io/berriai/litellm" in files["values.yaml"]
    assert "azure.workload.identity/use:" in files["values.yaml"]
    assert "enable_azure_ad_token_refresh: true" in files["values.yaml"]
    assert 'CLUSTER="aks-prod"' in files["deploy.sh"]
    assert "helm upgrade --install litellm oci://ghcr.io/berriai/litellm-helm" in files["deploy.sh"]
    assert "sk-" not in files["values.yaml"]  # Secrets are made where the script runs.


# -- the environment and the deployed app ---------------------------------------------------


def test_gateway_from_the_environment_is_fixed(azure: FakeAzure, tmp_path: Path) -> None:
    wizard = _Wizard(azure, _settings(), tmp_path)
    try:
        assert wizard.client.get("/api/target").json()["source"] == "environment"
        response = wizard.client.put("/api/litellm/target", json=_select(), headers=WRITE)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409
    assert "_GATEWAY_NAME" in response.json()["detail"]


def test_deployed_app_cannot_choose(azure: FakeAzure, tmp_path: Path) -> None:
    wizard = _Wizard(azure, _settings(auth_mode="easy_auth", owners="dev@contoso.com"), tmp_path)
    headers = {**WRITE, "X-MS-CLIENT-PRINCIPAL-NAME": "dev@contoso.com"}
    try:
        response = wizard.client.put("/api/litellm/target", json=_select(), headers=headers)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400


def test_settings_use_the_saved_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    config.save_target(
        "litellm",
        {"subscription_id": "s", "resource_group": "g", "gateway_name": "gw", "tenant_id": "t"},
        state,
    )
    monkeypatch.setattr(config, "STATE_FILE", state)
    monkeypatch.setenv("POOL_MANAGER_AUTH_MODE", "disabled")
    for name in ("SUBSCRIPTION_ID", "RESOURCE_GROUP", "GATEWAY_NAME", "GATEWAY_URL"):
        monkeypatch.delenv(f"POOL_MANAGER_{name}", raising=False)
    config.get_settings.cache_clear()
    try:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        saved = read_saved_target()
        assert saved is not None
        settings = settings.with_saved_target(saved)
    finally:
        config.get_settings.cache_clear()
    assert (settings.gateway_name, settings.tenant_id, settings.can_choose_target) == (
        "gw",
        "t",
        True,
    )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"subscription_id": "s"}, "Set all of"),
        ({"auth_mode": "easy_auth"}, "Set POOL_MANAGER_SUBSCRIPTION_ID"),
        ({"gateway_url": URL}, "GATEWAY_NAME"),
    ],
)
def test_partial_or_missing_target_is_rejected(values: dict[str, str], message: str) -> None:
    settings = Settings(_env_file=None, **{"auth_mode": "disabled", **values})  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match=message):
        settings.validate_runtime()


# -- deleting ---------------------------------------------------------------------------------


def _delete_body(name: str = "gw-dev", **overrides: Any) -> dict[str, Any]:
    return {**_select(name), "confirm_name": name, **overrides}


def test_delete_check_lists_what_would_be_deleted(wizard: _Wizard) -> None:
    check = wizard.client.post(
        "/api/litellm/target/gateways/delete-check", json=_select(), headers=WRITE
    ).json()

    assert check["current"] is False
    assert check["deployed"] is True
    assert check["resources"] == [
        "Microsoft.App/containerApps/gw-dev",
        "Microsoft.App/managedEnvironments/cae-gw-dev",
        "Microsoft.DBforPostgreSQL/flexibleServers/psql-gw-dev-x1",
        "Microsoft.ManagedIdentity/userAssignedIdentities/id-gw-dev",
        "Microsoft.Insights/components/appi-gw-dev",
        "Microsoft.OperationalInsights/workspaces/log-gw-dev",
    ]


def test_only_gateways_this_app_deployed_can_be_deleted(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.add_gateway("by-hand", group="rg-x", subscription=SUBSCRIPTION, managed=False)

    response = wizard.client.post(
        "/api/litellm/target/gateways/delete",
        json=_delete_body("by-hand", resource_group="rg-x"),
        headers=WRITE,
    )

    assert response.status_code == 403
    assert len([key for key in azure.container_apps if "by-hand" in key]) == 1


def test_delete_needs_the_name_typed_again(wizard: _Wizard, azure: FakeAzure) -> None:
    response = wizard.client.post(
        "/api/litellm/target/gateways/delete", json=_delete_body(confirm_name="gw"), headers=WRITE
    )

    assert response.status_code == 400
    assert any("gw-dev" in key for key in azure.container_apps)


def test_delete_removes_the_gateways_resources_but_not_the_group(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    azure.add_resource_group("rg-ai-gw")
    unrelated = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-ai-gw/providers/X/y/other"
    azure.resources[unrelated.casefold()] = {"id": unrelated, "name": "other", "type": "X/y"}

    response = wizard.client.post(
        "/api/litellm/target/gateways/delete", json=_delete_body(), headers=WRITE
    )

    assert response.status_code == 204, response.text
    assert not any("gw-dev" in key for key in azure.container_apps)
    assert not any("gw-dev" in key for key in azure.resources)
    assert unrelated.casefold() in azure.resources
    assert f"/subscriptions/{SUBSCRIPTION}/resourcegroups/rg-ai-gw" in azure.resource_groups
    deletes = [r.url.path for r in azure.requests if r.method == "DELETE"]
    assert deletes[0].endswith("/containerApps/gw-dev")  # The app goes first.
    assert deletes[1].endswith("/managedEnvironments/cae-gw-dev")


def test_delete_doesnt_wait_for_the_environment(wizard: _Wizard, azure: FakeAzure) -> None:
    azure.slow_deletes = {"microsoft.app/managedenvironments"}

    first = wizard.client.post(
        "/api/litellm/target/gateways/delete", json=_delete_body(), headers=WRITE
    )

    assert first.status_code == 204, first.text
    # The environment is left to Azure; the rest is gone without waiting for it.
    left = [item["name"] for item in azure.resources.values() if "gw-dev" in item["name"]]
    assert left == ["cae-gw-dev"]
    assert azure.operation_polls == 0
    # Asked again while Azure is still deleting it, the environment isn't asked twice.
    azure.add_gateway("gw-dev", group="rg-ai-gw", subscription=SUBSCRIPTION)
    azure.resources[next(k for k in azure.resources if k.endswith("/cae-gw-dev"))]["properties"] = {
        "provisioningState": "ScheduledForDelete"
    }
    before = len([r for r in azure.requests if r.method == "DELETE" and "cae-gw-dev" in r.url.path])
    again = wizard.client.post(
        "/api/litellm/target/gateways/delete", json=_delete_body(), headers=WRITE
    )
    after = len([r for r in azure.requests if r.method == "DELETE" and "cae-gw-dev" in r.url.path])
    assert again.status_code == 204, again.text
    assert after == before


def test_deleting_the_current_gateway_forgets_it(azure: FakeAzure, tmp_path: Path) -> None:
    settings = _settings(
        subscription_id=SUBSCRIPTION, resource_group="rg-ai-gw", gateway_name="gw-dev"
    ).with_saved_target(
        {"subscription_id": SUBSCRIPTION, "resource_group": "rg-ai-gw", "gateway_name": "gw-dev"}
    )
    wizard = _Wizard(azure, settings, tmp_path)
    wizard.state_file.write_text("{}", encoding="utf-8")
    try:
        check = wizard.client.post(
            "/api/litellm/target/gateways/delete-check", json=_select(), headers=WRITE
        ).json()
        response = wizard.client.post(
            "/api/litellm/target/gateways/delete", json=_delete_body(), headers=WRITE
        )
    finally:
        app.dependency_overrides.clear()
    assert check["current"] is True
    assert response.status_code == 204
    assert not wizard.state_file.exists()
