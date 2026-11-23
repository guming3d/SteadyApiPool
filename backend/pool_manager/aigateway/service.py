"""Pool and member operations against one Azure AI Gateway, all through ARM.

Pools are aliases in the gateway's default workspace; members are model registrations under a
model provider. Every write is ETag-conditional (``If-Match``, or ``If-None-Match: *`` on
create), and only resources whose description carries ``MANAGED_MARKER`` are ever changed or
deleted; the rest are shown read-only.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..arm import ArmClient, ArmError, segment
from ..config import Settings
from ..discovery import KnownMember
from ..errors import ServiceError
from ..model_check import FoundryModelCheck, deployment_target
from ..models import ModelCheck, is_valid_resource_id
from .models import (
    BackendView,
    Capabilities,
    CleanupConfirm,
    MemberBackendWrite,
    PoolEndpoint,
    PoolView,
    PoolWrite,
    RemovalConfirm,
    RemovalPlan,
    RemovalResult,
)
from .resources import (
    WORKSPACE,
    alias_properties,
    child_name,
    custom_model_properties,
    custom_provider_properties,
    foundry_model_properties,
    foundry_provider_properties,
    is_managed,
    is_protected,
    member_ids,
    name_of,
    parse_model,
    parse_pool,
    provider_of,
)

logger = logging.getLogger(__name__)

# The runtime surface clients call: ``<host>/default/models/openai/v1``.
OPENAI_PATH = "/default/models/openai/v1"


def translate(error: ArmError) -> ServiceError:
    if error.status == 412:
        return ServiceError(409, "The resource changed since it was loaded. Reload and retry.")
    if error.status in {401, 403}:
        return ServiceError(
            502,
            f"The app identity is not authorized for this AI Gateway operation: {error.message}",
        )
    if error.status in {400, 404, 409}:
        return ServiceError(error.status, error.message)
    return ServiceError(502, f"AI Gateway returned {error.status} {error.code}: {error.message}")


def _concurrency(etag: str | None) -> dict[str, str]:
    return {"If-Match": etag} if etag else {"If-None-Match": "*"}


def managed_key_name(id_prefix: str) -> str:
    """The one runtime key this app creates, for its test calls and load tests."""
    return child_name(f"{id_prefix}test")


@dataclass
class _Index:
    """The workspace as one read: providers by name, models, and member IDs both ways."""

    providers: dict[str, dict[str, Any]]
    models: list[dict[str, Any]]
    # Model registration ARM ID (case-folded) -> member ID, and member ID (case-folded) -> model.
    member_of: dict[str, str]
    by_member: dict[str, dict[str, Any]]

    def provider(self, model: Mapping[str, Any]) -> dict[str, Any]:
        body = self.providers.get(provider_of(str(model.get("id") or "")).casefold()) or {}
        return dict(body.get("properties") or {})


def _etag(found: tuple[dict[str, Any], str | None]) -> str | None:
    body, etag = found
    return etag or (str(body["etag"]) if body.get("etag") else None)


class AiGatewayService:
    def __init__(self, settings: Settings, arm: ArmClient) -> None:
        self._settings = settings
        self._arm = arm
        self._version = settings.aigateway_api_version
        self._protected = settings.protected_prefix_list
        self._gateway: dict[str, Any] | None = None
        self._models = FoundryModelCheck(settings, arm)

    # -- helpers -----------------------------------------------------------------------

    def _require_id(self, resource_id: str) -> None:
        if not is_valid_resource_id(resource_id):
            raise ServiceError(400, "IDs use letters, digits and hyphens (max 80 characters)")

    def _require_writable(self, resource_id: str) -> None:
        self._require_id(resource_id)
        if is_protected(resource_id, self._protected):
            raise ServiceError(
                403, f"{resource_id} is owned by another control plane and is read-only here"
            )

    def _get(self, path: str) -> tuple[dict[str, Any], str | None] | None:
        return self._arm.get_json(path, api_version=self._version)

    def _list(self, path: str) -> list[dict[str, Any]]:
        return list(self._arm.list(path, api_version=self._version))

    @staticmethod
    def _alias_path(pool_id: str) -> str:
        return f"{WORKSPACE}/aliases/{segment(pool_id)}"

    @staticmethod
    def _provider_path(provider: str) -> str:
        return f"{WORKSPACE}/modelProviders/{segment(provider)}"

    def _model_path(self, provider: str, model: str) -> str:
        return f"{self._provider_path(provider)}/models/{segment(model)}"

    def _model_path_of(self, model_id: str) -> str:
        return self._model_path(provider_of(model_id), name_of(model_id))

    # -- the gateway -------------------------------------------------------------------

    def gateway(self) -> dict[str, Any]:
        """The gateway resource. Cached once it has an identity; until then it is re-read, so
        enabling the identity shows up without a restart."""
        if self._gateway is None or not (self._gateway.get("identity") or {}).get("principalId"):
            found = self._get("")
            self._gateway = found[0] if found else {}
        return self._gateway

    def identity(self) -> dict[str, Any]:
        """The gateway's ``identity`` block with its provisioning state, read fresh."""
        found = self._get("")
        self._gateway = found[0] if found else {}
        identity = dict(self._gateway.get("identity") or {})
        identity["provisioningState"] = (self._gateway.get("properties") or {}).get(
            "provisioningState"
        )
        return identity

    def forget_gateway(self) -> None:
        self._gateway = None

    def _gateway_value(self, section: str, key: str) -> str | None:
        try:
            gateway = self.gateway()
        except ArmError as error:
            logger.warning("Cannot read the AI Gateway: %s", error.message)
            return None
        return str((gateway.get(section) or {}).get(key) or "") or None

    def gateway_url(self) -> str | None:
        try:
            gateway = self.gateway()
        except ArmError as error:
            logger.warning("Cannot read the AI Gateway: %s", error.message)
            return None
        host = ((gateway.get("properties") or {}).get("frontend") or {}).get("defaultHostname")
        return f"https://{host}" if host else None

    def plan(self) -> tuple[str, str]:
        """The gateway's SKU and region, e.g. ("Standard", "eastus2")."""
        try:
            gateway = self.gateway()
        except ArmError:
            return "", ""
        return str((gateway.get("sku") or {}).get("name") or ""), str(gateway.get("location") or "")

    def app_insights_id(self) -> str | None:
        """The Application Insights component the gateway's telemetry exporter sends to."""
        try:
            exporters = self._list(f"{WORKSPACE}/telemetryExporters")
        except ArmError as error:
            logger.info("Cannot list the gateway's telemetry exporters: %s", error.message)
            return None
        for exporter in exporters:
            properties = exporter.get("properties") or {}
            resource = (properties.get("applicationInsights") or {}).get("resourceId")
            if resource:
                return str(resource)
        return None

    def capabilities(self) -> Capabilities:
        sku, location = self.plan()
        return Capabilities(
            gateway_name=self._settings.aigateway_name,
            resource_group=self._settings.resource_group,
            id_prefix=self._settings.id_prefix,
            protected_prefixes=self._protected,
            gateway_resource_id=self._arm.resource_id,
            gateway_principal_id=self._gateway_value("identity", "principalId"),
            gateway_url=self.gateway_url(),
            location=location or None,
            sku=sku or None,
            app_insights_id=self.app_insights_id(),
            discovery_subscriptions=self._settings.discovery_subscription_list,
        )

    def endpoint(self, pool_id: str) -> PoolEndpoint:
        pool = self.get_pool(pool_id)
        url = self.gateway_url()
        return PoolEndpoint(
            pool_id=pool.id,
            base_url=f"{url}{OPENAI_PATH}" if url else None,
            model_name=pool.model_name,
        )

    # -- the workspace -----------------------------------------------------------------

    def _index(self) -> _Index:
        providers = {
            str(body.get("name") or "").casefold(): body
            for body in self._list(f"{WORKSPACE}/modelProviders")
        }
        models = self._list(f"{WORKSPACE}/models")
        member_of = member_ids(models)
        by_member = {
            member_of[str(model.get("id") or "").casefold()].casefold(): model for model in models
        }
        return _Index(providers, models, member_of, by_member)

    def _aliases(self) -> list[dict[str, Any]]:
        return self._list(f"{WORKSPACE}/aliases")

    def _pools_of(self, aliases: list[dict[str, Any]], index: _Index) -> dict[str, list[str]]:
        """The pools each member (model ARM ID, case-folded) is in."""
        pools: dict[str, list[str]] = {}
        for alias in aliases:
            for target in (alias.get("properties") or {}).get("backendModels") or []:
                key = str(target.get("resourceId") or "").casefold()
                pools.setdefault(key, []).append(str(alias.get("name") or ""))
        return pools

    # -- members -----------------------------------------------------------------------

    def _views(self) -> tuple[_Index, list[BackendView]]:
        index = self._index()
        pools = self._pools_of(self._aliases(), index)
        views = [
            parse_model(
                model,
                None,
                index.member_of[str(model.get("id") or "").casefold()],
                index.provider(model),
                pools.get(str(model.get("id") or "").casefold(), []),
                self._protected,
            )
            for model in index.models
        ]
        return index, sorted(views, key=lambda view: view.id.casefold())

    def list_backends(self) -> list[BackendView]:
        try:
            return self._views()[1]
        except ArmError as error:
            raise translate(error) from error

    def get_backend(self, backend_id: str) -> BackendView:
        self._require_id(backend_id)
        try:
            index = self._index()
            model = index.by_member.get(backend_id.casefold())
            if model is None:
                raise ServiceError(404, f"Member {backend_id} was not found")
            found = self._get(self._model_path_of(str(model.get("id") or "")))
            if found is None:
                raise ServiceError(404, f"Member {backend_id} was not found")
            pools = self._pools_of(self._aliases(), index)
        except ArmError as error:
            raise translate(error) from error
        model_id = str(found[0].get("id") or model.get("id") or "")
        return parse_model(
            {**found[0], "id": model_id},
            _etag(found),
            index.member_of.get(model_id.casefold(), backend_id),
            index.provider(found[0] if found[0].get("id") else model),
            pools.get(model_id.casefold(), []),
            self._protected,
        )

    def known_members(self) -> list[KnownMember]:
        return [
            KnownMember(id=view.id, url=view.url or "", imported=True)
            for view in self.list_backends()
        ]

    def foundry_accounts(self) -> list[str]:
        """Foundry accounts behind members the gateway calls as its managed identity; it needs
        a data-plane role on each of them."""
        accounts: dict[str, str] = {}
        for view in self.list_backends():
            if (
                view.kind == "foundry"
                and view.auth_type == "managed_identity"
                and view.deployment_id
            ):
                account = view.deployment_id.rpartition("/deployments/")[0]
                if account:
                    accounts.setdefault(account.casefold(), account)
        return sorted(accounts.values(), key=str.casefold)

    def _taken(self, index: _Index) -> set[str]:
        return {str(model.get("name") or "").casefold() for model in index.models} | set(
            index.by_member
        )

    def _unique(self, wanted: str, taken: set[str], *, dots: bool = False) -> str:
        name = child_name(wanted, dots=dots)
        if name.casefold() in taken:
            digest = hashlib.sha256(wanted.encode()).hexdigest()[:8]
            name = child_name(f"{name[:54]}-{digest}", dots=dots)
        return name

    def _foundry_provider(self, account_id: str, index: _Index, actor: str) -> str:
        """This app's model provider for a Foundry account, created when there is none."""
        for name, body in index.providers.items():
            properties = body.get("properties") or {}
            ids = ((properties.get("foundry") or {}).get("resourceIds")) or []
            if is_managed(properties) and any(
                str(item).casefold() == account_id.casefold() for item in ids
            ):
                return str(body.get("name") or name)
        account = self._arm.get_arm_json(
            account_id, api_version=self._settings.cognitive_api_version
        )
        if account is None:
            raise ServiceError(404, f"Foundry account {name_of(account_id)} was not found")
        provider = self._unique(
            f"{self._settings.id_prefix}{name_of(account_id)}", set(index.providers)
        )
        self._arm.request(
            "PUT",
            self._provider_path(provider),
            api_version=self._version,
            json_body={"properties": foundry_provider_properties(account)},
            headers={"If-None-Match": "*"},
        )
        logger.info("%s created model provider %s for %s", actor, provider, account_id)
        index.providers[provider.casefold()] = {
            "name": provider,
            "properties": foundry_provider_properties(account),
        }
        return provider

    def add_deployment_member(self, member_id: str, title: str, url: str, actor: str) -> str:
        """Registers a Foundry deployment (found by discovery) as a model, under this app's
        provider for its account, called as the gateway's managed identity."""
        target = deployment_target(url)
        account_id = self._models.account_for_url(url)
        if target is None or account_id is None:
            raise ServiceError(400, f"{url} is not a deployment of a discovered Foundry account")
        deployment_id = f"{account_id}/deployments/{segment(target[1])}"
        try:
            deployment = (
                self._arm.get_arm_json(
                    deployment_id, api_version=self._settings.cognitive_api_version
                )
                or {}
            )
            properties = deployment.get("properties") or {}
            capabilities = properties.get("capabilities") or {}
            embeddings = str(capabilities.get("embeddings") or "").casefold() == "true" and not str(
                capabilities.get("chatCompletion") or ""
            )
            version = str((properties.get("model") or {}).get("version") or "") or None
            index = self._index()
            provider = self._foundry_provider(account_id, index, actor)
            model = self._unique(member_id, self._taken(index))
            self._arm.request(
                "PUT",
                self._model_path(provider, model),
                api_version=self._version,
                json_body={
                    "properties": foundry_model_properties(
                        title, url, deployment_id, version, embeddings
                    )
                },
                headers={"If-None-Match": "*"},
            )
        except ArmError as error:
            raise translate(error) from error
        logger.info("%s registered %s as member %s", actor, deployment_id, model)
        return model

    def put_backend(self, backend_id: str, write: MemberBackendWrite, actor: str) -> BackendView:
        """Creates or updates a custom member: its own provider (base URL and secret header)
        and the model registration under it."""
        self._require_writable(backend_id)
        secret = write.auth.secret.get_secret_value() if write.auth.secret else None
        try:
            index = self._index()
            existing = index.by_member.get(backend_id.casefold())
            if existing is None and write.etag:
                raise ServiceError(404, f"Member {backend_id} no longer exists")
            if existing is not None:
                if write.etag is None:
                    raise ServiceError(409, f"Member {backend_id} already exists")
                provider_properties = index.provider(existing)
                if not (
                    is_managed(existing.get("properties") or {}) and is_managed(provider_properties)
                ):
                    raise ServiceError(
                        409, f"Member {backend_id} was not created by this app and is read-only"
                    )
                if provider_properties.get("modelProviderType") != "Custom":
                    raise ServiceError(409, f"Member {backend_id} is a Foundry deployment")
                provider = provider_of(str(existing.get("id") or ""))
                model = name_of(str(existing.get("id") or ""))
            else:
                if secret is None:
                    raise ServiceError(400, "A credential secret is required")
                model = self._unique(backend_id, self._taken(index))
                provider = self._unique(model, set(index.providers))
            body = custom_provider_properties(backend_id, write, secret)
            if existing is not None and secret is None:
                # PATCH keeps the secret the gateway has; only the URL and title change.
                body.pop("authentication")
                body.pop("modelProviderType")
                self._arm.request(
                    "PATCH",
                    self._provider_path(provider),
                    api_version=self._version,
                    json_body={"properties": body},
                    headers={"If-Match": "*"},
                )
            else:
                self._arm.request(
                    "PUT",
                    self._provider_path(provider),
                    api_version=self._version,
                    json_body={"properties": body},
                    headers={"If-Match": "*"} if existing is not None else {"If-None-Match": "*"},
                )
            self._arm.request(
                "PUT",
                self._model_path(provider, model),
                api_version=self._version,
                json_body={"properties": custom_model_properties(backend_id, write)},
                headers=_concurrency(write.etag),
            )
        except ArmError as error:
            raise translate(error) from error
        logger.info("%s wrote custom member %s", actor, model)
        return self.get_backend(model if existing is None else backend_id)

    def delete_backend(self, backend_id: str, etag: str | None, actor: str) -> None:
        self._require_writable(backend_id)
        backend = self.get_backend(backend_id)
        if not backend.managed:
            raise ServiceError(409, f"Member {backend_id} was not created by this app")
        if backend.pools:
            raise ServiceError(409, f"Member is in pool(s): {', '.join(backend.pools)}")
        try:
            self._delete_model(backend.resource_id, etag or backend.etag)
        except ArmError as error:
            raise translate(error) from error
        logger.info("%s deleted member %s", actor, backend_id)

    def _delete_model(self, model_id: str, etag: str | None) -> None:
        """Deletes a model registration, and its provider (this app's) once it has none left."""
        provider = provider_of(model_id)
        self._arm.request(
            "DELETE",
            self._model_path_of(model_id),
            api_version=self._version,
            headers={"If-Match": etag or "*"},
            allow_not_found=True,
        )
        found = self._get(self._provider_path(provider))
        if found is None or not is_managed(found[0].get("properties") or {}):
            return
        if self._list(f"{self._provider_path(provider)}/models"):
            return
        self._arm.request(
            "DELETE",
            self._provider_path(provider),
            api_version=self._version,
            headers={"If-Match": _etag(found) or "*"},
            allow_not_found=True,
        )

    # -- pools -------------------------------------------------------------------------

    def list_pools(self) -> list[PoolView]:
        try:
            index = self._index()
            pools = [
                parse_pool(alias, None, self._protected, index.member_of)
                for alias in self._aliases()
            ]
        except ArmError as error:
            raise translate(error) from error
        return sorted(pools, key=lambda pool: pool.id.casefold())

    def get_pool(self, pool_id: str) -> PoolView:
        self._require_id(pool_id)
        try:
            found = self._get(self._alias_path(pool_id))
            if found is None:
                raise ServiceError(404, f"Pool {pool_id} was not found")
            index = self._index()
        except ArmError as error:
            raise translate(error) from error
        return parse_pool(found[0], _etag(found), self._protected, index.member_of)

    def _member_models(self, backend_ids: list[str], index: _Index) -> list[dict[str, Any]]:
        models = []
        for backend_id in backend_ids:
            model = index.by_member.get(backend_id.casefold())
            if model is None:
                raise ServiceError(400, f"Member {backend_id} is not a model in this gateway")
            models.append(model)
        return models

    def _check(self, backend_ids: list[str], index: _Index) -> ModelCheck:
        views = {
            view.id.casefold(): view
            for view in (
                parse_model(
                    model,
                    None,
                    index.member_of[str(model.get("id") or "").casefold()],
                    index.provider(model),
                    [],
                    self._protected,
                )
                for model in self._member_models(backend_ids, index)
            )
        }
        members = [views[backend_id.casefold()] for backend_id in backend_ids]
        return self._models.check(
            [(view.id, view.url if view.kind == "foundry" else None) for view in members],
            {view.id: view.deployment_id for view in members if view.deployment_id},
        )

    def check_models(self, backend_ids: list[str]) -> ModelCheck:
        """The model check ``put_pool`` enforces, without writing anything."""
        try:
            index = self._index()
        except ArmError as error:
            raise translate(error) from error
        return self._check(backend_ids, index)

    def put_pool(self, pool_id: str, write: PoolWrite, actor: str) -> PoolView:
        self._require_writable(pool_id)
        try:
            existing = self._get(self._alias_path(pool_id))
            if existing is None and write.etag:
                raise ServiceError(404, f"Pool {pool_id} no longer exists")
            aliases = self._aliases()
            if existing is not None:
                if write.etag is None:
                    raise ServiceError(409, f"Pool {pool_id} already exists")
                if not is_managed(existing[0].get("properties") or {}):
                    raise ServiceError(
                        409, f"Pool {pool_id} was not created by this app and is read-only"
                    )
            else:
                # The pool ID is the model name clients send; another alias may already route it.
                taken = next(
                    (
                        alias
                        for alias in aliases
                        if str((alias.get("properties") or {}).get("aliasName") or "").casefold()
                        == pool_id.casefold()
                    ),
                    None,
                )
                if taken is not None:
                    raise ServiceError(
                        409,
                        f"Alias {taken.get('name')} already answers to {pool_id}; choose another",
                    )
            index = self._index()
            models = self._member_models([member.backend_id for member in write.members], index)
            check = self._check([member.backend_id for member in write.members], index)
            if not check.ok:
                raise ServiceError(409, f"Pool {pool_id} was not saved: {check.message}")
            by_id = {
                member.backend_id.casefold(): str(model.get("id") or "")
                for member, model in zip(write.members, models, strict=True)
            }
            previous = (
                [
                    str(target.get("resourceId") or "")
                    for target in (existing[0].get("properties") or {}).get("backendModels") or []
                ]
                if existing
                else []
            )
            self._arm.request(
                "PUT",
                self._alias_path(pool_id),
                api_version=self._version,
                json_body={
                    "properties": alias_properties(
                        pool_id,
                        write,
                        by_id,
                        (existing[0].get("properties") or {}) if existing else None,
                    )
                },
                headers=_concurrency(write.etag),
            )
        except ArmError as error:
            raise translate(error) from error
        logger.info("%s wrote pool %s with %d members", actor, pool_id, len(write.members))
        kept = {model_id.casefold() for model_id in by_id.values()}
        self._delete_orphans([item for item in previous if item.casefold() not in kept], actor)
        return self.get_pool(pool_id)

    def _delete_orphans(self, model_ids: list[str], actor: str) -> list[str]:
        """Deletes members this app created once no pool uses them. Failures are logged, never
        raised: the pool is saved. Returns the members it deleted."""
        deleted: list[str] = []
        if not model_ids:
            return deleted
        try:
            index = self._index()
            in_use = set(self._pools_of(self._aliases(), index))
        except ArmError as error:
            logger.warning("Kept removed members; cannot list pools: %s", error.message)
            return deleted
        for model_id in model_ids:
            key = model_id.casefold()
            member = index.member_of.get(key)
            if key in in_use or member is None or is_protected(member, self._protected):
                continue
            model = index.by_member.get(member.casefold()) or {}
            if not (
                is_managed(model.get("properties") or {}) and is_managed(index.provider(model))
            ):
                continue
            try:
                self._delete_model(model_id, None)
            except ArmError as error:
                logger.warning("Could not delete unused member %s: %s", member, error.message)
                continue
            logger.info("%s deleted member %s, which no pool uses any more", actor, member)
            deleted.append(member)
        return deleted

    # -- removing pools ------------------------------------------------------------------

    def pool_removal_plan(self, pool_id: str) -> RemovalPlan:
        pool = self.get_pool(pool_id)
        return self._removal_plan([pool], self.list_pools(), cleanup=False)

    def cleanup_plan(self) -> RemovalPlan:
        """Everything this app created in the gateway: its pools, their members, the test key."""
        pools = self.list_pools()
        owned = [pool for pool in pools if pool.managed and not pool.protected]
        return self._removal_plan(owned, pools, cleanup=True)

    def _test_key(self) -> str | None:
        name = managed_key_name(self._settings.id_prefix)
        try:
            found = self._get(f"/apiKeys/{segment(name)}")
        except ArmError as error:
            logger.info("Cannot read the test key: %s", error.message)
            return None
        return name if found is not None else None

    def _removal_plan(
        self, removed: list[PoolView], pools: list[PoolView], *, cleanup: bool
    ) -> RemovalPlan:
        removed_ids = {pool.id.casefold() for pool in removed}
        blocked = [f"Pool {pool.id} is read-only here" for pool in removed if pool.protected]
        blocked += [
            f"Pool {pool.id} was not created by this app" for pool in removed if not pool.managed
        ]
        kept_members = {
            member.backend_id.casefold()
            for pool in pools
            if pool.id.casefold() not in removed_ids
            for member in pool.members
        }
        removed_members = {
            member.backend_id.casefold() for pool in removed for member in pool.members
        }
        delete_members = [
            view.id
            for view in self.list_backends()
            if view.managed
            and not view.protected
            and view.id.casefold() not in kept_members
            and (cleanup or view.id.casefold() in removed_members)
        ]
        key = self._test_key() if cleanup else None
        return RemovalPlan(
            pools=[pool.id for pool in removed],
            delete_members=delete_members,
            delete_keys=[key] if key else [],
            kept_pools=[pool.id for pool in pools if pool.id.casefold() not in removed_ids]
            if cleanup
            else [],
            blocked=blocked,
        )

    def remove_pool(self, pool_id: str, confirm: RemovalConfirm, actor: str) -> RemovalResult:
        self._require_writable(pool_id)
        return self._remove(self.pool_removal_plan(pool_id), confirm, actor, pool_etag=confirm.etag)

    def cleanup(self, confirm: CleanupConfirm, actor: str) -> RemovalResult:
        """Removes everything this app created in the gateway; the gateway itself stays."""
        name = self._settings.aigateway_name
        if confirm.confirm_name.strip().casefold() != name.casefold():
            raise ServiceError(400, f"Type the gateway name ({name}) to confirm")
        return self._remove(self.cleanup_plan(), confirm, actor)

    def _remove(
        self,
        plan: RemovalPlan,
        confirm: RemovalConfirm,
        actor: str,
        *,
        pool_etag: str | None = None,
    ) -> RemovalResult:
        if plan.blocked:
            raise ServiceError(409, " ".join(plan.blocked))
        seen = {item.casefold() for item in [*confirm.pools, *confirm.delete_keys]}
        changed = [item for item in [*plan.pools, *plan.delete_keys] if item.casefold() not in seen]
        if changed:
            listed = ", ".join(changed)
            raise ServiceError(409, f"Changed since you reviewed it ({listed}); review it again.")
        result = RemovalResult(pools=[], deleted_members=[], deleted_keys=[])
        index = self._index()
        try:
            for pool_id in plan.pools:
                found = self._get(self._alias_path(pool_id))
                if found is None:
                    continue
                # Checked again right before the delete: only aliases this app created.
                if not is_managed(found[0].get("properties") or {}):
                    raise ServiceError(409, f"Pool {pool_id} was not created by this app")
                self._arm.request(
                    "DELETE",
                    self._alias_path(pool_id),
                    api_version=self._version,
                    headers={"If-Match": pool_etag or _etag(found) or "*"},
                    allow_not_found=True,
                )
                logger.info("%s deleted pool %s", actor, pool_id)
                result.pools.append(pool_id)
            for key in plan.delete_keys:
                self._arm.request(
                    "DELETE",
                    f"/apiKeys/{segment(key)}",
                    api_version=self._version,
                    headers={"If-Match": "*"},
                    allow_not_found=True,
                )
                logger.info("%s deleted test key %s", actor, key)
                result.deleted_keys.append(key)
        except (ArmError, ServiceError) as error:
            failure = translate(error) if isinstance(error, ArmError) else error
            done = [
                *(f"deleted pool {item}" for item in result.pools),
                *(f"deleted key {item}" for item in result.deleted_keys),
            ]
            raise ServiceError(
                failure.status,
                f"{failure.message} Already done: {', '.join(done) or 'nothing'}.",
            ) from error
        models = [
            str(index.by_member[member.casefold()].get("id") or "")
            for member in plan.delete_members
            if member.casefold() in index.by_member
        ]
        result.deleted_members = self._delete_orphans(models, actor)
        return result
