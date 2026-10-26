"""The master keys of external gateways (e.g. on AKS), kept for the local app only.

They are stored encrypted by the operating system (msal-extensions: DPAPI on Windows, the
Keychain on macOS, libsecret on Linux), in ``backend/.pool-manager-gateways.bin``. Where the OS
has no encryption (Linux or WSL without libsecret), they stay in memory until the app stops and
are never written in plain text, like local sign-ins (credentials.py). Gateways on Container Apps
need none of this: their master key is a Container App secret, read through ARM when needed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock
from typing import Any

from .credentials import persistence_available

logger = logging.getLogger(__name__)

STORE_FILE = Path(__file__).resolve().parent.parent / ".pool-manager-gateways.bin"


def _key(url: str) -> str:
    return url.rstrip("/").casefold()


class SecretStore:
    def __init__(self, path: Path | None = None, *, persistent: bool | None = None) -> None:
        self._path = path or STORE_FILE
        self._persistent = persistence_available() if persistent is None else persistent
        self._lock = Lock()
        self._memory: dict[str, str] = {}
        self._persistence: Any = None

    @property
    def persistent(self) -> bool:
        return self._persistent

    def _store(self) -> Any:
        if self._persistence is None:
            from msal_extensions import build_encrypted_persistence  # type: ignore[import-untyped]

            self._persistence = build_encrypted_persistence(str(self._path))
        return self._persistence

    def _load(self) -> dict[str, str]:
        if not self._persistent:
            return dict(self._memory)
        try:
            raw = self._store().load()
        except FileNotFoundError:
            return {}
        except Exception as error:  # A store another user or machine encrypted.
            logger.warning("Cannot read the stored gateway keys: %s", error)
            return {}
        try:
            values = json.loads(raw or "{}")
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in values.items()} if isinstance(values, dict) else {}

    def _save(self, values: dict[str, str]) -> None:
        if not self._persistent:
            self._memory = dict(values)
            return
        self._store().save(json.dumps(values))

    def get(self, url: str) -> str | None:
        with self._lock:
            return self._load().get(_key(url))

    def put(self, url: str, master_key: str) -> None:
        with self._lock:
            values = self._load()
            values[_key(url)] = master_key
            self._save(values)

    def forget(self, url: str) -> None:
        with self._lock:
            values = self._load()
            if values.pop(_key(url), None) is not None:
                self._save(values)
