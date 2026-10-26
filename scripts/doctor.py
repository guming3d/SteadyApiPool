#!/usr/bin/env python3
"""Checks this machine before apim-backend-pool runs locally, and says how to fix what's missing.

Standard library only, and no syntax newer than Python 3.8, so it runs before anything is
installed and on the old Python a machine may have (it then says so):

    python scripts/doctor.py            # everything, for any OS
    python scripts/doctor.py --brief    # only problems; start-local runs this
    python scripts/doctor.py --tui      # for the terminal UI (Node.js not needed)

Exits 1 when something blocks running the app, 0 otherwise.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
STATIC_INDEX = BACKEND / "pool_manager" / "static" / "index.html"
MIN_PYTHON = (3, 11)
MIN_NODE = 22
TIMEOUT = 8.0

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
LABELS = {OK: "OK  ", INFO: "INFO", WARN: "WARN", FAIL: "FAIL"}


@dataclass
class Result:
    status: str
    title: str
    fix: list[str] = field(default_factory=list)


@dataclass
class Machine:
    """What the checks look at, gathered once so tests can describe any machine."""

    system: str  # Windows, Darwin or Linux
    release: str
    arch: str
    distro: str  # ID and ID_LIKE from /etc/os-release, e.g. "ubuntu debian"
    distro_name: str
    wsl: bool
    container: bool
    env: dict[str, str]
    python: tuple[int, ...]
    python_path: str
    which: Callable[[str], str | None]

    @property
    def kind(self) -> str:
        """windows, macos, debian, fedora, rhel, suse, arch or linux: picks the install commands."""
        if self.system == "Windows":
            return "windows"
        if self.system == "Darwin":
            return "macos"
        for name in self.distro.split():
            if name in ("debian", "ubuntu"):
                return "debian"
            if name == "fedora":
                return "fedora"
            if name in ("rhel", "centos", "rocky", "almalinux", "ol", "azurelinux", "mariner"):
                return "rhel"
            if name in ("suse", "opensuse", "sles", "opensuse-leap", "opensuse-tumbleweed"):
                return "suse"
            if name in ("arch", "manjaro"):
                return "arch"
        return "linux"

    @property
    def remote(self) -> bool:
        return bool(self.env.get("SSH_CONNECTION") or self.env.get("SSH_TTY"))

    @property
    def headless(self) -> bool:
        """No browser can open here: an SSH session, a container, or Linux without a display."""
        if self.remote or self.container:
            return True
        if self.system == "Linux" and not self.wsl:
            return not (self.env.get("DISPLAY") or self.env.get("WAYLAND_DISPLAY"))
        return False


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def this_machine() -> Machine:
    os_release = {}
    for line in _read("/etc/os-release").splitlines():
        key, _, value = line.partition("=")
        os_release[key] = value.strip().strip('"')
    system = platform.system()
    if system == "Windows":
        release = platform.version()  # 10.0.22000 and later is Windows 11
    elif system == "Darwin":
        release = platform.mac_ver()[0]
    else:
        release = platform.release()
    return Machine(
        system=system,
        release=release,
        arch=platform.machine(),
        distro=f"{os_release.get('ID', '')} {os_release.get('ID_LIKE', '')}".strip(),
        distro_name=os_release.get("PRETTY_NAME", ""),
        wsl="microsoft" in _read("/proc/version").lower(),
        container=Path("/.dockerenv").exists() or "docker" in _read("/proc/1/cgroup"),
        env=dict(os.environ),
        python=tuple(sys.version_info[:3]),
        python_path=sys.executable,
        which=shutil.which,
    )


# -- install commands, per OS ------------------------------------------------------------

PYTHON_INSTALL = {
    "windows": [
        "winget install -e --id Python.Python.3.13",
        "or the installer from https://www.python.org/downloads/windows/"
        " (tick 'Add python.exe to PATH').",
        "If 'python' opens the Microsoft Store, turn that alias off in Settings > Apps >"
        " Advanced app settings > App execution aliases.",
    ],
    "macos": [
        "brew install python@3.13",
        "or the installer from https://www.python.org/downloads/macos/"
        " (the Python macOS ships is 3.9).",
    ],
    "debian": [
        "sudo apt install python3 python3-venv",
        "Ubuntu 22.04 and Debian 11 ship an older Python:"
        " sudo apt install python3.11 python3.11-venv",
    ],
    "fedora": ["sudo dnf install python3"],
    "rhel": ["sudo dnf install python3.12"],
    "suse": ["sudo zypper install python312"],
    "arch": ["sudo pacman -S python"],
    "linux": [
        "Install Python 3.11 or later from your distribution or https://www.python.org/downloads/"
    ],
}

NODE_INSTALL = {
    "windows": ["winget install -e --id OpenJS.NodeJS.LTS"],
    "macos": ["brew install node"],
}
NODE_INSTALL_OTHER = [
    "Install Node.js 22 or later from https://nodejs.org/en/download (it also shows nvm and fnm);"
    " distribution packages are often older.",
]

AZ_INSTALL = {
    "windows": ["winget install -e --id Microsoft.AzureCLI"],
    "macos": ["brew install azure-cli"],
    "debian": ["curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash"],
}
AZ_INSTALL_OTHER = ["https://learn.microsoft.com/cli/azure/install-azure-cli"]


# -- checks -------------------------------------------------------------------------------


def check_python(machine: Machine) -> Result:
    version = ".".join(str(part) for part in machine.python)
    if machine.python >= MIN_PYTHON:
        return Result(OK, f"Python {version} ({machine.python_path})")
    return Result(
        FAIL,
        f"Python {version} is too old; the app needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or later",
        PYTHON_INSTALL[machine.kind],
    )


def check_venv(machine: Machine, venv_exists: bool, probe: Callable[[], bool]) -> Result | None:
    """Only matters until backend/.venv exists. Debian and Ubuntu split venv's pip out."""
    if venv_exists:
        return None
    if probe():
        return Result(OK, "Python can create virtual environments (venv and pip)")
    version = f"{machine.python[0]}.{machine.python[1]}"
    fix = (
        [f"sudo apt install python{version}-venv"]
        if machine.kind == "debian"
        else ["Reinstall Python with pip included (ensurepip is missing)."]
    )
    return Result(FAIL, "Python can't create virtual environments: ensurepip is missing", fix)


