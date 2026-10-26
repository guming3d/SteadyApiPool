"""scripts/doctor.py: the preflight run before anything is installed, on any OS."""

from __future__ import annotations

import ast
import importlib.util
import socket
import ssl
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "doctor.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("doctor", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["doctor"] = module  # dataclasses look their module up while being built.
    spec.loader.exec_module(module)
    return module


doctor = _load()


def _machine(**changes: Any) -> Any:
    base = doctor.Machine(
        system="Linux",
        release="6.8",
        arch="x86_64",
        distro="ubuntu debian",
        distro_name="Ubuntu 24.04 LTS",
        wsl=False,
        container=False,
        env={"DISPLAY": ":0"},
        python=(3, 12, 3),
        python_path="/usr/bin/python3",
        which=lambda _: None,
    )
    return replace(base, **changes)


def test_it_still_parses_on_old_python() -> None:
    # It has to say "too old" on the Python a machine has, e.g. macOS's 3.9.
    ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 8))


@pytest.mark.parametrize(
    ("changes", "kind", "command"),
    [
        ({"system": "Windows"}, "windows", "winget install -e --id Python.Python.3.13"),
        ({"system": "Darwin"}, "macos", "brew install python@3.13"),
        ({"distro": "ubuntu debian"}, "debian", "sudo apt install python3 python3-venv"),
        ({"distro": "rocky rhel centos fedora"}, "rhel", "sudo dnf install python3.12"),
        ({"distro": "opensuse-leap suse"}, "suse", "sudo zypper install python312"),
        ({"distro": "alpine"}, "linux", "Install Python 3.11 or later"),
    ],
)
def test_an_old_python_gets_the_install_command_for_this_os(
    changes: dict[str, Any], kind: str, command: str
) -> None:
    machine = _machine(python=(3, 9, 6), **changes)

    result = doctor.check_python(machine)

    assert machine.kind == kind
    assert result.status == "fail" and "3.9.6 is too old" in result.title
    assert any(line.startswith(command) for line in result.fix)


def test_debian_without_venv_names_the_package() -> None:
    result = doctor.check_venv(_machine(python=(3, 11, 2)), False, lambda: False)

    assert result.status == "fail"
    assert result.fix == ["sudo apt install python3.11-venv"]
    assert doctor.check_venv(_machine(), True, lambda: False) is None  # Already made.


def test_node_is_only_needed_without_a_prebuilt_ui() -> None:
    machine = _machine(system="Windows")

    missing = doctor.check_node(machine, False, False, True, None)
    old = doctor.check_node(machine, False, True, True, 18)
    release = doctor.check_node(machine, False, True, False, None)

    assert missing.status == "fail"
    assert "winget install -e --id OpenJS.NodeJS.LTS" in missing.fix
    assert any("releases" in line for line in missing.fix)
    assert any("-Tui" in line for line in missing.fix)
    assert old.status == "info" and "older than 22" in old.title
    assert release.status == "ok" and "isn't needed" in release.title
    assert doctor.check_node(machine, False, False, True, 22).status == "ok"
    assert doctor.check_node(machine, True, False, True, None) is None  # The TUI.


@pytest.mark.parametrize(
    ("changes", "where"),
    [
        ({"env": {"SSH_CONNECTION": "10.0.0.2 5022 10.0.0.1 22", "DISPLAY": ":0"}}, "SSH"),
        ({"container": True}, "a container"),
        ({"env": {}}, "Linux without a display"),
    ],
)
def test_without_a_browser_it_points_at_the_device_code(
    changes: dict[str, Any], where: str
) -> None:
    result = doctor.check_browser(_machine(**changes))

    assert where in result.title and "Use a code instead" in result.title


def test_desktops_and_wsl_can_use_the_browser() -> None:
    assert doctor.check_browser(_machine(system="Darwin", env={})).status == "ok"
    assert doctor.check_browser(_machine(system="Windows", env={})).status == "ok"
    assert "WSL" in doctor.check_browser(_machine(wsl=True, env={})).title


def test_sign_in_tools_are_optional() -> None:
    none = doctor.check_sign_in_tools(_machine(system="Darwin"))
    some = doctor.check_sign_in_tools(_machine(which=lambda name: name if name == "azd" else None))

    assert none.status == "info" and "brew install azure-cli" in none.fix
    assert some.status == "ok" and "Azure Developer CLI" in some.title


def test_no_keyring_means_the_app_sign_in_is_not_kept() -> None:
    assert doctor.check_keyring(None) is None
    assert doctor.check_keyring(lambda: None) is None  # No backend venv yet.
    assert doctor.check_keyring(lambda: True).status == "ok"
    assert "until the app stops" in doctor.check_keyring(lambda: False).title


def test_endpoints_follow_mirrors() -> None:
    machine = _machine(
        env={
            "PIP_INDEX_URL": "https://pypi.corp.example/simple",
            "npm_config_registry": "https://npm.corp.example/",
        }
    )

    hosts = [item.host for item in doctor.endpoints(machine, need_pypi=True, need_npm=True)]
    later = [item.host for item in doctor.endpoints(machine, need_pypi=False, need_npm=False)]

    assert hosts == [
        "login.microsoftonline.com",
        "management.azure.com",
        "pypi.corp.example",
        "npm.corp.example",
    ]
    assert later == hosts[:2]


@pytest.mark.parametrize(
    ("reason", "problem"),
    [
        (ssl.SSLCertVerificationError("certificate verify failed"), "certificate"),
        (socket.gaierror(11001, "getaddrinfo failed"), "dns"),
        (TimeoutError("timed out"), "timeout"),
        (ConnectionRefusedError(), "refused"),
    ],
)
def test_network_failures_are_told_apart(reason: Exception, problem: str) -> None:
    assert doctor._reason(reason) == problem


def test_network_results_explain_the_fix() -> None:
    machine = _machine(env={"HTTPS_PROXY": "http://proxy:8080"})
    items = doctor.endpoints(machine, need_pypi=True, need_npm=False)
    problems = {"login.microsoftonline.com": None, "management.azure.com": "certificate"}

    results = doctor.check_network(machine, items, lambda item: problems.get(item.host, "dns"))

    assert [r.status for r in results] == ["info", "ok", "fail", "fail"]
    assert results[0].title == "Proxy: HTTPS_PROXY=http://proxy:8080"
    assert "isn't trusted" in results[2].title
    assert "operating system's store" in results[2].fix[0]
    assert "doesn't resolve" in results[3].title
    assert any("HTTPS_PROXY" in line for line in results[3].fix)
    assert any("PIP_INDEX_URL" in line for line in results[3].fix)


def test_an_old_python_stops_after_saying_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_run", lambda *_, **__: None)

    results = doctor.diagnose(_machine(python=(3, 9, 6)), tui=False, brief=True)

    assert [r.status for r in results] == ["fail"]
