"""Drives the load VM from the operator's machine: Azure CLI Run Command, no SSH.

    python perf/ctl.py init                   # tells the VM the relay's address and token
    python perf/ctl.py upload                 # the tools and perf/plans/*.json, to the relay
    python perf/ctl.py start <plan> <run-id>  # runs plans/<plan> on the VM in the background
    python perf/ctl.py status <run-id>        # whether it runs, and the end of its log
    python perf/ctl.py stop <run-id>
    python perf/ctl.py fetch <run-id>         # results/<run-id>/ into perf/results/<run-id>/
    python perf/ctl.py key <name>             # a gateway key, read from stdin, onto the VM

Files go through the control app's relay (mock_openai.py ``/files/``) with the token in
``perf/.relay-token``. Secrets (the relay token, gateway keys) reach the VM only as protected
Run Command parameters and are written there for the ``perf`` user alone. The rig's names
come from ``perf/rig.json`` (``PERF_RIG=<name>``: ``perf/rig-<name>.json``).
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = ["loadgen.py", "run.py", "vm.py", "requirements.txt"]
VM_HOME = "/home/perf/perf"


def rig() -> dict[str, str]:
    """``perf/rig.json``, or ``perf/rig-<name>.json`` with ``PERF_RIG=<name>`` (a second region)."""
    name = os.environ.get("PERF_RIG")
    found: dict[str, str] = json.loads(
        (HERE / (f"rig-{name}.json" if name else "rig.json")).read_text()
    )
    return found


def relay_token() -> str:
    path = HERE / ".relay-token"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
    return path.read_text().strip()


def az(*args: str) -> str:
    result = subprocess.run(
        ["az", *args], capture_output=True, text=True, check=False, shell=sys.platform == "win32"
    )
    if result.returncode != 0:
        raise SystemExit(f"az {' '.join(args[:3])} failed:\n{result.stderr}")
    return result.stdout


def _script_file(script: str) -> str:
    """The script as ``@<file>``: a multi-line argument doesn't survive cmd.exe on Windows."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
    ) as f:
        f.write(script)
    return f"@{f.name}"


def vm_shell(script: str) -> str:
    r = rig()
    out = az(
        "vm",
        "run-command",
        "invoke",
        "-g",
        r["resource_group"],
        "-n",
        r["vm"],
        "--command-id",
        "RunShellScript",
        "--scripts",
        _script_file(script),
        "--query",
        "value[0].message",
        "-o",
        "tsv",
    )
    return out.replace("\\n", "\n")


def vm_secret_shell(script: str, **secret_values: str) -> str:
    """Runs ``script`` with ``secret_values`` as protected parameters (environment variables
    there), so they appear in no command line, log or Run Command output."""
    r = rig()
    name = f"perf-{secrets.token_hex(4)}"
    parameters = [f"{k}={v}" for k, v in secret_values.items()]
    try:
        az(
            "vm",
            "run-command",
            "create",
            "-g",
            r["resource_group"],
            "--vm-name",
            r["vm"],
            "--name",
            name,
            "--script",
            _script_file(script),
            "--protected-parameters",
            *parameters,
            "--timeout-in-seconds",
            "600",
            "-o",
            "none",
        )
        return az(
            "vm",
            "run-command",
            "show",
            "-g",
            r["resource_group"],
            "--vm-name",
            r["vm"],
            "--name",
            name,
            "--instance-view",
            "--query",
            "instanceView.{out:output,err:error,code:exitCode}",
            "-o",
            "json",
        )
    finally:
        az(
            "vm",
            "run-command",
            "delete",
            "-g",
            r["resource_group"],
            "--vm-name",
            r["vm"],
            "--name",
            name,
            "--yes",
        )


def _relay(name: str, data: bytes | None = None, method: str = "GET") -> bytes:
    request = urllib.request.Request(
        f"{rig()['control_url']}/files/{urllib.parse.quote(name)}",
        data=data,
        method=method,
        headers={"x-relay-token": relay_token()},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        answer: bytes = response.read()
        return answer


def init() -> None:
    script = f"""set -e
mkdir -p {VM_HOME}/.keys
umask 077
printf '%s %s\\n' "$RELAY_URL" "$RELAY_TOKEN" > {VM_HOME}/.relay
chown -R perf:perf {VM_HOME}
chmod 700 {VM_HOME}/.keys
echo ready
"""
    print(vm_secret_shell(script, RELAY_URL=rig()["control_url"], RELAY_TOKEN=relay_token()))


def upload() -> None:
    files = [(HERE / name, f"tools/{name}") for name in TOOLS]
    files += [
        (path, f"tools/plans/{path.name}") for path in sorted((HERE / "plans").glob("*.json"))
    ]
    for path, name in files:
        _relay(name, path.read_bytes(), "PUT")
        print("uploaded", name)


def start(plan: str, run_id: str) -> None:
    script = f"""set -e
cd {VM_HOME}
read URL TOKEN < .relay
curl -sf -H "x-relay-token: $TOKEN" "$URL/files/tools/vm.py" -o vm.py && chown perf:perf vm.py
sudo -u perf python3 vm.py fetch > /dev/null
[ -x .venv/bin/python ] || sudo -u perf python3 -m venv .venv
sudo -u perf .venv/bin/pip -q install -r requirements.txt
systemd-run --unit perf-{run_id} --uid perf -p LimitNOFILE=1048576 --working-directory={VM_HOME} \\
  {VM_HOME}/.venv/bin/python vm.py run plans/{plan} {run_id}
echo started perf-{run_id}
"""
    print(vm_shell(script))


def status(run_id: str) -> None:
    print(
        vm_shell(
            f"systemctl is-active perf-{run_id}; tail -n 40 {VM_HOME}/results/{run_id}/log.txt"
        )
    )


def stop(run_id: str) -> None:
    print(vm_shell(f"systemctl stop perf-{run_id}; systemctl is-active perf-{run_id} || true"))


def fetch(run_id: str) -> None:
    target = HERE / "results" / run_id
    target.mkdir(parents=True, exist_ok=True)
    names = json.loads(_relay(f"results/{run_id}/"))["files"]
    for name in names:
        (HERE / name).write_bytes(_relay(name))
    print(f"fetched {len(names)} files into", target)


def key(name: str) -> None:
    if not name.replace("-", "").isalnum():
        raise SystemExit("a key name is letters, digits and dashes")
    value = sys.stdin.read().strip()
    script = f"""set -e
umask 077
printf '%s' "$KEY_VALUE" > {VM_HOME}/.keys/{name}
chown perf:perf {VM_HOME}/.keys/{name}
echo stored {name}
"""
    print(vm_secret_shell(script, KEY_VALUE=value))


def main(argv: list[str]) -> int:
    commands: dict[str, tuple[Callable[..., None], int]] = {
        "init": (init, 0),
        "upload": (upload, 0),
        "start": (start, 2),
        "status": (status, 1),
        "stop": (stop, 1),
        "fetch": (fetch, 1),
        "key": (key, 1),
    }
    if not argv or argv[0] not in commands or len(argv) - 1 != commands[argv[0]][1]:
        print(__doc__)
        return 2
    function, _ = commands[argv[0]]
    function(*argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