def _ensurepip_works() -> bool:
    try:
        import ensurepip

        return bool(ensurepip.version())
    except Exception:  # Debian's stub raises; a stripped build has no wheels.
        return False


def node_version(machine: Machine, run: Callable[[list[str]], str | None]) -> int | None:
    if not machine.which("node"):
        return None
    out = run(["node", "-p", "process.versions.node"])
    try:
        return int((out or "").strip().split(".")[0])
    except ValueError:
        return None


def check_node(
    machine: Machine, tui: bool, prebuilt: bool, has_frontend: bool, major: int | None
) -> Result | None:
    if tui:
        return None
    install = NODE_INSTALL.get(machine.kind, NODE_INSTALL_OTHER)
    if major is not None and major >= MIN_NODE and has_frontend:
        return Result(OK, f"Node.js {major} for the browser UI")
    if prebuilt:
        why = "not installed" if major is None else f"version {major} is older than {MIN_NODE}"
        if not has_frontend:
            return Result(OK, "Browser UI is prebuilt (release package); Node.js isn't needed")
        return Result(
            INFO,
            f"Node.js {why}: the app serves the UI already built in backend/pool_manager/static",
            ["Only UI development needs Node.js:"] + install,
        )
    found = "not installed" if major is None else f"version {major} is too old"
    return Result(
        FAIL,
        f"Node.js {MIN_NODE}+ is needed to build the browser UI, and it's {found}",
        install
        + [
            "Or download the release package, which has the UI built in and needs no Node.js:"
            " https://github.com/guming3d/apim-backend-pool-manager/releases",
            "Or use the terminal UI: start-local with -Tui (PowerShell) or --tui (bash).",
        ],
    )


TOOLS = (("az", "Azure CLI"), ("azd", "Azure Developer CLI"), ("pwsh", "Azure PowerShell"))


def check_sign_in_tools(machine: Machine) -> Result:
    found = [label for exe, label in TOOLS if machine.which(exe)]
    if (
        machine.system == "Windows"
        and "Azure PowerShell" not in found
        and machine.which("powershell")
    ):
        found.append("Azure PowerShell")
    if found:
        return Result(
            OK,
            "Installed sign-in tools: "
            + ", ".join(found)
            + " (the app uses one you're signed in to)",
        )
    return Result(
        INFO,
        "No Azure CLI, azd or PowerShell: sign in on the app's setup page instead",
        ["Optional, to sign in once for every tool:"]
        + AZ_INSTALL.get(machine.kind, AZ_INSTALL_OTHER),
    )


def check_browser(machine: Machine) -> Result:
    if machine.headless:
        where = (
            "an SSH session"
            if machine.remote
            else "a container"
            if machine.container
            else "Linux without a display"
        )
        return Result(
            INFO,
            f"No browser here ({where}): on the setup page, choose 'Use a code instead'",
            [
                "Sign in on any device with the code it shows. Over SSH, also forward the port to"
                " open the app on your own machine: ssh -L 5174:localhost:5174 <host>",
            ],
        )
    if machine.wsl:
        return Result(
            INFO,
            "WSL: if the browser sign-in doesn't open, choose 'Use a code instead'"
            " on the setup page",
        )
    return Result(OK, "A browser can open here for sign-in")


