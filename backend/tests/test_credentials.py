"""The local credential: tools, sign-ins made from the app, and remembering them."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from azure.identity import AuthenticationRecord, CredentialUnavailableError

from pool_manager.credentials import LocalCredential

SCOPE = "https://management.azure.com/.default"


@dataclass
class _Token:
    token: str
    expires_on: int = 4_102_444_800


class _Tool:
    def __init__(self, name: str, signed_in: bool = True) -> None:
        self.name = name
        self.signed_in = signed_in
        self.calls = 0

    def get_token(self, *_: str, **__: Any) -> _Token:
        self.calls += 1
        if not self.signed_in:
            raise CredentialUnavailableError(f"{self.name} not found on path")
        return _Token(self.name)


def _record(user: str = "dev@contoso.com") -> AuthenticationRecord:
    return AuthenticationRecord("tenant", "client", "login.microsoftonline.com", "home.id", user)


class _Interactive:
    """Stands in for InteractiveBrowserCredential / DeviceCodeCredential."""

    made: list[dict[str, Any]] = []

    def __init__(self, **options: Any) -> None:
        self.options = options
        _Interactive.made.append(options)

    def authenticate(self, **_: Any) -> AuthenticationRecord:
        callback = self.options.get("prompt_callback")
        if callback:
            callback("https://microsoft.com/devicelogin", "ABCD-EFGH", 4_102_444_800)
        return _record()

    def get_token(self, *_: str, **__: Any) -> _Token:
        return _Token("app")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _reset() -> None:
    _Interactive.made = []


def _credential(tmp_path: Path, tools: list[Any], **kwargs: Any) -> LocalCredential:
    return LocalCredential(
        tmp_path / "auth.json",
        tools=[(tool.name, tool) for tool in tools],
        interactive=_Interactive,
        device_code=_Interactive,
        **{"persistent": True, **kwargs},
    )


def test_tools_are_tried_in_order_and_misses_wait_a_minute(tmp_path: Path) -> None:
    cli, azd, pwsh = _Tool("azure_cli", False), _Tool("azd", False), _Tool("powershell")
    clock = _Clock()
    credential = _credential(tmp_path, [cli, azd, pwsh], clock=clock)

    assert credential.get_token(SCOPE).token == "powershell"
    assert credential.source == "powershell"
    credential.get_token(SCOPE)
    assert (cli.calls, azd.calls, pwsh.calls) == (1, 1, 2)  # Misses aren't re-run at once.

    clock.now = 61
    credential.get_token(SCOPE)
    assert (cli.calls, azd.calls) == (2, 2)
    credential.forget_misses()
    credential.get_token(SCOPE)
    assert cli.calls == 3


def test_nothing_signed_in_says_how_to_sign_in(tmp_path: Path) -> None:
    credential = _credential(tmp_path, [_Tool("azure_cli", False)])

    with pytest.raises(CredentialUnavailableError) as error:
        credential.get_token(SCOPE)

    assert "Sign in from the setup page" in str(error.value)
    assert "the Azure CLI: azure_cli not found on path" in str(error.value)


def test_a_browser_sign_in_comes_first_and_survives_a_restart(tmp_path: Path) -> None:
    cli = _Tool("azure_cli")
    credential = _credential(tmp_path, [cli])
    credential.sign_in("tenant-1", SCOPE)

    assert credential.get_token(SCOPE).token == "app"
    assert credential.source == "browser"
    assert credential.remembered is True
    options = _Interactive.made[0]
    assert options["tenant_id"] == "tenant-1"
    assert options["disable_automatic_authentication"] is True
    assert options["cache_persistence_options"].name == "apim-backend-pool"
    saved = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    assert saved["method"] == "browser" and "dev@contoso.com" in saved["record"]
    assert cli.calls == 0

    restarted = _credential(tmp_path, [cli])

    assert restarted.remembered is True
    assert restarted.get_token(SCOPE).token == "app"
    assert _Interactive.made[-1]["authentication_record"].username == "dev@contoso.com"


def test_without_os_encryption_a_sign_in_lasts_until_the_app_stops(tmp_path: Path) -> None:
    credential = _credential(tmp_path, [], persistent=False)
    credential.sign_in(None, SCOPE)

    assert credential.get_token(SCOPE).token == "app"
    assert credential.remembered is False
    assert "cache_persistence_options" not in _Interactive.made[0]
    assert not (tmp_path / "auth.json").exists()


def test_device_code_sign_in_runs_in_the_background(tmp_path: Path) -> None:
    credential = _credential(tmp_path, [])

    prompt = credential.start_device_code(None, SCOPE)

    assert (prompt.verification_uri, prompt.user_code) == (
        "https://microsoft.com/devicelogin",
        "ABCD-EFGH",
    )
    for thread in threading.enumerate():
        if thread.name == "device-code-sign-in":
            thread.join(5)
    flow = credential.device_flow(acknowledge=True)
    assert (flow.state, flow.unacknowledged) == ("done", True)
    assert credential.device_flow().unacknowledged is False
    assert credential.source == "device_code"
    assert (
        json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))["method"] == "device_code"
    )


def test_a_failed_device_code_sign_in_is_reported(tmp_path: Path) -> None:
    class _Failing(_Interactive):
        def authenticate(self, **_: Any) -> AuthenticationRecord:
            raise RuntimeError("AADSTS70016: the user didn't finish in time")

    credential = LocalCredential(
        tmp_path / "auth.json", tools=[], persistent=True, device_code=_Failing
    )

    with pytest.raises(RuntimeError, match="AADSTS70016"):
        credential.start_device_code(None, SCOPE)
    assert credential.device_flow().state == "failed"


def test_sign_out_forgets_the_app_sign_in_only(tmp_path: Path) -> None:
    cli = _Tool("azure_cli")
    credential = _credential(tmp_path, [cli])
    credential.sign_in(None, SCOPE)

    credential.sign_out()

    assert not (tmp_path / "auth.json").exists()
    assert credential.remembered is False
    assert credential.get_token(SCOPE).token == "azure_cli"


def test_a_corrupt_record_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text("{not json", encoding="utf-8")

    credential = _credential(tmp_path, [_Tool("azure_cli")])

    assert credential.remembered is False
    assert credential.get_token(SCOPE).token == "azure_cli"


# -- through the API ------------------------------------------------------------------------


def _app(tmp_path: Path, tools: list[Any]) -> tuple[Any, LocalCredential]:
    from tests.fake_azure import FakeAzure
    from tests.test_app import _client, _settings

    credential = _credential(tmp_path, tools)
    apim = FakeAzure()
    apim.callers.update({"app": "app", "azure_cli": "app"})
    return _client(apim, _settings(), sign_in=credential, credential=credential), credential


def test_setup_reports_how_the_app_is_signed_in(tmp_path: Path) -> None:
    from pool_manager.app import app

    client, _ = _app(tmp_path, [_Tool("azure_cli")])
    try:
        report = client.get("/api/setup").json()
    finally:
        app.dependency_overrides.clear()

    signed_in = report["local_sign_in"]
    assert (signed_in["source"], signed_in["remembered"], signed_in["persistent"]) == (
        "azure_cli",
        False,
        True,
    )
    assert isinstance(signed_in["tools"], list)


def test_device_code_and_sign_out_endpoints(tmp_path: Path) -> None:
    from pool_manager.app import app
    from tests.test_app import WRITE

    client, credential = _app(tmp_path, [_Tool("azure_cli", False)])
    try:
        assert client.post("/api/setup/sign-in/device-code").status_code == 403  # No CSRF header.
        prompt = client.post("/api/setup/sign-in/device-code", headers=WRITE)
        assert prompt.status_code == 200, prompt.text
        assert prompt.json()["user_code"] == "ABCD-EFGH"
        for thread in threading.enumerate():
            if thread.name == "device-code-sign-in":
                thread.join(5)
        status = client.get("/api/setup/sign-in/device-code").json()
        report = client.get("/api/setup").json()
        signed_out = client.post("/api/setup/sign-out", headers=WRITE)
        after = client.get("/api/setup").json()
    finally:
        app.dependency_overrides.clear()

    assert status == {"state": "done", "error": None}
    assert report["checks"][0]["status"] == "ok"
    assert (report["local_sign_in"]["source"], report["local_sign_in"]["remembered"]) == (
        "device_code",
        True,
    )
    assert signed_out.status_code == 204
    assert after["checks"][0] | {"status": "action", "fix": "sign_in"} == after["checks"][0]
    assert credential.remembered is False


def test_deployed_app_has_no_local_sign_in(tmp_path: Path) -> None:
    from pool_manager.app import app
    from tests.fake_azure import FakeAzure
    from tests.test_app import WRITE, _client, _settings

    client = _client(FakeAzure(), _settings(auth_mode="easy_auth"))
    client.headers.update({"X-MS-CLIENT-PRINCIPAL-NAME": "dev@contoso.com"})
    try:
        report = client.get("/api/setup").json()
        device = client.post("/api/setup/sign-in/device-code", headers=WRITE)
    finally:
        app.dependency_overrides.clear()

    assert report["local_sign_in"] is None
    assert device.status_code == 400
