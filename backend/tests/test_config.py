"""Which gateway product the settings choose, and how each product's routes guard it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pool_manager import config
from pool_manager.app import app
from pool_manager.config import Settings, read_saved_target, save_target
from tests.apim.fake_apim import FakeApim
from tests.apim.test_app import _client as apim_client
from tests.apim.test_app import _settings as apim_settings
from tests.fake_azure import FakeAzure
from tests.litellm.fake_litellm import URL
from tests.litellm.test_app import _client as litellm_client
from tests.litellm.test_app import _settings as litellm_settings


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("values", "product"),
    [
        ({}, None),
        ({"subscription_id": "s", "resource_group": "g", "apim_name": "a"}, "apim"),
        ({"subscription_id": "s", "resource_group": "g", "gateway_name": "gw"}, "litellm"),
        ({"gateway_name": "gw", "gateway_url": URL}, "litellm"),
    ],
)
def test_the_name_that_is_set_chooses_the_product(values: dict[str, str], product: str) -> None:
    settings = _settings(auth_mode="disabled", **values)
    settings.validate_runtime()
    assert settings.product == product
    assert settings.has_target is (product is not None)


def test_an_apim_and_a_litellm_name_together_are_refused() -> None:
    settings = _settings(
        auth_mode="disabled",
        subscription_id="s",
        resource_group="g",
        apim_name="a",
        gateway_name="gw",
    )
    with pytest.raises(RuntimeError, match="not both"):
        settings.validate_runtime()


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"auth_mode": "easy_auth"}, "POOL_MANAGER_APIM_NAME"),
        ({"subscription_id": "s", "apim_name": "a"}, "Set all of"),
        ({"auth_mode": "entra", "entra_client_id": "x", "gateway_url": URL}, "can't use"),
    ],
)
def test_incomplete_or_unsafe_settings_fail_fast(values: dict[str, str], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        _settings(**values).validate_runtime()


def test_the_public_app_manages_apim_services() -> None:
    settings = _settings(
        auth_mode="entra",
        entra_client_id="x",
        guest_subscription_id="s",
        guest_resource_group="g",
        guest_apim_name="a",
    )
    settings.validate_runtime()
    assert settings.as_guest().product == "apim"


@pytest.mark.parametrize(
    ("target", "product"),
    [
        ({"subscription_id": "s", "resource_group": "g", "apim_name": "a"}, "apim"),
        ({"subscription_id": "s", "resource_group": "g", "gateway_name": "gw"}, "litellm"),
        ({"gateway_name": "gw", "gateway_url": URL}, "litellm"),
    ],
)
def test_state_files_from_before_both_products_are_read_by_their_fields(
    tmp_path: Path, target: dict[str, str], product: str
) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"target": target}), encoding="utf-8")
    saved = read_saved_target(state)
    assert saved is not None
    assert _settings(auth_mode="disabled").with_saved_target(saved).product == product


def test_the_saved_target_records_its_product_and_only_its_fields(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    # A switch from LiteLLM to APIM leaves no LiteLLM field behind.
    save_target(
        "apim",
        {"subscription_id": "s", "resource_group": "g", "apim_name": "a", "gateway_url": URL},
        state,
    )
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "target": {
            "product": "apim",
            "subscription_id": "s",
            "resource_group": "g",
            "apim_name": "a",
            "tenant_id": "",
        }
    }
    saved = read_saved_target(state)
    assert saved is not None
    assert "gateway_url" not in saved


def test_settings_read_the_saved_product(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    save_target("apim", {"subscription_id": "s", "resource_group": "g", "apim_name": "a"}, state)
    monkeypatch.setattr(config, "STATE_FILE", state)
    monkeypatch.setenv("POOL_MANAGER_AUTH_MODE", "disabled")
    for name in ("SUBSCRIPTION_ID", "RESOURCE_GROUP", "APIM_NAME", "GATEWAY_NAME", "GATEWAY_URL"):
        monkeypatch.delenv(f"POOL_MANAGER_{name}", raising=False)
    monkeypatch.chdir(tmp_path)  # No .env here.
    config.get_settings.cache_clear()
    try:
        assert config.get_settings().product == "apim"
    finally:
        config.get_settings.cache_clear()


def test_each_product_answers_only_its_own_routes() -> None:
    try:
        apim = apim_client(FakeApim(), apim_settings())
        assert apim.get("/api/apim/pools").status_code == 200
        litellm_answer = apim.get("/api/litellm/pools")
        assert litellm_answer.status_code == 409
        assert "isn't a LiteLLM gateway" in litellm_answer.json()["detail"]
    finally:
        app.dependency_overrides.clear()
    try:
        litellm: TestClient = litellm_client(FakeAzure(), litellm_settings())
        assert litellm.get("/api/litellm/pools").status_code == 200
        apim_answer = litellm.get("/api/apim/pools")
        assert apim_answer.status_code == 409
        assert "isn't an APIM service" in apim_answer.json()["detail"]
        # Shared routes work for both.
        assert litellm.get("/api/target").json()["product"] == "litellm"
    finally:
        app.dependency_overrides.clear()
