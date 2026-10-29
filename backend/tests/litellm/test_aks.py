"""Deploying a LiteLLM gateway on a new AKS cluster from the local setup wizard: the template,
the checks before it, the install in the cluster (Run Command), HTTPS, and handing it over."""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pool_manager.app import app
from pool_manager.litellm.aks_deploy import AKS_SERVER_SCOPE
from pool_manager.litellm.gateway_template import (
    AKS_COMMAND,
    AksInstall,
    InstallSize,
    aks_bootstrap,
    aks_bundle,
    aks_secrets,
    aks_template,
)
from pool_manager.secret_store import SecretStore
from tests.fake_azure import SUBSCRIPTION, TENANT, FakeAzure
from tests.litellm.test_app import PRODUCTION, TRIAL, WRITE, _client, _settings

NO_TARGET = {"subscription_id": "", "resource_group": "", "gateway_name": ""}
DEPLOYMENT = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-new"
    "/providers/Microsoft.Resources/deployments/ai-gateway-gw-aks"
)
CLUSTER = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-new"
    "/providers/Microsoft.ContainerService/managedClusters/gw-aks"
)
URL = "https://gw-aks.swedencentral.cloudapp.azure.com"


def _jwt(**claims: Any) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


USER_TOKEN = _jwt(oid="user-oid", tid=TENANT, upn="alex@contoso.com")


class _Token:
    token = USER_TOKEN
    expires_on = 4_102_444_800


class _UserCredential:
    """The local sign-in: a real-looking token with the user's object ID."""

    def __init__(self) -> None:
        self.scopes: list[str] = []

    def get_token(self, *scopes: str, **_: Any) -> _Token:
        self.scopes.extend(scopes)
        return _Token()


class _Wizard:
    def __init__(self, azure: FakeAzure, tmp_path: Path) -> None:
        self.state_file = tmp_path / "state.json"
        self.secrets = SecretStore(tmp_path / "keys.bin", persistent=False)
        self.credential = _UserCredential()
        azure.callers[USER_TOKEN] = "app"
        self.client = _client(
            azure,
            _settings(**NO_TARGET),
            credential=self.credential,
            state_file=self.state_file,
            secrets=self.secrets,
        )

    def saved(self) -> dict[str, Any]:
        return dict(json.loads(self.state_file.read_text(encoding="utf-8"))["target"])

    def create(self, **overrides: Any) -> Any:
        body = {
            "tenant_id": TENANT,
            "subscription_id": SUBSCRIPTION,
            "resource_group": "rg-new",
            "location": "swedencentral",
            "name": "gw-aks",
            "platform": "aks",
            "sizing": TRIAL,
            **overrides,
        }
        return self.client.post("/api/litellm/target/gateways", json=body, headers=WRITE)

    def status(self) -> dict[str, Any]:
        params = {"subscription_id": SUBSCRIPTION, "resource_group": "rg-new", "name": "gw-aks"}
        response = self.client.get("/api/litellm/target/deploy-status", params=params)
        assert response.status_code == 200, response.text
        return dict(response.json())


@pytest.fixture
def azure() -> FakeAzure:
    return FakeAzure()


@pytest.fixture
def wizard(azure: FakeAzure, tmp_path: Path) -> Iterator[_Wizard]:
    yield _Wizard(azure, tmp_path)
    app.dependency_overrides.clear()


def _context(command: dict[str, Any]) -> dict[str, str]:
    archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(command["context"])))
    return {name: archive.read(name).decode() for name in archive.namelist()}


# -- deploying ------------------------------------------------------------------------------


