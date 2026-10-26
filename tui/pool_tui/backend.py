"""Finds the backend the TUI talks to, or starts one from this repository.

The TUI is only a client: with ``--api`` it uses that URL. Otherwise it reuses a backend
already answering on 127.0.0.1:8000 (``scripts/start-local``) or starts ``backend/`` on a free
port with its own virtual environment, logging to a temp file so the screen stays clean.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
DEFAULT_URL = "http://127.0.0.1:8000"
LOG_FILE = Path(tempfile.gettempdir()) / "apim-backend-pool-tui-backend.log"


def healthy(url: str) -> bool:
    try:
        response = httpx.get(f"{url.rstrip('/')}/health", timeout=1.5)
    except httpx.HTTPError:
        return False
    try:
        return response.status_code == 200 and response.json().get("status") == "ok"
    except ValueError:
        return False


def backend_python() -> Path:
    for candidate in (
        BACKEND / ".venv" / "Scripts" / "python.exe",
        BACKEND / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "backend/.venv is missing. Run scripts/start-local.ps1 -Tui (or start-local.sh --tui) "
        "once, or create it: cd backend && python -m venv .venv && pip install -r requirements.txt"
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class LocalBackend:
    """A backend process owned by the TUI, stopped when the TUI exits."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None

    def start(self, port: int | None = None, timeout: float = 30.0) -> str:
        python = backend_python()
        env_file = BACKEND / ".env"
        if not env_file.exists():
            # Nothing to fill in: the setup view chooses the APIM service.
            shutil.copyfile(BACKEND / ".env.example", env_file)
        port = port or free_port()
        url = f"http://127.0.0.1:{port}"
        log = LOG_FILE.open("wb")
        self._process = subprocess.Popen(
            [
                str(python),
                "-m",
                "uvicorn",
                "pool_manager.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=BACKEND,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(
                    f"The backend exited during startup; see {LOG_FILE}:\n{self.tail()}"
                )
            if healthy(url):
                return url
            time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"The backend did not start within {timeout:.0f} s; see {LOG_FILE}")

    def stop(self) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()

    @staticmethod
    def tail(lines: int = 15) -> str:
        try:
            return "\n".join(
                LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
            )
        except OSError:
            return ""


def resolve(url: str | None, start: bool) -> tuple[str, LocalBackend | None]:
    """The backend URL to use, and the process the TUI started for it (if any)."""
    if url:
        if not healthy(url):
            raise RuntimeError(f"No pool manager backend answers at {url}/health")
        return url, None
    if healthy(DEFAULT_URL):
        return DEFAULT_URL, None
    if not start:
        raise RuntimeError(f"No backend at {DEFAULT_URL}; start one or drop --no-start")
    backend = LocalBackend()
    print("Starting the backend...", file=sys.stderr)
    return backend.start(), backend
