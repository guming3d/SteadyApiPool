"""Deletes what live Azure test runs (e2e/azure) left behind: resource groups tagged
``steadyapipool-e2e`` older than ``--older-than`` hours, and soft-deleted APIM services those
runs created (named ``e2eapim…``). A run deletes its own group in teardown; this catches a run
that was cancelled or whose runner died. It touches nothing else, the long-lived Foundry group
included (it has no such tag). ``--dry-run`` lists instead.

    python scripts/e2e-azure-janitor.py --subscription <id> [--older-than 6] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

TAG = "steadyapipool-e2e"
APIM_PREFIX = "e2eapim"


def az(*args: str) -> Any:
    executable = shutil.which("az") or shutil.which("az.cmd")
    if executable is None:
        raise SystemExit("The Azure CLI (az) is required")
    result = subprocess.run(
        [executable, *args, "--only-show-errors", "-o", "json"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(f"az {' '.join(args[:3])} failed:\n{result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def stale_groups(groups: list[dict[str, Any]], now: datetime, older_than: timedelta) -> list[str]:
    """Names of the e2e groups created before ``now - older_than`` (or with no date)."""
    names = []
    for group in groups:
        tags = group.get("tags") or {}
        if TAG not in tags:
            continue
        try:
            created = datetime.strptime(str(tags.get("created")), "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            names.append(str(group["name"]))
            continue
        if created.replace(tzinfo=UTC) < now - older_than:
            names.append(str(group["name"]))
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--older-than", type=float, default=6.0, help="hours (default 6)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    az("account", "set", "--subscription", args.subscription)
    groups = az("group", "list", "--tag", TAG) or []
    for name in stale_groups(groups, datetime.now(UTC), timedelta(hours=args.older_than)):
        print(f"{'would delete' if args.dry_run else 'deleting'} resource group {name}")
        if not args.dry_run:
            az("group", "delete", "--name", name, "--yes", "--no-wait")
    for service in az("apim", "deletedservice", "list") or []:
        name = str(service.get("name") or "")
        if name.startswith(APIM_PREFIX):
            location = str(service.get("location") or "").replace(" ", "").lower()
            print(f"{'would purge' if args.dry_run else 'purging'} deleted APIM service {name}")
            if not args.dry_run:
                az(
                    "apim",
                    "deletedservice",
                    "purge",
                    "--service-name",
                    name,
                    "--location",
                    location,
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
