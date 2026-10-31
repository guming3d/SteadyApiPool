"""What the live Azure tests run on: the app as a user runs it, the Azure CLI, and a sign-in that
lasts as long as a run.

- ``AppProcess`` starts the backend (``uvicorn pool_manager.app:app``) from a copy of the package
  in a temporary folder, so its state file, its encrypted key store and its sign-in record are
  that folder's, never the checkout's; with no ``.env`` there, only the settings given here.
- ``Api`` calls it as the browser does: JSON, the ``X-Pool-Manager`` header on writes, and
  ``wait`` for anything Azure takes minutes over.
- ``AzureLogin`` keeps the Azure CLI signed in. The app gets every token from it (``az account
  get-access-token``); in GitHub Actions the CLI's federated sign-in lasts only as long as the
  workflow's OIDC token (minutes), so it signs in again with a new one every few minutes.
- ``az`` runs the Azure CLI for what the app doesn't do: the run's resource group and its
  removal, and reading what the app created to check it.

Nothing here prints a key or a token.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "backend" / "pool_manager"
T = TypeVar("T")


class Failure(AssertionError):
    pass


# -- configuration ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Env:
    """The run's settings, from E2E_* environment variables (docs/testing.md)."""

    subscription_id: str
    tenant_id: str | None
    location: str
    # Foundry accounts (ARM IDs) with a deployment of the same model each, made once by
    # scripts/e2e-azure-setup.py and never touched by a run.
    foundry_accounts: list[str]
    foundry_deployment: str
    publisher_email: str
    run_id: str
    # Leave what a run created, to look at it (delete the e2e-* resource groups yourself).
    keep: bool
    # The LiteLLM gateways' size: ``trial`` (one replica, Burstable database) or ``small`` (up
    # to two replicas, General Purpose database, Redis), for subscriptions that can't create a
    # Burstable server (Azure answers SkuNotAvailable though it lists the size).
    gateway_size: str = "trial"

    @classmethod
    def load(cls) -> Env:
        def need(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise Failure(f"{name} is not set (see docs/testing.md)")
            return value

        # Git Bash on Windows rewrites a value that starts with a slash into a Windows path
        # (C:/Program Files/Git/subscriptions/...) before Python sees it: take the ARM ID back.
        accounts = [
            a.strip()[a.find("/subscriptions/") :] if "/subscriptions/" in a else a.strip()
            for a in need("E2E_FOUNDRY_ACCOUNTS").split(",")
            if a.strip()
        ]
        if len(accounts) < 2:
            raise Failure("E2E_FOUNDRY_ACCOUNTS needs two Foundry account IDs, comma-separated")
        run = os.environ.get("E2E_RUN_ID") or time.strftime("%m%d%H%M") + os.urandom(2).hex()
        return cls(
            subscription_id=need("E2E_SUBSCRIPTION_ID"),
            tenant_id=os.environ.get("E2E_TENANT_ID") or None,
            location=os.environ.get("E2E_LOCATION", "swedencentral"),
            foundry_accounts=accounts,
            foundry_deployment=need("E2E_FOUNDRY_DEPLOYMENT"),
            publisher_email=os.environ.get("E2E_PUBLISHER_EMAIL", "e2e@contoso.com"),
            run_id="".join(c for c in run.lower() if c.isalnum())[:12],
            keep=os.environ.get("E2E_KEEP", "") == "1",
            gateway_size=os.environ.get("E2E_GATEWAY_SIZE") or "trial",
        )


# -- the Azure CLI ---------------------------------------------------------------------------


def az(*args: str, check: bool = True) -> Any:
    """Runs ``az`` and returns its JSON output (None for none)."""
    executable = shutil.which("az") or shutil.which("az.cmd")
    if executable is None:
        raise Failure("The Azure CLI (az) is not installed")
    result = subprocess.run(
        [executable, *args, "--only-show-errors", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        if not check:
            return None
        raise Failure(f"az {' '.join(args[:3])} failed: {result.stderr.strip()[:2000]}")
    return json.loads(result.stdout) if result.stdout.strip() else None


class AzureLogin:
    """Signs the Azure CLI in again with a new GitHub OIDC token every ``every`` seconds, in
    GitHub Actions with ``id-token: write``; anywhere else the CLI's own sign-in is used as is."""

    def __init__(self, env: Env, every: float = 240.0) -> None:
        self._env = env
        self._every = every
        self._last = 0.0
        self.federated = bool(
            os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL") and os.environ.get("AZURE_CLIENT_ID")
        )

    def refresh(self, force: bool = False) -> None:
        if not self.federated or (not force and time.monotonic() - self._last < self._every):
            return
        response = httpx.get(
            os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"],
            params={"audience": "api://AzureADTokenExchange"},
            headers={"Authorization": f"Bearer {os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}"},
            timeout=30,
        )
        response.raise_for_status()
        token = response.json()["value"]
        az(
            "login",
            "--service-principal",
            "--username",
            os.environ["AZURE_CLIENT_ID"],
            "--tenant",
            os.environ.get("AZURE_TENANT_ID") or self._env.tenant_id or "",
            "--federated-token",
            token,
        )
        az("account", "set", "--subscription", self._env.subscription_id)
        self._last = time.monotonic()

    def check(self) -> dict[str, Any]:
        """Who the CLI is signed in as, in the run's subscription."""
        self.refresh(force=True)
        account: dict[str, Any] = az("account", "show")
        if account.get("id") != self._env.subscription_id:
            az("account", "set", "--subscription", self._env.subscription_id)
            account = az("account", "show")
        return account


# -- the app ---------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class AppProcess:
    """The backend, as ``scripts/start-local`` runs it, from a copy of the package."""

    env: Env
    folder: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="e2e-app-")))
    port: int = field(default_factory=_free_port)
    process: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        shutil.copytree(
            PACKAGE,
            self.folder / "pool_manager",
            ignore=shutil.ignore_patterns("__pycache__", "static"),
        )
        environment = {
            **os.environ,
            "POOL_MANAGER_AUTH_MODE": "disabled",
            "POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS": self.env.subscription_id,
            "PYTHONUNBUFFERED": "1",
        }
        # Nothing about the gateway from the developer's environment: the run chooses it.
        for name in list(environment):
            if name.startswith("POOL_MANAGER_") and name not in {
                "POOL_MANAGER_AUTH_MODE",
                "POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS",
            }:
                environment.pop(name)
        log = (self.folder / "app.log").open("wb")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "pool_manager.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=self.folder,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise Failure(f"The app exited at start:\n{self.log_tail()}")
            try:
                if httpx.get(f"{self.url}/api/config", timeout=5).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise Failure(f"The app didn't start:\n{self.log_tail()}")

    def log_tail(self, lines: int = 60) -> str:
        path = self.folder / "app.log"
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        return "\n".join(text.splitlines()[-lines:])

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()