def check_keyring(probe: Callable[[], bool | None] | None) -> Result | None:
    """Whether a sign-in made in the app can be stored encrypted (DPAPI, Keychain, libsecret)."""
    if probe is None:
        return None
    available = probe()
    if available is None or available:
        return (
            None
            if available is None
            else Result(OK, "Sign-ins made in the app are remembered (encrypted)")
        )
    return Result(
        INFO,
        "No encrypted store for the app's own sign-in: it lasts until the app stops",
        [
            "az login, azd auth login and Connect-AzAccount keep their own sign-ins, so use one of"
            " them to avoid signing in after every restart.",
        ],
    )


def _backend_persistence() -> bool | None:
    python = BACKEND / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        return None
    out = _run(
        [
            str(python),
            "-c",
            "from pool_manager.credentials import persistence_available as p; print(p())",
        ],
        cwd=BACKEND,
    )
    return None if out is None else out.strip() == "True"


# -- network ------------------------------------------------------------------------------


@dataclass
class Endpoint:
    host: str
    url: str
    purpose: str
    required: bool = True


def endpoints(machine: Machine, need_pypi: bool, need_npm: bool) -> list[Endpoint]:
    items = [
        Endpoint(
            "login.microsoftonline.com",
            "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration",
            "Azure sign-in",
        ),
        Endpoint("management.azure.com", "https://management.azure.com/", "Azure Resource Manager"),
    ]
    if need_pypi:
        index = machine.env.get("PIP_INDEX_URL") or "https://pypi.org/simple/"
        items.append(Endpoint(_host(index), index.rstrip("/") + "/pip/", "Python packages"))
    if need_npm:
        registry = machine.env.get("npm_config_registry") or "https://registry.npmjs.org/"
        items.append(Endpoint(_host(registry), registry, "npm packages"))
    return items


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or url


def tls_context(machine: Machine) -> ssl.SSLContext:
    """The OS trust store, as the app uses it (truststore), including a company CA."""
    context = ssl.create_default_context()
    if machine.system == "Darwin":
        # python.org builds trust their own OpenSSL bundle only; add the macOS keychains.
        out = _run(
            [
                "security",
                "find-certificate",
                "-a",
                "-p",
                "/System/Library/Keychains/SystemRootCertificates.keychain",
                "/Library/Keychains/System.keychain",
            ]
        )
        if out:
            with contextlib.suppress(ssl.SSLError):
                context.load_verify_locations(cadata=out)
    return context


def probe_endpoint(endpoint: Endpoint, context: ssl.SSLContext) -> str | None:
    """None when the host answers over HTTPS (any status), else why not."""
    request = urllib.request.Request(
        endpoint.url, headers={"User-Agent": "apim-backend-pool-doctor"}
    )
    try:
        # urllib follows HTTPS_PROXY / NO_PROXY like pip, npm and the app (httpx).
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context):
            return None
    except urllib.error.HTTPError as error:
        if error.code == 407:
            return "proxy"
        return None
    except urllib.error.URLError as error:
        return _reason(error.reason)
    except TimeoutError:
        return "timeout"
    except (ssl.SSLError, OSError) as error:
        return _reason(error)


def _reason(reason: object) -> str:
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "certificate"
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    text = str(reason)
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return "certificate"
    if "timed out" in text:
        return "timeout"
    return text or "error"


PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")


