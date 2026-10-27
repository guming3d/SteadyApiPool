"""What a gateway runs: the pinned LiteLLM image, its config.yaml, the OpenTelemetry Collector
config, the Container Apps and AKS templates, the install the app runs in a new AKS cluster, and
the guided bundle for a cluster the user already has.

The templates are infra/gateway/litellm-{aca,aks}.bicep, compiled into templates/ (CI checks
they match). Every gateway gets the same config.yaml, so it behaves the same wherever it runs:

- models live in the database (``store_model_in_db``), written by this app;
- Foundry members are called with the gateway's managed identity
  (``enable_azure_ad_token_refresh``, ``AZURE_CLIENT_ID``);
- Responses API follow-ups stay on the deployment that answered first
  (``encrypted_content_affinity``, ``responses_api_deployment_check``);
- with Redis (more than one replica), replicas share cooldowns.
"""

from __future__ import annotations

import base64
import io
import json
import secrets
import string
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from .models import AksBundle, AksBundleFile
from .resources import to_yaml

# Keep in step with tests/litellm_stack/docker-compose.yml and the Bicep default.
LITELLM_VERSION = "1.106.4"
LITELLM_IMAGE = f"ghcr.io/berriai/litellm:v{LITELLM_VERSION}"
LITELLM_HELM_CHART = "oci://ghcr.io/berriai/litellm-helm"
COLLECTOR_IMAGE = "otel/opentelemetry-collector-contrib:0.120.0"
TEMPLATE_FILE = Path(__file__).parent / "templates" / "litellm-aca.json"
AKS_TEMPLATE_FILE = Path(__file__).parent / "templates" / "litellm-aks.json"
# What the app installs in a new AKS cluster, besides LiteLLM's chart.
CERT_MANAGER_CHART = "oci://quay.io/jetstack/charts/cert-manager"
CERT_MANAGER_VERSION = "v1.21.3"
LETSENCRYPT = "https://acme-v02.api.letsencrypt.org/directory"
# The namespace and service account LiteLLM runs as (federated in litellm-aks.bicep).
AKS_NAMESPACE = "litellm"
AKS_NODE_SIZE = "Standard_D4ds_v5"
# Run Command unpacks the files into its working directory and runs this.
AKS_COMMAND = "bash bootstrap.sh"

Profile = Literal["dev", "production"]
# Span names LiteLLM gives model calls ("chat <model group>", ...). Everything else it traces
# (database queries, auth) is dropped before it reaches Application Insights.
_MODEL_SPANS = "^(chat|responses|embeddings|text_completion|completion) "


@lru_cache
def aca_template() -> dict[str, Any]:
    template: dict[str, Any] = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
    return template


@lru_cache
def aks_template() -> dict[str, Any]:
    template: dict[str, Any] = json.loads(AKS_TEMPLATE_FILE.read_text(encoding="utf-8"))
    return template


def litellm_config(*, redis: bool) -> dict[str, Any]:
    router: dict[str, Any] = {
        "routing_strategy": "simple-shuffle",
        "optional_pre_call_checks": [
            "encrypted_content_affinity",
            "responses_api_deployment_check",
        ],
    }
    if redis:
        router.update(
            redis_host="os.environ/REDIS_HOST",
            redis_port="os.environ/REDIS_PORT",
            redis_password="os.environ/REDIS_PASSWORD",
        )
    return {
        "model_list": [],
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "database_url": "os.environ/DATABASE_URL",
            "store_model_in_db": True,
        },
        "litellm_settings": {
            "enable_azure_ad_token_refresh": True,
            "callbacks": ["otel"],
            "num_retries": 1,
            "request_timeout": 120,
        },
        "router_settings": router,
    }


