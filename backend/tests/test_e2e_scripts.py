"""The scripts around the live Azure tests: what the janitor deletes, and what the CI identity's
role assignment rights are limited to."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


janitor = _load("e2e-azure-janitor")
setup = _load("e2e-azure-setup")


def test_the_janitor_takes_only_old_e2e_groups() -> None:
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    groups = [
        {
            "name": "e2e-aca-old",
            "tags": {"steadyapipool-e2e": "r1", "created": "2026-11-01T02:00:00Z"},
        },
        {
            "name": "e2e-aca-new",
            "tags": {"steadyapipool-e2e": "r2", "created": "2026-11-01T11:00:00Z"},
        },
        {"name": "e2e-undated", "tags": {"steadyapipool-e2e": "r3"}},
        # The long-lived Foundry accounts, and anything else, have no such tag.
        {"name": "e2e-foundry", "tags": {}},
        {"name": "production", "tags": None},
    ]

    stale = janitor.stale_groups(groups, now, timedelta(hours=6))

    assert stale == ["e2e-aca-old", "e2e-undated"]


def test_the_ci_identity_may_assign_only_the_apps_two_roles() -> None:
    condition = setup._condition()

    for action in ("write", "delete"):
        assert f"Microsoft.Authorization/roleAssignments/{action}" in condition
    assert setup.OPENAI_USER in condition and setup.AKS_RBAC_CLUSTER_ADMIN in condition
    # Neither Owner, Contributor nor the RBAC administrator role itself can be handed out.
    assert setup.CONTRIBUTOR not in condition and setup.RBAC_ADMIN not in condition
