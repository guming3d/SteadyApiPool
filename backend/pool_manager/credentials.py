"""The credential used when running locally: whatever the user is already signed in with, or a
sign-in made from the app (browser or device code) that survives restarts.

Tried in order:

1. A sign-in made from the setup page. Its tokens live in the OS-encrypted token cache
   (Windows DPAPI, macOS Keychain, Linux libsecret) and an ``AuthenticationRecord`` file says
   which account to use, so a restart doesn't ask again. Without OS encryption (typical on
   headless Linux or WSL) it is kept in memory only and the setup page says so; tokens are never
   written in plain text.
2. Azure CLI (``az login``), Azure Developer CLI (``azd auth login``) and Azure PowerShell
   (``Connect-AzAccount``), whichever is installed and signed in.

Unlike ``DefaultAzureCredential`` nothing probes for environment variables or a managed identity:
off Azure that probe made every miss take about 12 seconds. A tool that reports it isn't
installed or signed in is skipped for a minute instead of being re-run on every request.

Tokens can be requested for any tenant the user belongs to (``tenant_id``), so the setup page can
list subscriptions across tenants.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

Source = Literal["browser", "device_code", "azure_cli", "azd", "powershell"]
AppMethod = Literal["browser", "device_code"]
DeviceState = Literal["idle", "pending", "done", "failed"]

# Which account the app signed in; it holds no secret (the tokens are in the encrypted cache).
RECORD_FILE = Path(__file__).resolve().parent.parent / ".pool-manager-auth.json"
CACHE_NAME = "apim-backend-pool"
_TOOL_RETRY_SECONDS = 60.0
# The browser and device-code flows give up after this long.
_SIGN_IN_TIMEOUT_SECONDS = 300
TOOL_LABELS: dict[Source, str] = {
    "browser": "a browser sign-in from this app",
    "device_code": "a device-code sign-in from this app",
    "azure_cli": "the Azure CLI",
    "azd": "the Azure Developer CLI",
    "powershell": "Azure PowerShell",
}


@dataclass(frozen=True)
class DevicePrompt:
    verification_uri: str
    user_code: str
    expires_on: float


@dataclass
class DeviceFlow:
    state: DeviceState = "idle"
    prompt: DevicePrompt | None = None
    error: str | None = None
    # Set once the flow finished and the caller hasn't reset its cached tokens yet.
    unacknowledged: bool = False


def persistence_available() -> bool:
    """Whether the OS can encrypt a token cache for this user (msal-extensions' backends)."""
    try:
        from msal_extensions import build_encrypted_persistence  # type: ignore[import-untyped]

        with tempfile.TemporaryDirectory() as directory:
            build_encrypted_persistence(str(Path(directory) / "probe.bin"))
        return True
    except Exception as error:  # Linux without libsecret/PyGObject raises ImportError or others.
        logger.info("No OS-encrypted token cache here, sign-ins are kept in memory: %s", error)
        return False


def installed_tools() -> list[str]:
    """Sign-in tools on PATH, for the setup page and the doctor."""
    names = {"azure_cli": "az", "azd": "azd", "powershell": "pwsh"}
    found = [source for source, command in names.items() if shutil.which(command)]
    if "powershell" not in found and shutil.which("powershell"):
        found.append("powershell")  # Windows PowerShell 5.1 can host the Az module too.
    return found


def _default_tools() -> list[tuple[Source, Any]]:
    from azure.identity import (
        AzureCliCredential,
        AzureDeveloperCliCredential,
        AzurePowerShellCredential,
    )

    anywhere = ["*"]
    return [
        ("azure_cli", AzureCliCredential(additionally_allowed_tenants=anywhere)),
        ("azd", AzureDeveloperCliCredential(additionally_allowed_tenants=anywhere)),
        ("powershell", AzurePowerShellCredential(additionally_allowed_tenants=anywhere)),
    ]


def _first_line(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


class LocalCredential:
    def __init__(
        self,
        record_file: Path | None = None,
        *,
        tools: list[tuple[Source, Any]] | None = None,
        persistent: bool | None = None,
        interactive: Callable[..., Any] | None = None,
        device_code: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._record_file = record_file or RECORD_FILE
        self._tools = tools if tools is not None else _default_tools()
        self._persistent = persistence_available() if persistent is None else persistent
        self._interactive_factory = interactive
        self._device_factory = device_code
        self._clock = clock
        self._skipped: dict[Source, float] = {}
        self._app: tuple[AppMethod, Any] | None = None
        self._source: Source | None = None
        self._device = DeviceFlow()
        self._restore()

    # -- the credential protocol ---------------------------------------------------------

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        from azure.core.exceptions import ClientAuthenticationError

        with self._lock:
            app = self._app
            now = self._clock()
            tools = [
                (name, credential)
                for name, credential in self._tools
                if self._skipped.get(name, 0.0) <= now
            ]
        misses: list[str] = []
        candidates: list[tuple[Source, Any]] = [app] if app else []
        for name, credential in [*candidates, *tools]:
            try:
                token = credential.get_token(*scopes, **kwargs)
            except ClientAuthenticationError as error:  # Includes CredentialUnavailableError.
                misses.append(f"{TOOL_LABELS[name]}: {_first_line(error)}")
                if app is None or name != app[0]:
                    with self._lock:
                        self._skipped[name] = self._clock() + _TOOL_RETRY_SECONDS
                continue
            with self._lock:
                self._source = name
            return token
        from azure.identity import CredentialUnavailableError

        raise CredentialUnavailableError(
            "Not signed in to Azure. Sign in from the setup page (browser or device code), "
            "or run az login, azd auth login or Connect-AzAccount. Tried: " + "; ".join(misses)
        )

    # -- what the setup page shows ------------------------------------------------------

    @property
    def source(self) -> Source | None:
        """What answered the last token request."""
        with self._lock:
            return self._source

    @property
    def remembered(self) -> bool:
        """An app sign-in that a restart keeps."""
        with self._lock:
            return self._app is not None and self._persistent and self._record_file.exists()

    @property
    def persistent(self) -> bool:
        return self._persistent

    def forget_misses(self) -> None:
        """Retry every tool on the next request (after 'Check again', or a sign-in elsewhere)."""
        with self._lock:
            self._skipped.clear()

    # -- signing in from the app --------------------------------------------------------

    def _options(self, tenant_id: str | None, record: Any = None) -> dict[str, Any]:
        # Later token requests never open a browser or print a code by themselves; they fail and
        # the setup page asks again. Without a tenant the user signs in to their home tenant.
        options: dict[str, Any] = {
            "additionally_allowed_tenants": ["*"],
            "disable_automatic_authentication": True,
            "timeout": _SIGN_IN_TIMEOUT_SECONDS,
        }
        if tenant_id:
            options["tenant_id"] = tenant_id
        if record is not None:
            options["authentication_record"] = record
        if self._persistent:
            from azure.identity import TokenCachePersistenceOptions

            options["cache_persistence_options"] = TokenCachePersistenceOptions(name=CACHE_NAME)
        return options

    def _browser(self, **options: Any) -> Any:
        if self._interactive_factory is not None:
            return self._interactive_factory(**options)
        from azure.identity import InteractiveBrowserCredential

        return InteractiveBrowserCredential(**options)

    def _device_credential(self, **options: Any) -> Any:
        if self._device_factory is not None:
            return self._device_factory(**options)
        from azure.identity import DeviceCodeCredential

        return DeviceCodeCredential(**options)

    def _remember(self, method: AppMethod, credential: Any, record: Any) -> None:
        if self._persistent and record is not None:
            try:
                self._record_file.write_text(
                    json.dumps({"method": method, "record": record.serialize()}), encoding="utf-8"
                )
            except OSError as error:
                logger.warning("Cannot save the sign-in record to %s: %s", self._record_file, error)
        with self._lock:
            self._app = (method, credential)
            self._source = method
            self._skipped.clear()

    def _restore(self) -> None:
        if not self._persistent or not self._record_file.exists():
            return
        try:
            saved = json.loads(self._record_file.read_text(encoding="utf-8"))
            from azure.identity import AuthenticationRecord

            record = AuthenticationRecord.deserialize(saved["record"])
            method: AppMethod = "device_code" if saved.get("method") == "device_code" else "browser"
        except Exception as error:  # A corrupt or foreign file: ignore it, don't fail startup.
            logger.warning("Ignoring the saved sign-in in %s: %s", self._record_file, error)
            return
        factory = self._device_credential if method == "device_code" else self._browser
        self._app = (method, factory(**self._options(None, record)))

    def sign_in(self, tenant_id: str | None, scope: str) -> None:
        """Browser sign-in on this machine; blocks until it completes or times out."""
        credential = self._browser(**self._options(tenant_id))
        record = credential.authenticate(scopes=[scope])
        self._remember("browser", credential, record)

    def start_device_code(
        self, tenant_id: str | None, scope: str, wait: float = 30.0
    ) -> DevicePrompt:
        """Starts a device-code sign-in and returns the code to enter at the URL. The sign-in
        finishes in the background; :meth:`device_flow` reports how it went."""
        ready = threading.Event()

        def prompt(uri: str, code: str, expires_on: Any) -> None:
            expires = (
                expires_on.timestamp() if hasattr(expires_on, "timestamp") else float(expires_on)
            )
            with self._lock:
                self._device.prompt = DevicePrompt(uri, code, expires)
            ready.set()

        credential = self._device_credential(**self._options(tenant_id), prompt_callback=prompt)
        with self._lock:
            self._device = DeviceFlow(state="pending")

        def run() -> None:
            try:
                record = credential.authenticate(scopes=[scope])
            except Exception as error:
                with self._lock:
                    self._device.state = "failed"
                    self._device.error = _first_line(error)
                ready.set()
                return
            self._remember("device_code", credential, record)
            with self._lock:
                self._device.state = "done"
                self._device.unacknowledged = True

        threading.Thread(target=run, name="device-code-sign-in", daemon=True).start()
        if not ready.wait(wait):
            raise TimeoutError("Microsoft Entra didn't return a device code in time")
        with self._lock:
            flow = self._device
        if flow.prompt is None:
            raise RuntimeError(flow.error or "The device-code sign-in didn't start")
        return flow.prompt

    def device_flow(self, acknowledge: bool = False) -> DeviceFlow:
        """The device-code sign-in's state; ``acknowledge`` clears ``unacknowledged``."""
        with self._lock:
            flow = DeviceFlow(**vars(self._device))
            if acknowledge:
                self._device.unacknowledged = False
        return flow

    def sign_out(self) -> None:
        """Forgets the app's sign-in (not az / azd / PowerShell, which the user manages)."""
        with self._lock:
            self._app = None
            self._source = None
            self._skipped.clear()
            self._device = DeviceFlow()
        self._record_file.unlink(missing_ok=True)