def collector_config(endpoint: str = "127.0.0.1:4318") -> dict[str, Any]:
    """The collector next to LiteLLM: a sidecar on loopback on Container Apps, a Service in the
    cluster on AKS."""
    return {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": endpoint}}}},
        "processors": {
            "filter/model-calls": {
                "error_mode": "ignore",
                "traces": {"span": [f'not IsMatch(name, "{_MODEL_SPANS}")']},
            },
            "batch": {},
        },
        "exporters": {
            "azuremonitor": {"connection_string": "${env:APPLICATIONINSIGHTS_CONNECTION_STRING}"}
        },
        "service": {
            "pipelines": {
                "traces": {
                    "receivers": ["otlp"],
                    "processors": ["filter/model-calls", "batch"],
                    "exporters": ["azuremonitor"],
                }
            }
        },
    }


def _token(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        # Postgres wants upper and lower case letters and digits.
        if (
            any(c.isupper() for c in value)
            and any(c.islower() for c in value)
            and any(c.isdigit() for c in value)
        ):
            return value


def aca_parameters(name: str, location: str, profile: Profile) -> dict[str, Any]:
    """Deployment parameters, with new secrets: only ever used to create a gateway."""
    values = {
        "name": name,
        "location": location,
        "profile": profile,
        "image": LITELLM_IMAGE,
        "collectorImage": COLLECTOR_IMAGE,
        "masterKey": f"sk-{_token(40)}",
        "saltKey": f"sk-{_token(40)}",
        "postgresPassword": _token(32),
        "litellmConfig": to_yaml(litellm_config(redis=profile == "production")),
        "collectorConfig": to_yaml(collector_config()),
        "tags": {},
    }
    return {key: {"value": value} for key, value in values.items()}


# -- AKS: the cluster the app deploys, and the install in it ------------------------------------


def aks_proxy_config(*, redis: bool) -> dict[str, Any]:
    """config.yaml on AKS. LiteLLM's chart passes the master key as PROXY_MASTER_KEY."""
    config = litellm_config(redis=redis)
    config["general_settings"]["master_key"] = "os.environ/PROXY_MASTER_KEY"
    return config


def aks_secrets() -> dict[str, str]:
    """A new AKS gateway's secrets, made once by the app: the salt key must never change."""
    return {
        "master_key": f"sk-{_token(40)}",
        "salt_key": f"sk-{_token(40)}",
        "postgres_password": _token(32),
    }


def aks_parameters(
    *,
    name: str,
    location: str,
    profile: Profile,
    deployer: str,
    zones: list[str],
    postgres_password: str,
) -> dict[str, Any]:
    values = {
        "name": name,
        "location": location,
        "profile": profile,
        "nodeVmSize": AKS_NODE_SIZE,
        "zones": zones if profile == "production" else [],
        "deployerPrincipalId": deployer,
        "postgresPassword": postgres_password,
        "tags": {},
    }
    return {key: {"value": value} for key, value in values.items()}


def aks_fqdn(name: str, location: str) -> str:
    """The HTTPS address: the DNS label of the gateway's public IP (litellm-aks.bicep)."""
    return f"{name}.{location}.cloudapp.azure.com"


@dataclass(frozen=True)
class AksInstall:
    """What the install in the cluster needs from the deployed Azure resources."""

    name: str
    profile: Profile
    fqdn: str
    identity_client_id: str
    postgres_host: str
    app_insights_connection_string: str
    redis_host: str = ""
    redis_password: str = ""
    # Let's Encrypt's expiry notices; optional.
    email: str = ""


def _values(install: AksInstall) -> dict[str, Any]:
    production = install.profile == "production"
    collector = f"http://otel-collector.{AKS_NAMESPACE}.svc:4318"
    values: dict[str, Any] = {
        "replicaCount": 2 if production else 1,
        "image": {"repository": LITELLM_IMAGE.split(":")[0], "tag": f"v{LITELLM_VERSION}"},
        "masterkeySecretName": "litellm-masterkey",
        "masterkeySecretKey": "masterkey",
        "db": {
            "useExisting": True,
            "deployStandalone": False,
            "endpoint": install.postgres_host,
            "database": "litellm",
            "url": (
                "postgresql://$(DATABASE_USERNAME):$(DATABASE_PASSWORD)"
                "@$(DATABASE_HOST)/$(DATABASE_NAME)?sslmode=require"
            ),
            "secret": {"name": "litellm-db", "usernameKey": "username", "passwordKey": "password"},
        },
        "environmentSecrets": ["litellm-env"],
        "envVars": {
            "STORE_MODEL_IN_DB": "True",
            "LITELLM_OTEL_V2": "true",
            "OTEL_EXPORTER": "otlp_http",
            "OTEL_ENDPOINT": collector,
            "OTEL_EXPORTER_OTLP_ENDPOINT": collector,
            "OTEL_SERVICE_NAME": install.name,
            **({"REDIS_SSL": "True"} if production else {}),
        },
        "serviceAccount": {
            "create": True,
            "name": AKS_NAMESPACE,
            "annotations": {"azure.workload.identity/client-id": install.identity_client_id},
        },
        "podLabels": {"azure.workload.identity/use": "true"},
        "service": {"type": "ClusterIP", "port": 4000},
        "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}},
        "proxy_config": aks_proxy_config(redis=production),
    }
    if production:
        values["autoscaling"] = {
            "enabled": True,
            "minReplicas": 2,
            "maxReplicas": 6,
            "targetCPUUtilizationPercentage": 60,
        }
        values["pdb"] = {"enabled": True, "minAvailable": 1}
    return values


def _collector_manifests() -> list[dict[str, Any]]:
    labels = {"app": "otel-collector"}
    container = {
        "name": "otel-collector",
        "image": COLLECTOR_IMAGE,
        "args": ["--config", "/conf/otel.yaml"],
        "ports": [{"containerPort": 4318}],
        "envFrom": [{"secretRef": {"name": "otel-env"}}],
        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}},
        "volumeMounts": [{"name": "conf", "mountPath": "/conf"}],
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "otel-collector", "namespace": AKS_NAMESPACE},
            "data": {"otel.yaml": to_yaml(collector_config("0.0.0.0:4318"))},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "otel-collector", "namespace": AKS_NAMESPACE, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [container],
                        "volumes": [{"name": "conf", "configMap": {"name": "otel-collector"}}],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "otel-collector", "namespace": AKS_NAMESPACE},
            "spec": {"selector": labels, "ports": [{"port": 4318, "targetPort": 4318}]},
        },
    ]


