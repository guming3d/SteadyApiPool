"""Checks that the Foundry deployments in a pool serve the same model.

A member's model is read from its deployment's ARM resource (``properties.model.name``) and is
never inferred from a deployment or backend name: a deployment called ``gpt-4o`` can serve
gpt-4o-mini. Members are matched to deployments by URL, ``https://<account endpoint>/openai/
deployments/<name>``, which is what imported members use and where APIM really sends traffic.

Members that do not point at a Foundry deployment (custom URLs, or a whole account where the
client picks the deployment) are not checked. A Foundry deployment whose model cannot be read
fails the check, so an unverifiable member cannot slip into a pool. Model versions may differ.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any
from urllib.parse import unquote, urlsplit

from .arm import ArmClient, ArmError, segment
from .config import Settings
from .models import MemberModel, ModelCheck

logger = logging.getLogger(__name__)

_ACCOUNT_KINDS = frozenset({"openai", "aiservices"})
# Hosts of Azure OpenAI / Foundry endpoints, including the sovereign clouds.
_FOUNDRY_HOST_SUFFIXES = (
    ".openai.azure.com",
    ".cognitiveservices.azure.com",
    ".services.ai.azure.com",
    ".openai.azure.us",
    ".cognitiveservices.azure.us",
    ".openai.azure.cn",
    ".cognitiveservices.azure.cn",
)
_DEPLOYMENTS_PATH = "/openai/deployments/"
_CACHE_SECONDS = 300.0
_WORKERS = 8


def deployment_target(url: str | None) -> tuple[str, str] | None:
    """``(host, deployment)`` when the URL addresses one deployment, else ``None``."""
    if not url:
        return None
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if not parts.hostname or _DEPLOYMENTS_PATH not in f"{path}/":
        return None
    name = path.partition(_DEPLOYMENTS_PATH)[2]
    if not name or "/" in name:
        return None
    return parts.hostname.casefold(), unquote(name)


def _is_foundry_host(host: str) -> bool:
    return host.endswith(_FOUNDRY_HOST_SUFFIXES)


def _account_hosts(account: Mapping[str, Any]) -> set[str]:
    properties = account.get("properties") or {}
    endpoints = properties.get("endpoints")
    urls = [
        properties.get("endpoint"),
        *(endpoints.values() if isinstance(endpoints, Mapping) else []),
    ]
    return {
        host.casefold() for url in urls if isinstance(url, str) and (host := urlsplit(url).hostname)
    }


class FoundryModelCheck:
    def __init__(self, settings: Settings, arm: ArmClient) -> None:
        self._settings = settings
        self._arm = arm
        self._lock = Lock()
        self._hosts: tuple[float, dict[str, str]] | None = None

    def _scan_hosts(self) -> dict[str, str]:
        hosts: dict[str, str] = {}
        for subscription in self._settings.discovery_subscription_list:
            path = (
                f"/subscriptions/{segment(subscription)}"
                "/providers/Microsoft.CognitiveServices/accounts"
            )
            try:
                accounts = list(
                    self._arm.list_arm(path, api_version=self._settings.cognitive_api_version)
                )
            except ArmError as error:
                logger.warning("Cannot list accounts in %s: %s", subscription, error.message)
                continue
            for account in accounts:
                account_id = str(account.get("id") or "")
                if account_id and str(account.get("kind") or "").casefold() in _ACCOUNT_KINDS:
                    for host in _account_hosts(account):
                        hosts.setdefault(host, account_id)
        return hosts

    def _account_for(self, host: str) -> str | None:
        with self._lock:
            cached = self._hosts
            if cached is None or time.monotonic() - cached[0] > _CACHE_SECONDS:
                cached = (time.monotonic(), self._scan_hosts())
                self._hosts = cached
            if host not in cached[1] and _is_foundry_host(host):
                # The account may be newer than the cache.
                cached = (time.monotonic(), self._scan_hosts())
                self._hosts = cached
        return cached[1].get(host)

    def account_for_url(self, url: str | None) -> str | None:
        """ARM ID of the Foundry account a deployment URL points at, if it is in discovery."""
        target = deployment_target(url)
        return self._account_for(target[0]) if target else None

    def resolve(self, backend_id: str, url: str | None) -> MemberModel:
        def result(**values: Any) -> MemberModel:
            fields: dict[str, Any] = {
                "deployment_id": None,
                "model_name": None,
                "model_version": None,
                "problem": None,
                **values,
            }
            return MemberModel(backend_id=backend_id, **fields)

        target = deployment_target(url)
        if target is None:
            return result()
        host, deployment = target
        account_id = self._account_for(host)
        if account_id is None:
            if not _is_foundry_host(host):
                return result()
            return result(
                problem=f"no Azure OpenAI / Foundry account in the discovery subscriptions "
                f"serves {host}; add its subscription to POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS",
            )
        deployment_id = f"{account_id}/deployments/{segment(deployment)}"
        try:
            body = self._arm.get_arm_json(
                deployment_id, api_version=self._settings.cognitive_api_version
            )
        except ArmError as error:
            return result(
                deployment_id=deployment_id,
                problem=f"cannot read deployment {deployment}: {error.status} {error.message}",
            )
        if body is None:
            return result(
                deployment_id=deployment_id, problem=f"deployment {deployment} does not exist"
            )
        model = (body.get("properties") or {}).get("model") or {}
        name = str(model.get("name") or "")
        if not name:
            return result(
                deployment_id=deployment_id,
                problem=f"deployment {deployment} does not report a model name",
            )
        return result(
            deployment_id=deployment_id,
            model_name=name,
            model_version=str(model.get("version") or "") or None,
        )

    def check(self, members: list[tuple[str, str | None]]) -> ModelCheck:
        """``members`` are ``(backend_id, url)`` pairs in pool order."""
        with ThreadPoolExecutor(max_workers=_WORKERS) as executor:
            resolved = list(executor.map(lambda member: self.resolve(*member), members))
        # Model names compare case-insensitively; keep each one's first spelling and its members.
        groups: dict[str, tuple[str, list[str]]] = {}
        for member in resolved:
            if member.model_name:
                group = groups.setdefault(member.model_name.casefold(), (member.model_name, []))
                group[1].append(member.backend_id)
        problems = [member for member in resolved if member.problem]
        message = None
        if problems:
            message = "; ".join(
                f"cannot verify the model of {member.backend_id}: {member.problem}"
                for member in problems
            )
        elif len(groups) > 1:
            found = ", ".join(f"{name} ({', '.join(ids)})" for name, ids in groups.values())
            message = f"pool members must serve the same Foundry model; found {found}"
        return ModelCheck(
            members=resolved,
            model_names=[name for name, _ in groups.values()],
            ok=message is None,
            message=message,
        )