def test_deploying_on_aks_runs_the_template_and_selects_it(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    response = wizard.create()

    assert response.status_code == 200, response.text
    view = response.json()
    assert (view["platform"], view["url"], view["id"]) == ("aks", URL, CLUSTER)
    deployment = azure.arm_deployments[DEPLOYMENT.casefold()]
    assert deployment["template"] == aks_template()
    assert deployment["tags"] == {
        "managed-by": "ai-gateway-pool",
        "ai-gateway-platform": "aks",
        "ai-gateway-on-failure": "delete",
    }
    parameters = deployment["parameters"]
    assert parameters["deployerPrincipalId"] == "user-oid"
    assert parameters["zones"] == []  # dev: one node, no zones
    assert parameters["postgresPassword"].isalnum()
    # The keys are kept before Azure sees anything; setup follows the deployment.
    saved = wizard.saved()
    assert (saved["gateway_url"], saved["gateway_cluster_id"]) == (URL, CLUSTER)
    assert saved["gateway_provisioning"] == "aks"
    # The app's settings merge the saved target (config.get_settings); so does this client.
    following = _client(
        azure,
        _settings(**NO_TARGET).with_saved_target(saved),
        credential=wizard.credential,
        secrets=wizard.secrets,
    )
    target = following.get("/api/setup").json()["checks"][1]
    assert (target["id"], target["status"], target["detail"]) == (
        "gateway_target",
        "pending",
        "DeployingAks",
    )


def test_a_production_cluster_uses_the_zones_of_its_node_size(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    assert wizard.create(sizing=PRODUCTION).status_code == 200

    assert azure.arm_deployments[DEPLOYMENT.casefold()]["parameters"]["zones"] == ["1", "2", "3"]


def test_a_cluster_gets_nodes_of_a_family_the_subscription_may_use(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    azure.vm_restricted_sizes = {f"Standard_D{n}ds_v5" for n in (4, 8, 16)}

    assert wizard.create().status_code == 200

    parameters = azure.arm_deployments[DEPLOYMENT.casefold()]["parameters"]
    assert parameters["nodeVmSize"] == "Standard_D4ds_v7"


@pytest.mark.parametrize(
    ("setup", "status", "text"),
    [
        (lambda a: a.dns_taken.add("gw-aks"), 409, "is taken"),
        (lambda a: setattr(a, "vm_restricted", True), 409, "isn't available"),
        (lambda a: setattr(a, "vcpus_free", 2), 409, "DDSv5 vCPUs has 2 free"),
        (lambda a: a.allow("app", f"/subscriptions/{SUBSCRIPTION}", "*/read"), 403, "Owner"),
        (
            lambda a: a.postgres_restricted.update({"swedencentral": "Restricted."}),
            409,
            "PostgreSQL",
        ),
    ],
)
def test_what_aks_needs_is_checked_before_anything_is_created(
    wizard: _Wizard, azure: FakeAzure, setup: Any, status: int, text: str
) -> None:
    setup(azure)

    response = wizard.create()

    assert response.status_code == status, response.text
    assert text in response.json()["detail"]
    assert not azure.arm_deployments
    assert f"/subscriptions/{SUBSCRIPTION}/resourcegroups/rg-new" not in azure.resource_groups
    assert not wizard.secrets.get(f"aks-install:{CLUSTER}")


def test_a_cluster_whose_resources_fail_is_cleaned_up_and_its_keys_forgotten(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    assert wizard.create().status_code == 200
    assert wizard.secrets.get(f"aks-install:{CLUSTER}")
    (identity,) = azure.fail_resource("gw-aks")
    wizard.status()

    status = wizard.status()

    assert (status["platform"], status["phase"], status["state"]) == ("aks", "azure", "failed")
    assert status["cleanup"]["state"] == "done"
    assert identity.casefold() not in azure.resources
    assert not wizard.secrets.get(f"aks-install:{CLUSTER}")


def test_the_phases_install_litellm_then_wait_for_https(wizard: _Wizard, azure: FakeAzure) -> None:
    assert wizard.create().status_code == 200
    assert (wizard.status()["state"], wizard.status()["phase"]) == ("running", "azure")
    azure.finish_deployment("gw-aks")
    azure.litellm.unreachable_hosts.add("gw-aks.swedencentral.cloudapp.azure.com")

    # The install starts with Run Command, with a token for the cluster's Kubernetes API.
    started = wizard.status()
    assert (started["state"], started["phase"]) == ("running", "cluster")
    [command] = azure.run_commands
    assert command["command"] == AKS_COMMAND
    assert command["clusterToken"] == USER_TOKEN
    assert AKS_SERVER_SCOPE in wizard.credential.scopes
    files = _context(command)
    assert set(files) >= {"bootstrap.sh", "values.yaml", "gateway.yaml", "secrets/master-key"}
    master_key = files["secrets/master-key"]
    # No secret in the command line or anything but the secret files.
    for name, content in files.items():
        if not name.startswith("secrets/"):
            assert master_key not in content, name
    assert master_key not in command["command"]
    assert (
        "approuting-istio" in files["gateway.yaml"]
        and URL.removeprefix("https://") in files["gateway.yaml"]
    )
    assert "gw-aks-client" in files["values.yaml"]  # workload identity
    assert "InstrumentationKey=gw-aks" in files["secrets/otel.env"]

    assert wizard.status()["phase"] == "cluster"  # still running
    waiting = wizard.status()
    assert (waiting["state"], waiting["phase"]) == ("running", "certificate")
    assert waiting["log_tail"] == "==> done"

    azure.litellm.unreachable_hosts.clear()
    done = wizard.status()
    assert (done["state"], done["phase"]) == ("succeeded", "done")
    # Handed over: an external gateway with its identity, telemetry and cluster.
    saved = wizard.saved()
    assert saved["gateway_provisioning"] == ""
    assert (saved["gateway_principal_id"], saved["gateway_cluster_id"]) == ("gw-aks-mi", CLUSTER)
    assert saved["gateway_app_insights_id"].endswith("/components/appi-gw-aks")
    assert wizard.secrets.get(URL) == master_key
    assert not wizard.secrets.get(f"aks-install:{CLUSTER}")
    assert len(azure.run_commands) == 1


def test_a_failed_install_can_run_again_with_the_same_keys(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    azure.run_command_exit = 1
    azure.run_command_polls = 0
    assert wizard.create().status_code == 200
    azure.finish_deployment("gw-aks")
    wizard.status()

    failed = wizard.status()
    assert (failed["state"], failed["phase"]) == ("failed", "cluster")
    assert "exit code 1" in failed["error"]
    assert "timed out" in failed["log_tail"]

    azure.run_command_exit = 0
    body = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-new",
        "gateway_name": "gw-aks",
    }
    assert (
        wizard.client.post("/api/litellm/target/gateways/retry-install", json=body, headers=WRITE)
    ).status_code == 204
    wizard.status()
    first, again = azure.run_commands
    assert _context(again)["secrets/master-key"] == _context(first)["secrets/master-key"]
    assert wizard.status()["phase"] == "done"


# -- an AKS gateway the app deployed, later ---------------------------------------------------


def test_aks_gateways_are_listed_and_chosen_with_their_key(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    azure.add_aks_gateway("gw-aks", group="rg-aks")
    body = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-aks",
        "gateway_name": "gw-aks",
    }

    listed = wizard.client.get(
        "/api/litellm/target/gateways", params={"subscription_id": SUBSCRIPTION}
    )
    no_key = wizard.client.put("/api/litellm/target", json=body, headers=WRITE)
    wizard.secrets.put(URL, "sk-aks-key")
    chosen = wizard.client.put("/api/litellm/target", json=body, headers=WRITE)

    [view] = [item for item in listed.json() if item["platform"] == "aks"]
    assert (view["name"], view["url"], view["managed"]) == ("gw-aks", URL, True)
    assert no_key.status_code == 409
    assert "kubectl -n litellm get secret litellm-masterkey" in no_key.json()["detail"]
    assert chosen.status_code == 200, chosen.text
    saved = wizard.saved()
    assert (saved["gateway_url"], saved["gateway_principal_id"]) == (URL, "gw-aks-mi")
    assert saved["gateway_cluster_id"].endswith("/managedClusters/gw-aks")


def test_deleting_an_aks_gateway_removes_its_resources_and_keys(
    wizard: _Wizard, azure: FakeAzure
) -> None:
    azure.add_aks_gateway("gw-aks", group="rg-aks")
    azure.add_resource_group("rg-aks")
    azure.slow_deletes = {"microsoft.containerservice/managedclusters"}
    wizard.secrets.put(URL, "sk-aks-key")
    body = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-aks",
        "gateway_name": "gw-aks",
    }

    check = wizard.client.post(
        "/api/litellm/target/gateways/delete-check", json=body, headers=WRITE
    )
    deleted = wizard.client.post(
        "/api/litellm/target/gateways/delete",
        json={**body, "confirm_name": "gw-aks"},
        headers=WRITE,
    )

    assert check.status_code == 200, check.text
    assert check.json()["resources"][0] == "Microsoft.ContainerService/managedClusters/gw-aks"
    assert deleted.status_code == 204, deleted.text
    # The cluster is left to Azure without waiting; the rest is gone, the group stays.
    assert [item["name"] for item in azure.resources.values() if "gw-aks" in item["name"]] == [
        "gw-aks"
    ]
    assert azure.operation_polls == 0
    assert f"/subscriptions/{SUBSCRIPTION}/resourcegroups/rg-aks" in azure.resource_groups
    assert not wizard.secrets.get(URL)


def test_deleting_the_current_aks_gateway_forgets_it(azure: FakeAzure, tmp_path: Path) -> None:
    outputs = azure.add_aks_gateway("gw-aks", group="rg-aks")
    target = {
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-aks",
        "gateway_name": "gw-aks",
        "gateway_url": URL,
        "gateway_cluster_id": outputs["clusterId"],
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"target": target}), encoding="utf-8")
    azure.callers[USER_TOKEN] = "app"
    client = _client(
        azure,
        _settings(**NO_TARGET).with_saved_target(target),
        credential=_UserCredential(),
        state_file=state_file,
    )
    body = {
        "tenant_id": TENANT,
        "subscription_id": SUBSCRIPTION,
        "resource_group": "rg-aks",
        "gateway_name": "gw-aks",
        "confirm_name": "gw-aks",
    }
    try:
        deleted = client.post("/api/litellm/target/gateways/delete", json=body, headers=WRITE)
    finally:
        app.dependency_overrides.clear()

    assert deleted.status_code == 204, deleted.text
    assert not state_file.exists()


# -- what runs in the cluster ----------------------------------------------------------------


def _install(profile: str = "dev") -> AksInstall:
    return AksInstall(
        name="gw-aks",
        size=InstallSize(2, 10) if profile == "production" else InstallSize(),
        fqdn="gw-aks.swedencentral.cloudapp.azure.com",
        identity_client_id="client-id",
        postgres_host="psql.postgres.database.azure.com",
        app_insights_connection_string="InstrumentationKey=x",
        redis_host="redis.swedencentral.redis.azure.net" if profile == "production" else "",
        redis_password="redis-key" if profile == "production" else "",
    )


def test_the_install_reads_secrets_from_files_and_never_prints_them() -> None:
    secrets = aks_secrets()
    files = aks_bootstrap(_install("production"), secrets)

    script = files["bootstrap.sh"]
    for value in (*secrets.values(), "redis-key"):
        for name in ("bootstrap.sh", "values.yaml", "gateway.yaml", "collector.yaml"):
            assert value not in files[name], name
    assert "REDIS_PASSWORD=redis-key" in files["secrets/litellm.env"]
    assert f"LITELLM_SALT_KEY={secrets['salt_key']}" in files["secrets/litellm.env"]
    # Idempotent: secrets only when missing, charts upgraded in place.
    assert "exists, kept" in script and "helm upgrade --install litellm" in script
    # LiteLLM's chart passes the master key as PROXY_MASTER_KEY.
    assert "os.environ/PROXY_MASTER_KEY" in files["values.yaml"]
    assert "LITELLM_OTEL_V2" in files["values.yaml"]
    bash = shutil.which("bash")
    if bash:
        result = subprocess.run([bash, "-n"], input=script, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr


def test_production_pods_get_litellms_recommended_size() -> None:
    values = aks_bootstrap(_install("production"), aks_secrets())["values.yaml"]

    # 1 vCPU and 4Gi for LiteLLM's one worker, as both requests and limits.
    assert values.count('cpu: "1"') == 2 and values.count('memory: "4Gi"') == 2
    assert "maxReplicas: 10" in values
    # Spend is written once a minute; ten pods at ten connections fit the server.
    assert "proxy_batch_write_at: 60" in values
    assert "database_connection_pool_limit: 10" in values
    # LiteLLM's production environment, instead of the chart's INFO logs.
    assert "LITELLM_MODE: PRODUCTION" in values and "LITELLM_LOG: ERROR" in values
    assert "targetCPUUtilizationPercentage: 60" in values


def test_the_bundle_for_an_existing_cluster_uses_the_charts_master_key() -> None:
    bundle = aks_bundle(
        name="gw",
        namespace="litellm",
        cluster="aks",
        resource_group="rg",
        subscription_id=SUBSCRIPTION,
        location="eastus2",
    )

    values = next(file.content for file in bundle.files if file.path == "values.yaml")
    assert "os.environ/PROXY_MASTER_KEY" in values
    # One replica: more need Redis and a General Purpose database, which the script doesn't make.
    assert "replicaCount: 1" in values
    assert "LITELLM_MODE: PRODUCTION" in values
    readme = next(file.content for file in bundle.files if file.path == "README.md")
    assert "Redis" in readme and "General Purpose" in readme