def _gateway_manifests(install: AksInstall) -> list[dict[str, Any]]:
    """HTTPS in front of LiteLLM: application routing's Istio Gateway on a public IP whose DNS
    label is the gateway's name, with a Let's Encrypt certificate from cert-manager (HTTP-01
    through the same Gateway). Plain HTTP only redirects, except for the ACME challenge."""
    namespace = {"namespace": AKS_NAMESPACE}
    acme: dict[str, Any] = {
        "server": LETSENCRYPT,
        "privateKeySecretRef": {"name": "letsencrypt-account"},
        "solvers": [
            {
                "http01": {
                    "gatewayHTTPRoute": {
                        "parentRefs": [
                            {"name": "litellm", "kind": "Gateway", "sectionName": "http"}
                        ]
                    }
                }
            }
        ],
    }
    if install.email:
        acme["email"] = install.email
    listeners: list[dict[str, Any]] = [
        {
            "name": "http",
            "port": 80,
            "protocol": "HTTP",
            "hostname": install.fqdn,
            "allowedRoutes": {"namespaces": {"from": "Same"}},
        },
        {
            "name": "https",
            "port": 443,
            "protocol": "HTTPS",
            "hostname": install.fqdn,
            "tls": {"mode": "Terminate", "certificateRefs": [{"name": "litellm-tls"}]},
            "allowedRoutes": {"namespaces": {"from": "Same"}},
        },
    ]
    redirect = {
        "type": "RequestRedirect",
        "requestRedirect": {"scheme": "https", "statusCode": 301},
    }
    return [
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "Gateway",
            "metadata": {"name": "litellm", **namespace},
            "spec": {
                "gatewayClassName": "approuting-istio",
                "infrastructure": {
                    "annotations": {"service.beta.kubernetes.io/azure-dns-label-name": install.name}
                },
                "listeners": listeners,
            },
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Issuer",
            "metadata": {"name": "letsencrypt", **namespace},
            "spec": {"acme": acme},
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": "litellm-tls", **namespace},
            "spec": {
                "secretName": "litellm-tls",
                "dnsNames": [install.fqdn],
                "issuerRef": {"name": "letsencrypt", "kind": "Issuer"},
            },
        },
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": "litellm", **namespace},
            "spec": {
                "parentRefs": [{"name": "litellm", "sectionName": "https"}],
                "hostnames": [install.fqdn],
                # Model calls can take minutes; the gateway mustn't cut them short.
                "rules": [
                    {
                        "backendRefs": [{"name": "litellm", "port": 4000}],
                        "timeouts": {"request": "600s"},
                    }
                ],
            },
        },
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": "litellm-redirect", **namespace},
            "spec": {
                "parentRefs": [{"name": "litellm", "sectionName": "http"}],
                "hostnames": [install.fqdn],
                "rules": [{"filters": [redirect]}],
            },
        },
    ]


