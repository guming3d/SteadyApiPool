"""What a gateway runs: the pinned LiteLLM image, its config.yaml, the OpenTelemetry Collector
config, the Container Apps template and the guided AKS bundle.

The ACA template is infra/gateway/litellm-aca.bicep, compiled into templates/litellm-aca.json
(CI checks the two match). Both deployments get the same config.yaml, so a gateway behaves the
same wherever it runs:

- models live in the database (``store_model_in_db``), written by this app;
- Foundry members are called with the gateway's managed identity
  (``enable_azure_ad_token_refresh``, ``AZURE_CLIENT_ID``);
- Responses API follow-ups stay on the deployment that answered first
  (``encrypted_content_affinity``, ``responses_api_deployment_check``);
- with Redis (more than one replica), replicas share cooldowns.
"""

from __future__ import annotations

import json
import secrets
import string
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

Profile = Literal["dev", "production"]
# Span names LiteLLM gives model calls ("chat <model group>", ...). Everything else it traces
# (database queries, auth) is dropped before it reaches Application Insights.
_MODEL_SPANS = "^(chat|responses|embeddings|text_completion|completion) "


@lru_cache
def aca_template() -> dict[str, Any]:
    template: dict[str, Any] = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
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


def collector_config() -> dict[str, Any]:
    return {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "127.0.0.1:4318"}}}},
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
            **litellm_config(redis=False),
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
traffic, put an ingress with TLS (e.g. the AKS application routing add-on with a certificate)
in front and connect to its HTTPS URL.
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