def check_network(
    machine: Machine, items: list[Endpoint], probe: Callable[[Endpoint], str | None]
) -> list[Result]:
    proxies = [f"{name}={machine.env[name]}" for name in PROXY_VARS if machine.env.get(name)]
    results: list[Result] = []
    if proxies:
        results.append(Result(INFO, "Proxy: " + ", ".join(proxies)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(items) or 1) as pool:
        outcomes = list(pool.map(probe, items))
    for endpoint, problem in zip(items, outcomes):  # noqa: B905 (strict= needs 3.10)
        name = f"{endpoint.host} ({endpoint.purpose})"
        if problem is None:
            results.append(Result(OK, f"Reached {name}"))
            continue
        status = FAIL if endpoint.required else WARN
        results.append(
            Result(
                status,
                f"Can't reach {name}: {_PROBLEM.get(problem, problem)}",
                _network_fix(problem, endpoint, bool(proxies)),
            )
        )
    return results


_PROBLEM = {
    "certificate": "its certificate isn't trusted",
    "dns": "the name doesn't resolve",
    "timeout": "no answer within 8 seconds",
    "refused": "connection refused",
    "proxy": "the proxy asks for credentials",
}


def _network_fix(problem: str, endpoint: Endpoint, proxied: bool) -> list[str]:
    if problem == "certificate":
        lines = [
            "A proxy is probably inspecting TLS with a company certificate. Install that"
            " certificate in the operating system's store; the app trusts the OS store.",
        ]
        if endpoint.purpose == "Python packages":
            lines.append(
                "pip 24.2+ uses the OS store too; older pip: set PIP_CERT=<company-ca.pem>."
            )
        if endpoint.purpose == "npm packages":
            lines.append("npm: set NODE_EXTRA_CA_CERTS=<company-ca.pem>.")
        return lines
    if problem == "proxy":
        return ["Put the credentials in the proxy URL: HTTPS_PROXY=http://user:password@proxy:8080"]
    lines = []
    if proxied:
        lines.append(
            "Check the proxy address in HTTPS_PROXY, and that NO_PROXY doesn't list this host."
        )
    else:
        lines.append(
            "Behind a proxy? Set HTTPS_PROXY=http://proxy:port (and NO_PROXY=localhost,127.0.0.1)."
        )
    if endpoint.purpose == "Python packages":
        lines.append("Or use a reachable mirror: PIP_INDEX_URL=https://<mirror>/simple/")
    elif endpoint.purpose == "npm packages":
        lines.append("Or use a reachable mirror: npm_config_registry=https://<mirror>/")
    else:
        lines.append(f"Ask your network team to allow https://{endpoint.host}.")
    return lines


# -- running it ---------------------------------------------------------------------------


def _run(command: list[str], cwd: Path | None = None) -> str | None:
    try:
        done = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def diagnose(machine: Machine, tui: bool, brief: bool = False) -> list[Result]:
    venv_exists = (BACKEND / ".venv").exists()
    prebuilt = STATIC_INDEX.exists()
    has_frontend = (FRONTEND / "package.json").exists()
    major = node_version(machine, _run)
    use_vite = not tui and has_frontend and major is not None and major >= MIN_NODE
    results = [check_python(machine)]
    if machine.python < MIN_PYTHON:
        return results  # Install a newer Python first; the rest depends on it.
    for result in (
        check_venv(machine, venv_exists, _ensurepip_works),
        check_node(machine, tui, prebuilt, has_frontend, major),
        check_sign_in_tools(machine),
        check_browser(machine),
        # Starting the backend's Python takes a second, and --brief wouldn't show it anyway.
        check_keyring(None if brief else _backend_persistence),
    ):
        if result:
            results.append(result)
    need_npm = use_vite and not (FRONTEND / "node_modules").exists()
    context = tls_context(machine)
    results.extend(
        check_network(
            machine,
            endpoints(machine, need_pypi=not venv_exists, need_npm=need_npm),
            lambda endpoint: probe_endpoint(endpoint, context),
        )
    )
    return results


def describe(machine: Machine) -> str:
    system = {"Darwin": "macOS"}.get(machine.system, machine.system)
    parts = [f"{system} {machine.release}".strip(), machine.arch]
    if machine.distro_name:
        parts.append(machine.distro_name)
    for flag, label in (
        (machine.wsl, "WSL"),
        (machine.container, "container"),
        (machine.remote, "SSH"),
    ):
        if flag:
            parts.append(label)
    return ", ".join(part for part in parts if part)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--brief", action="store_true", help="print only problems")
    parser.add_argument(
        "--tui", action="store_true", help="for the terminal UI: Node.js isn't needed"
    )
    args = parser.parse_args(argv)
    # Plain ASCII output, but paths and proxy URLs may not be.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")

    machine = this_machine()
    results = diagnose(machine, args.tui, args.brief)
    shown = [r for r in results if r.status in (WARN, FAIL)] if args.brief else results
    if not args.brief:
        print(f"apim-backend-pool doctor: {describe(machine)}\n")
    for result in shown:
        print(f"  {LABELS[result.status]}  {result.title}")
        for line in result.fix:
            print(f"        {line}")
    failed = any(result.status == FAIL for result in results)
    if not args.brief:
        print(
            "\nSomething above blocks the app." if failed else "\nReady to run scripts/start-local."
        )
    elif failed and machine.python >= MIN_PYTHON:  # Skipping the checks can't fix the Python.
        print(
            "Fix the above, or skip these checks with -SkipChecks (PowerShell)"
            " or --skip-checks (bash)."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