def _documents(manifests: list[dict[str, Any]]) -> str:
    return "---\n".join(to_yaml(manifest) for manifest in manifests)


def _env_file(values: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items() if value)


def aks_bootstrap(install: AksInstall, secrets: dict[str, str]) -> dict[str, str]:
    """The files Run Command unpacks in the cluster: bootstrap.sh and what it applies.

    The script can run again at any time (after a failure, or a restart of the app): charts are
    upgraded in place, manifests applied, and secrets created only when missing, so the salt and
    master keys never change once the gateway has them. It never prints a secret.
    """
    litellm_env = {
        "LITELLM_SALT_KEY": secrets["salt_key"],
        "AZURE_CLIENT_ID": install.identity_client_id,
        "REDIS_HOST": install.redis_host,
        "REDIS_PORT": "10000" if install.redis_host else "",
        "REDIS_PASSWORD": install.redis_password,
    }
    script = f"""#!/usr/bin/env bash
# Installs the LiteLLM gateway "{install.name}" in this cluster. Run by the AI Gateway Pool
# Manager through AKS Run Command; safe to run again.
set -euo pipefail
cd "$(dirname "$0")"
NS={AKS_NAMESPACE}

echo "==> namespace and secrets"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
secret() {{
  local name=$1; shift
  if kubectl -n "$NS" get secret "$name" >/dev/null 2>&1; then
    echo "    $name exists, kept"
  else
    kubectl -n "$NS" create secret generic "$name" "$@" >/dev/null
    echo "    $name created"
  fi
}}
secret litellm-masterkey --from-file=masterkey=secrets/master-key
secret litellm-db --from-literal=username=litellm --from-file=password=secrets/postgres-password
secret litellm-env --from-env-file=secrets/litellm.env
secret otel-env --from-env-file=secrets/otel.env

echo "==> cert-manager {CERT_MANAGER_VERSION}"
helm upgrade --install cert-manager {CERT_MANAGER_CHART} --version {CERT_MANAGER_VERSION} \\
  --namespace cert-manager --create-namespace \\
  --set crds.enabled=true --set config.gatewayAPI.enabled=true \\
  --wait --timeout 10m

echo "==> OpenTelemetry Collector"
kubectl apply -f collector.yaml

echo "==> LiteLLM {LITELLM_VERSION}"
helm upgrade --install litellm {LITELLM_HELM_CHART} --version {LITELLM_VERSION} \\
  --namespace "$NS" -f values.yaml --wait --timeout 15m

echo "==> HTTPS on {install.fqdn}"
kubectl apply -f gateway.yaml
kubectl -n "$NS" wait --for=condition=Programmed gateway/litellm --timeout=10m
kubectl -n "$NS" get gateway litellm
echo "==> done; the certificate follows in a few minutes"
"""
    return {
        "bootstrap.sh": script,
        "values.yaml": to_yaml(_values(install)),
        "collector.yaml": _documents(_collector_manifests()),
        "gateway.yaml": _documents(_gateway_manifests(install)),
        "secrets/master-key": secrets["master_key"],
        "secrets/postgres-password": secrets["postgres_password"],
        "secrets/litellm.env": _env_file(litellm_env),
        "secrets/otel.env": _env_file(
            {"APPLICATIONINSIGHTS_CONNECTION_STRING": install.app_insights_connection_string}
        ),
    }


