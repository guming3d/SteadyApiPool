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
report = _load("e2e-report")
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


LOG = """../e2e/azure/test_apim.py::TestApim::test_a_short_load_test PASSED       [ 66%]
../e2e/azure/test_apim.py::TestApim::test_removing_the_pool_deletes_its_api FAILED [ 88%]
E           harness.Failure: 400 used by /subscriptions/b9da3a59-1b01-4211-b510-cdbc0790ed2c/x
E           --- app log ---
E           some app line with sk-abcdefghijklmnop0123 in it
FAILED ../e2e/azure/test_apim.py::TestApim::test_removing_the_pool_deletes_its_api - harness.Failure
======= 1 failed, 7 passed in 373.06s =======
"""


def test_a_failure_report_names_the_step_and_its_error() -> None:
    steps, errors, tail = report.summarize(LOG)

    assert steps == ["test_removing_the_pool_deletes_its_api"]
    assert len(errors) == 1 and errors[0].startswith("E           harness.Failure: 400")
    assert tail[-1].startswith("======= 1 failed")


def test_a_failure_report_has_no_ids_or_keys() -> None:
    # The repository is public: the issue never carries a subscription ID or a key.
    text = report.body("apim", "https://github.com/o/r/actions/runs/1", LOG)

    assert "b9da3a59-1b01-4211-b510-cdbc0790ed2c" not in text and "/subscriptions/<id>/" in text
    assert "sk-abcdefghijklmnop0123" not in text
    assert "`test_removing_the_pool_deletes_its_api`" in text
