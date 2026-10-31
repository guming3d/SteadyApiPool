"""One-time setup for the live Azure journeys (e2e/azure, docs/testing.md). Run it yourself,
signed in with ``az login`` as someone who may create resources and Entra app registrations;
it never runs in a test. ``--dry-run`` prints the commands instead.

foundry
    The two long-lived Foundry accounts every run pools: one per region, each with a
    pay-per-token (GlobalStandard) deployment of the same small model, in resource group
    ``e2e-foundry`` (not tagged for the janitor, so no run deletes it). Idle they cost nothing.
    Prints the E2E_FOUNDRY_* values.

identity
    The identity GitHub Actions signs in as, with no secret: an Entra app registration, its
    service principal, and a federated credential for this repository's ``azure-e2e`` GitHub
    environment (protect that environment so only the branches you choose run it). In the
    subscription it gets Contributor, and Role Based Access Control Administrator limited by a
    condition to the two roles the app assigns (Cognitive Services OpenAI User for a gateway's
    identity on Foundry accounts, Azure Kubernetes Service RBAC Cluster Admin on a new cluster),
    so it can't give anyone anything else. Prints the AZURE_* values for the environment.

Both only add what is missing. Python's standard library and the Azure CLI are enough.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from typing import Any

GROUP = "e2e-foundry"
OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
AKS_RBAC_CLUSTER_ADMIN = "b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b"
RBAC_ADMIN = "f58310d9-a9f6-439a-9e8d-f62e7b41a168"
CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
ENVIRONMENT = "azure-e2e"
DISPLAY_NAME = "SteadyApiPool e2e (GitHub Actions)"

DRY_RUN = False
_WRITES = {"create", "delete", "purge", "update"}


def az(*args: str, check: bool = True) -> Any:
    executable = shutil.which("az") or shutil.which("az.cmd")
    if executable is None:
        raise SystemExit("The Azure CLI (az) is required: https://aka.ms/azure-cli")
    # A dry run still reads (to know what exists), and prints what would change.
    if DRY_RUN and _WRITES & set(args[:5]):
        print("az", " ".join(args))
        return {}
    result = subprocess.run(
        [executable, *args, "--only-show-errors", "-o", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if not check:
            return None
        raise SystemExit(f"az {' '.join(args[:4])} failed:\n{result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def foundry(args: argparse.Namespace) -> None:
    subscription = az("account", "show")["id"]
    az("group", "create", "--name", GROUP, "--location", args.regions[0])
    accounts = []
    for region in args.regions:
        name = f"{args.prefix}-{region}"[:60]
        if az("cognitiveservices", "account", "show", "-g", GROUP, "-n", name, check=False):
            print(f"{name} exists")
        else:
            az(
                "cognitiveservices", "account", "create", "-g", GROUP, "-n", name,
                "--location", region, "--kind", "AIServices", "--sku", "S0",
                "--custom-domain", name, "--yes",
            )  # fmt: skip
        if not az(
            "cognitiveservices", "account", "deployment", "show",
            "-g", GROUP, "-n", name, "--deployment-name", args.deployment, check=False,
        ):  # fmt: skip
            az(
                "cognitiveservices", "account", "deployment", "create",
                "-g", GROUP, "-n", name, "--deployment-name", args.deployment,
                "--model-name", args.model, "--model-version", args.version,
                "--model-format", "OpenAI", "--sku-name", "GlobalStandard",
                "--sku-capacity", str(args.capacity),
            )  # fmt: skip
        accounts.append(
            f"/subscriptions/{subscription}/resourceGroups/{GROUP}"
            f"/providers/Microsoft.CognitiveServices/accounts/{name}"
        )
    print("\nSet these (GitHub environment variables, or your shell):")
    print(f"E2E_SUBSCRIPTION_ID={subscription}")
    print(f"E2E_FOUNDRY_ACCOUNTS={','.join(accounts)}")
    print(f"E2E_FOUNDRY_DEPLOYMENT={args.deployment}")


def _condition() -> str:
    """Role assignments it may write or delete: only the two roles the app assigns."""
    roles = f"{OPENAI_USER}, {AKS_RBAC_CLUSTER_ADMIN}"
    parts = []
    for action in ("write", "delete"):
        source = "Resource" if action == "delete" else "Request"
        attribute = f"@{source}[Microsoft.Authorization/roleAssignments:RoleDefinitionId]"
        parts.append(
            f"((!(ActionMatches{{'Microsoft.Authorization/roleAssignments/{action}'}})) OR "
            f"({attribute} ForAnyOfAnyValues:GuidEquals {{{roles}}}))"
        )
    return " AND ".join(parts)


def identity(args: argparse.Namespace) -> None:
    account = az("account", "show")
    subscription, tenant = account["id"], account["tenantId"]
    apps = az("ad", "app", "list", "--display-name", DISPLAY_NAME) or []
    app = apps[0] if apps else az("ad", "app", "create", "--display-name", DISPLAY_NAME)
    client_id = app.get("appId", "<new app's client ID>")
    if DRY_RUN or not az("ad", "sp", "show", "--id", client_id, check=False):
        az("ad", "sp", "create", "--id", client_id)
    sp = az("ad", "sp", "show", "--id", client_id, check=False) or {}
    principal = sp.get("id", "<its service principal's object ID>")
    subject = f"repo:{args.repo}:environment:{ENVIRONMENT}"
    existing = az("ad", "app", "federated-credential", "list", "--id", client_id, check=False)
    if not any(c.get("subject") == subject for c in existing or []):
        credential = {
            "name": "github-azure-e2e",
            "issuer": "https://token.actions.githubusercontent.com",
            "subject": subject,
            "audiences": ["api://AzureADTokenExchange"],
        }
        az(
            "ad", "app", "federated-credential", "create", "--id", client_id,
            "--parameters", json.dumps(credential),
        )  # fmt: skip
    scope = f"/subscriptions/{subscription}"
    az(
        "role", "assignment", "create", "--assignee-object-id", principal,
        "--assignee-principal-type", "ServicePrincipal", "--role", CONTRIBUTOR, "--scope", scope,
    )  # fmt: skip
    az(
        "role", "assignment", "create", "--assignee-object-id", principal,
        "--assignee-principal-type", "ServicePrincipal", "--role", RBAC_ADMIN, "--scope", scope,
        "--condition", _condition(), "--condition-version", "2.0",
    )  # fmt: skip
    print(f"\nIn GitHub: Settings > Environments > {ENVIRONMENT} > variables:")
    print(f"AZURE_CLIENT_ID={client_id}")
    print(f"AZURE_TENANT_ID={tenant}")
    print(f"AZURE_SUBSCRIPTION_ID={subscription}")


def main(argv: list[str] | None = None) -> int:
    global DRY_RUN
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the commands only")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("foundry", help="the long-lived Foundry accounts and deployments")
    one.add_argument("--regions", nargs=2, default=["eastus2", "swedencentral"])
    one.add_argument("--prefix", default="e2e-steadyapipool")
    one.add_argument("--model", default="gpt-4.1-mini")
    one.add_argument("--version", default="2025-04-14")
    one.add_argument("--deployment", default="e2e-chat")
    one.add_argument("--capacity", type=int, default=10, help="thousands of tokens a minute")
    two = sub.add_parser("identity", help="the GitHub Actions identity (OIDC, no secret)")
    two.add_argument("--repo", default="guming3d/SteadyApiPool", help="owner/name")
    args = parser.parse_args(argv)
    DRY_RUN = args.dry_run
    {"foundry": foundry, "identity": identity}[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