def context_zip(files: dict[str, str]) -> str:
    """Run Command's ``context``: the files, zipped and base64-encoded."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            info = zipfile.ZipInfo(path)
            # Scripts keep their execute bit; secrets are readable by their owner only.
            info.external_attr = (0o755 if path.endswith(".sh") else 0o600) << 16
            archive.writestr(info, content.replace("\r\n", "\n"))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


# -- the guided AKS deployment ------------------------------------------------------------------


def aks_bundle(
    *,
    name: str,
    namespace: str,
    cluster: str,
    resource_group: str,
    subscription_id: str,
    location: str,
) -> AksBundle:
    """Files and commands that deploy the same gateway on an existing AKS cluster with Helm.

    The cluster needs the OIDC issuer and workload identity enabled. Nothing here holds a
    secret: the script generates them on the machine that runs it.
    """
    identity = f"id-{name}"
    postgres = f"psql-{name}"
    values = {
        "replicaCount": 2,
        "image": {"repository": LITELLM_IMAGE.split(":")[0], "tag": f"v{LITELLM_VERSION}"},
        "masterkeySecretName": "litellm-masterkey",
        "masterkeySecretKey": "masterkey",
        "db": {
            "useExisting": True,
            "deployStandalone": False,
            "endpoint": f"{postgres}.postgres.database.azure.com",
            "database": "litellm",
            "url": "postgresql://$(DATABASE_USERNAME):$(DATABASE_PASSWORD)@$(DATABASE_HOST)/$(DATABASE_NAME)?sslmode=require",
            "secret": {"name": "litellm-db", "usernameKey": "username", "passwordKey": "password"},
        },
        "environmentSecrets": ["litellm-env"],
        "envVars": {"STORE_MODEL_IN_DB": "True"},
        "serviceAccount": {
            "create": True,
            "annotations": {"azure.workload.identity/client-id": "${IDENTITY_CLIENT_ID}"},
        },
        "podLabels": {"azure.workload.identity/use": "true"},
        "service": {"type": "LoadBalancer", "port": 4000},
        "proxy_config": {
            **aks_proxy_config(redis=False),
            # No collector on AKS by default; add OTEL_* settings to send spans somewhere.
            "litellm_settings": {
                **litellm_config(redis=False)["litellm_settings"],
                "callbacks": [],
            },
        },
    }
    script = f"""#!/usr/bin/env bash
# Deploys a LiteLLM gateway "{name}" on AKS cluster "{cluster}" (resource group
# "{resource_group}"). Needs az (signed in), kubectl and helm. Run it once; it prints the URL and
# master key to enter in the app's Connect form.
set -euo pipefail

SUBSCRIPTION="{subscription_id}"
RESOURCE_GROUP="{resource_group}"
LOCATION="{location}"
CLUSTER="{cluster}"
NAMESPACE="{namespace}"
IDENTITY="{identity}"
POSTGRES="{postgres}"

az account set --subscription "$SUBSCRIPTION"
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$CLUSTER" --overwrite-existing

# 1. The identity LiteLLM calls Foundry as, federated with the pod's service account.
az identity create --resource-group "$RESOURCE_GROUP" --name "$IDENTITY" --location "$LOCATION"
export IDENTITY_CLIENT_ID=$(az identity show -g "$RESOURCE_GROUP" -n "$IDENTITY" \\
  --query clientId -o tsv)
IDENTITY_PRINCIPAL_ID=$(az identity show -g "$RESOURCE_GROUP" -n "$IDENTITY" \\
  --query principalId -o tsv)
ISSUER=$(az aks show -g "$RESOURCE_GROUP" -n "$CLUSTER" \\
  --query oidcIssuerProfile.issuerUrl -o tsv)