class Api:
    """The app's HTTP API, called as its browser UI calls it."""

    def __init__(self, app: AppProcess, login: AzureLogin) -> None:
        self._app = app
        self._login = login
        self._http = httpx.Client(base_url=f"{app.url}/api", timeout=httpx.Timeout(900, connect=10))

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
        expect: int = 200,
    ) -> Any:
        self._login.refresh()
        headers = {} if method == "GET" else {"X-Pool-Manager": "1"}
        response = self._http.request(method, path, json=json, params=params, headers=headers)
        if response.status_code != expect:
            raise Failure(
                f"{method} /api{path} answered {response.status_code}, expected {expect}: "
                f"{response.text[:2000]}\n--- app log ---\n{self._app.log_tail(30)}"
            )
        return response.json() if response.content else None

    def get(self, path: str, **params: Any) -> Any:
        given = {key: value for key, value in params.items() if value is not None}
        return self.request("GET", path, params=given or None)

    def post(self, path: str, body: Any = None, *, expect: int = 200) -> Any:
        return self.request("POST", path, json=body, expect=expect)

    def put(self, path: str, body: Any, *, expect: int = 200) -> Any:
        return self.request("PUT", path, json=body, expect=expect)

    def status(self, method: str, path: str, body: Any = None) -> int:
        """Only the status code, for calls expected to be refused."""
        headers = {} if method == "GET" else {"X-Pool-Manager": "1"}
        return self._http.request(method, path, json=body, headers=headers).status_code

    def wait(
        self,
        what: str,
        read: Callable[[], T],
        done: Callable[[T], bool],
        *,
        timeout: float,
        every: float = 15.0,
        failed: Callable[[T], str | None] | None = None,
    ) -> T:
        """Reads until ``done``; fails at once when ``failed`` says why, or after ``timeout``."""
        deadline = time.monotonic() + timeout
        last: T | None = None
        while True:
            self._login.refresh()
            last = read()
            if done(last):
                return last
            reason = failed(last) if failed else None
            if reason:
                raise Failure(f"{what}: {reason}")
            if time.monotonic() > deadline:
                raise Failure(f"{what}: not done after {timeout:.0f} s; last: {str(last)[:2000]}")
            time.sleep(every)


# -- resources -------------------------------------------------------------------------------

E2E_TAG = "steadyapipool-e2e"


def create_group(env: Env, name: str) -> None:
    """The run's resource group, tagged so scripts/e2e-azure-janitor deletes it if a run never
    could (the app deploys into a group that exists; it never deletes one)."""
    az(
        "group",
        "create",
        "--name",
        name,
        "--location",
        env.location,
        "--tags",
        f"{E2E_TAG}={env.run_id}",
        f"created={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
    )


def delete_group(name: str) -> None:
    az("group", "delete", "--name", name, "--yes", "--no-wait", check=False)


def purge_apim(env: Env, name: str) -> None:
    """A deleted APIM service is kept for 48 hours (the app never purges); a test run's goes."""
    az(
        "apim",
        "deletedservice",
        "purge",
        "--service-name",
        name,
        "--location",
        env.location,
        check=False,
    )
