"""Finds Azure OpenAI / Foundry deployments and imports them as pool members.

Members are per-deployment URLs (``.../openai/deployments/<name>``), which become LiteLLM rows
``azure/<name>`` on the account's endpoint, so deployments of the same model with different
names (``gpt-5.6-luna`` and ``gpt-5.6-luna-2``) can share one pool. They authenticate as the
gateway's managed identity: most accounts disable key auth, and reading keys would need
``listKeys`` permission.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any

from .arm import ArmClient, ArmError, segment
from .config import Settings
from .models import (
    DeploymentImport,
    DeploymentImportResult,
    DeploymentView,
    MemberAuth,
    MemberBackendWrite,
)
from .service import PoolManagerService, ServiceError

logger = logging.getLogger(__name__)

_ACCOUNT_KINDS = frozenset({"openai", "aiservices"})
# Only models served on the Azure OpenAI ``/openai/deployments/<name>`` surface. Partner models
# (Anthropic, DeepSeek, xAI, ...) use other paths and would not work with this member URL.
_MODEL_FORMATS = frozenset({"openai"})
_OPENAI_ENDPOINT_KEY = "OpenAI Language Model Instance API"
_CACHE_SECONDS = 300.0  # Scanning many accounts takes ~20s; the UI offers Refresh.
_DEPLOYMENT_SCAN_WORKERS = 16


def suggested_backend_id(prefix: str, account: str, deployment: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9-]+", "-", f"{prefix}{account}-{deployment}")
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if len(slug) <= 80:
        return slug
    digest = hashlib.sha256(f"{account}/{deployment}".encode()).hexdigest()[:8]
    return f"{slug[:71].rstrip('-')}-{digest}"


def _openai_base(properties: Mapping[str, Any]) -> str | None:
    endpoints = properties.get("endpoints")
    base = endpoints.get(_OPENAI_ENDPOINT_KEY) if isinstance(endpoints, Mapping) else None
    base = base or properties.get("endpoint")
    return str(base).rstrip("/") if base else None


def _url_key(url: str) -> str:
    return url.rstrip("/").casefold()


def _ref_key(account_id: str, deployment: str) -> tuple[str, str]:
    return account_id.casefold(), deployment


class DeploymentDiscovery:
    def __init__(self, settings: Settings, arm: ArmClient, pools: PoolManagerService) -> None:
        self._settings = settings
        self._arm = arm
        self._pools = pools
        self._lock = Lock()
        self._cache: tuple[float, list[DeploymentView]] | None = None

    def accounts(self) -> list[dict[str, Any]]:
        """Foundry / Azure OpenAI accounts in the discovery subscriptions (ARM bodies)."""
        return self._accounts()

    def forget(self) -> None:
        """Drops the cached scan, e.g. after a deployment was created here."""
        with self._lock:
            self._cache = None

    def _accounts(self) -> list[dict[str, Any]]:
        accounts: list[dict[str, Any]] = []
        for subscription in self._settings.discovery_subscription_list:
            path = (
                f"/subscriptions/{segment(subscription)}"
                "/providers/Microsoft.CognitiveServices/accounts"
            )
            try:
                found = self._arm.list_arm(path, api_version=self._settings.cognitive_api_version)
                accounts.extend(
                    account
                    for account in found
                    if str(account.get("kind") or "").casefold() in _ACCOUNT_KINDS
                )
            except ArmError as error:
                # One unreadable subscription should not hide the others.
                logger.warning("Cannot list accounts in %s: %s", subscription, error.message)
        return accounts

    def _account_deployments(self, account: Mapping[str, Any]) -> list[DeploymentView]:
        account_id = str(account.get("id") or "")
        properties = account.get("properties") or {}
        base = _openai_base(properties)
        if not account_id or not base:
            return []
        try:
            deployments = list(
                self._arm.list_arm(
                    f"{account_id}/deployments", api_version=self._settings.cognitive_api_version
                )
            )
        except ArmError as error:
            logger.warning("Cannot list deployments of %s: %s", account_id, error.message)
            return []
        account_name = str(account.get("name") or "")
        resource_group = account_id.split("/resourceGroups/", 1)[-1].split("/", 1)[0]
        views = []
        for deployment in deployments:
            deployment_properties = deployment.get("properties") or {}
            model = deployment_properties.get("model") or {}
            name = str(deployment.get("name") or "")
            if (
                not name
                or str(model.get("format") or "").casefold() not in _MODEL_FORMATS
                or deployment_properties.get("provisioningState") != "Succeeded"
            ):
                continue
            sku = deployment.get("sku") or {}
            capacity = sku.get("capacity")
            views.append(
                DeploymentView(
                    account_id=account_id,
                    account_name=account_name,
                    resource_group=resource_group,
                    location=str(account.get("location") or ""),
                    deployment=name,
                    model_name=str(model.get("name") or name),
                    model_version=str(model.get("version") or ""),
                    sku=str(sku.get("name") or ""),
                    capacity=capacity if isinstance(capacity, int) else None,
                    backend_url=f"{base}/openai/deployments/{segment(name)}",
                    local_auth_disabled=properties.get("disableLocalAuth") is True,
                    suggested_backend_id=suggested_backend_id(
                        self._settings.id_prefix, account_name, name
                    ),
                    backend_id=None,
                )
            )
        return views

    def _scan(self) -> list[DeploymentView]:
        accounts = self._accounts()
        with ThreadPoolExecutor(max_workers=_DEPLOYMENT_SCAN_WORKERS) as executor:
            found = [
                view
                for views in executor.map(self._account_deployments, accounts)
                for view in views
            ]
        return sorted(
            found,
            key=lambda view: (
                view.model_name,
                view.model_version,
                view.account_name,
                view.deployment,
            ),
        )

    def list(self, *, refresh: bool = False) -> list[DeploymentView]:
        with self._lock:
            cached = self._cache
            if refresh or cached is None or time.monotonic() - cached[0] > _CACHE_SECONDS:
                cached = (time.monotonic(), self._scan())
                self._cache = cached
        # Members change independently of deployments, so match them on every call; only
        # members of saved pools count, others are pending imports.
        by_url = {
            _url_key(backend.url): backend.id
            for backend in self._pools.list_backends()
            if backend.pools
        }
        return [
            view.model_copy(update={"backend_id": by_url.get(_url_key(view.backend_url))})
            for view in cached[1]
        ]

    def import_deployments(self, request: DeploymentImport, actor: str) -> DeploymentImportResult:
        # Resolve every reference against discovery; never trust client-supplied URLs.
        current = {_ref_key(view.account_id, view.deployment): view for view in self.list()}
        if any(
            _ref_key(ref.account_id, ref.deployment) not in current for ref in request.deployments
        ):
            current = {
                _ref_key(view.account_id, view.deployment): view for view in self.list(refresh=True)
            }
        views = []
        for ref in request.deployments:
            view = current.get(_ref_key(ref.account_id, ref.deployment))
            if view is None:
                raise ServiceError(
                    404, f"Deployment {ref.deployment} was not found or is not ready"
                )
            views.append(view)

        taken = {backend.id.casefold() for backend in self._pools.list_backends()}
        backend_ids: list[str] = []
        created: list[str] = []
        for view in views:
            if view.backend_id:
                backend_ids.append(view.backend_id)
                continue
            # Imported before but not in a pool yet: the same member again.
            pending = next(
                (
                    backend
                    for backend in self._pools.list_backends()
                    if not backend.pools and _url_key(backend.url) == _url_key(view.backend_url)
                ),
                None,
            )
            if pending is not None:
                backend_ids.append(pending.id)
                continue
            backend_id = view.suggested_backend_id
            if backend_id.casefold() in taken:
                # The readable ID belongs to an unrelated backend; keep it and add a stable suffix.
                digest = hashlib.sha256(view.backend_url.encode()).hexdigest()[:8]
                backend_id = f"{backend_id[:71].rstrip('-')}-{digest}"
            taken.add(backend_id.casefold())
            backend = self._pools.put_backend(
                backend_id,
                MemberBackendWrite(
                    title=f"{view.deployment} · {view.account_name} ({view.location})",
                    url=view.backend_url,  # type: ignore[arg-type]
                    auth=MemberAuth(type="managed_identity"),
                ),
                actor,
            )
            backend_ids.append(backend.id)
            created.append(backend.id)
        logger.info("%s imported %d deployment(s) as members", actor, len(created))
        return DeploymentImportResult(backend_ids=backend_ids, created=created)