az identity federated-credential create --resource-group "$RESOURCE_GROUP" \\
  --identity-name "$IDENTITY" --name litellm --issuer "$ISSUER" \\
  --subject "system:serviceaccount:$NAMESPACE:litellm" --audiences api://AzureADTokenExchange

# 2. Postgres for LiteLLM's model table and keys.
DB_PASSWORD=$(openssl rand -hex 24)
az postgres flexible-server create --resource-group "$RESOURCE_GROUP" --name "$POSTGRES" \\
  --location "$LOCATION" --tier Burstable --sku-name Standard_B1ms --version 16 \\
  --storage-size 32 --admin-user litellm --admin-password "$DB_PASSWORD" \\
  --public-access 0.0.0.0 --yes
az postgres flexible-server db create --resource-group "$RESOURCE_GROUP" \\
  --server-name "$POSTGRES" --database-name litellm

# 3. Secrets. The salt key encrypts stored member keys: keep it, never change it.
MASTER_KEY="sk-$(openssl rand -hex 24)"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NAMESPACE" create secret generic litellm-masterkey \\
  --from-literal=masterkey="$MASTER_KEY"
kubectl -n "$NAMESPACE" create secret generic litellm-db \\
  --from-literal=username=litellm --from-literal=password="$DB_PASSWORD"
kubectl -n "$NAMESPACE" create secret generic litellm-env \\
  --from-literal=LITELLM_SALT_KEY="sk-$(openssl rand -hex 24)" \\
  --from-literal=AZURE_CLIENT_ID="$IDENTITY_CLIENT_ID"

# 4. LiteLLM.
envsubst < values.yaml > values.rendered.yaml
helm upgrade --install litellm {LITELLM_HELM_CHART} --version {LITELLM_VERSION} \\
  --namespace "$NAMESPACE" -f values.rendered.yaml --wait --timeout 15m

echo "Waiting for the gateway's public address..."
for _ in $(seq 1 60); do
  IP=$(kubectl -n "$NAMESPACE" get svc litellm \\
    -o jsonpath='{{.status.loadBalancer.ingress[0].ip}}' || true)
  [ -n "$IP" ] && break
  sleep 5
done

echo
echo "The gateway answers on http://$IP:4000 (plain HTTP). The app only connects over HTTPS,"
echo "or to localhost: put an ingress with TLS in front (see README.md), or to try it now run"
echo "  kubectl -n $NAMESPACE port-forward svc/litellm 4000:4000"
echo "and connect the app to http://localhost:4000. Then use:"
echo "  Master key:   $MASTER_KEY"
echo "  Identity:     $IDENTITY_PRINCIPAL_ID"
"""
    readme = f"""# LiteLLM gateway "{name}" on AKS

1. The cluster needs workload identity:
   `az aks update -g {resource_group} -n {cluster} --enable-oidc-issuer --enable-workload-identity`
2. Put `values.yaml` and `deploy.sh` in one folder and run `bash deploy.sh`
   (Azure Cloud Shell has az, kubectl, helm and envsubst).
3. In the app, choose **Connect a gateway** and enter the gateway's URL and the master key and
   identity the script prints. Then grant the identity access to your Foundry accounts in
   setup, as for any gateway.

The service is a LoadBalancer on plain HTTP, and the app sends the master key only over HTTPS
or to localhost. To try it from the machine running the app, keep
`kubectl -n {namespace} port-forward svc/litellm 4000:4000` running and connect to
`http://localhost:4000` (clients then reach the gateway through that tunnel only). For real
traffic, put HTTPS in front (e.g. application routing's Gateway API with a certificate) and
connect to its HTTPS URL. Or let the app deploy a new AKS cluster, which sets this up for you.
For more than one replica, add Redis so replicas share cooldowns (router_settings.redis_host).
"""
    return AksBundle(
        name=name,
        namespace=namespace,
        files=[
            AksBundleFile(path="values.yaml", content=to_yaml(values)),
            AksBundleFile(path="deploy.sh", content=script),
            AksBundleFile(path="README.md", content=readme),
        ],
    )
