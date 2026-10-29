"""The load VM's side of a benchmark, standard library only.

    python3 vm.py fetch                  # tools/* from the relay into this folder
    python3 vm.py run <plan> <run-id>    # run.py on <plan>, results relayed back

Files go through the control app's relay (``/files/``, mock_openai.py), whose address and token
are in ``.relay`` (``<url> <token>``, written by ``perf/ctl.py``, readable by this user only).
``run`` fetches the tools, runs ``run.py`` with its output in ``results/<run-id>`` (and
``log.txt`` there), and uploads that folder to ``results/<run-id>/`` every minute and when it
ends. Started from Run Command through ``systemd-run``, so it keeps going after the command
returns.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _relay() -> tuple[str, str]:
    url, token = (HERE / ".relay").read_text().split()
    return url.rstrip("/"), token


def _request(name: str, data: bytes | None = None, method: str = "GET") -> bytes:
    url, token = _relay()
    request = urllib.request.Request(
        f"{url}/files/{urllib.parse.quote(name)}",
        data=data,
        method=method,
        headers={"x-relay-token": token},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        answer: bytes = response.read()
        return answer


def fetch() -> None:
    for name in json.loads(_request("tools/"))["files"]:
        target = HERE / name.removeprefix("tools/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_request(name))
        print("fetched", name, flush=True)


def sync(folder: Path, run_id: str, sent: dict[str, float]) -> None:
    for path in sorted(folder.glob("*")):
        if path.is_file() and sent.get(path.name) != path.stat().st_mtime:
            mtime = path.stat().st_mtime
            _request(f"results/{run_id}/{path.name}", path.read_bytes(), "PUT")
            sent[path.name] = mtime


def run(plan: str, run_id: str) -> int:
    fetch()
    folder = HERE / "results" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    sent: dict[str, float] = {}
    done = threading.Event()

    def keep_syncing() -> None:
        while not done.wait(60):
            try:
                sync(folder, run_id, sent)
            except Exception as error:  # noqa: BLE001 - the run goes on; the next pass retries
                print("sync failed:", error, flush=True)

    threading.Thread(target=keep_syncing, daemon=True).start()
    with (folder / "log.txt").open("a", encoding="utf-8") as log:
        command = [
            sys.executable,
            "-u",
            str(HERE / "run.py"),
            str(HERE / plan),
            "--out",
            str(folder),
        ]
        code = subprocess.call(command, stdout=log, stderr=subprocess.STDOUT, cwd=HERE)
        log.write(f"exit {code} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    done.set()
    for _ in range(5):
        try:
            sync(folder, run_id, sent)
            break
        except Exception as error:  # noqa: BLE001
            print("final sync failed:", error, flush=True)
            time.sleep(30)
    return code


def main(argv: list[str]) -> int:
    if argv[:1] == ["fetch"]:
        fetch()
        return 0
    if len(argv) == 3 and argv[0] == "run":
        return run(argv[1], argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
