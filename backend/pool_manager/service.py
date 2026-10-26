"""Backend pool operations against one APIM service."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from typing import Any

import httpx

from .arm import ArmClient, ArmError, segment
from .config import Settings
from .model_check import FoundryModelCheck
from .models import (
    ApiBindingWrite,
    ApiView,
    BackendView,
    Capabilities,
    CircuitBreaker,
    CleanupConfirm,
    MemberBackendWrite,
    ModelCheck,
    PolicyPreview,
    PolicyView,
    PoolApiCreate,
    PoolView,
    PoolWrite,
    RemovalApi,
    RemovalConfirm,
    RemovalPlan,
    RemovalResult,
    is_valid_resource_id,
)
from .policy import PolicyConflict, apply_binding, read_binding, remove_binding
from .resources import (
    WILDCARD_OPERATION,
    WILDCARD_OPERATION_ID,
    api_pool_id,
    api_properties,
    circuit_breaker_properties,
    credential_named_value_id,
    is_managed,
    is_protected,
    parse_backend,
    parse_pool,
    pool_properties,
    single_backend_properties,
)

logger = logging.getLogger(__name__)

# Audience of the token APIM's managed identity sends to Azure OpenAI / Foundry members.
COGNITIVE_SERVICES_RESOURCE = "https://cognitiveservices.azure.com"

_POLICY_SCAN_WORKERS = 8


class ServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _translate(error: ArmError) -> ServiceError:
    if error.status == 412:
        return ServiceError(409, "The resource changed since it was loaded. Reload and retry.")
    if error.status in {401, 403}:
        return ServiceError(
            502, f"The app identity is not authorized for this APIM operation: {error.message}"
        )
    if error.status in {400, 404, 409}:
        return ServiceError(error.status, error.message)
    return ServiceError(502, f"APIM returned {error.status} {error.code}: {error.message}")


def policy_response_value(response: httpx.Response) -> str:
    """APIM returns the policy as raw XML
    (sometimes entity-escaped) or, depending on the endpoint, inside a JSON envelope."""
    if "json" in response.headers.get("content-type", "").casefold():
        value = (response.json().get("properties") or {}).get("value")
    else:
        value = response.text
        if value.lstrip().startswith("&lt;"):
            value = unescape(value)
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(502, "APIM returned an API policy without an XML value")
    return value


def _api_path(api: Mapping[str, Any]) -> str:
    return str((api.get("properties") or {}).get("path") or "").strip("/")


def _default_api_id(pool_id: str, path: str, apis: list[dict[str, Any]]) -> str:
    """The pool ID for a pool's first API (or a retry of it); ``<pool>-<path>`` for further ones."""
    same_name = next(
        (api for api in apis if str(api.get("name") or "").casefold() == pool_id.casefold()), None
    )
    if same_name is None or _api_path(same_name).casefold() == path.casefold():
        return pool_id
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9-]+", "-", f"{pool_id}-{path}")).strip("-")
    if len(slug) <= 80:
        return slug
    digest = hashlib.sha256(f"{pool_id}/{path}".encode()).hexdigest()[:8]
    return f"{slug[:71].rstrip('-')}-{digest}"


def _concurrency_headers(etag: str | None) -> dict[str, str]:
    return {"If-Match": etag} if etag else {"If-None-Match": "*"}


class PoolManagerService:
    def __init__(self, settings: Settings, arm: ArmClient) -> None:
        self._settings = settings
        self._arm = arm
        self._protected = settings.protected_prefix_list
        self._service: dict[str, Any] | None = None
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

    def _backend_path(self, backend_id: str) -> str:
        return f"/backends/{segment(backend_id)}"

    def _get_backend(self, backend_id: str) -> tuple[dict[str, Any], str | None] | None:
        # The pool API version is a superset; it also returns ``pool.sessionAffinity``.
        return self._arm.get_json(
            self._backend_path(backend_id), api_version=self._settings.pool_api_version
        )

    def _all_backends(self) -> list[tuple[dict[str, Any], str | None]]:
        return [
            (item, None)
            for item in self._arm.list("/backends", api_version=self._settings.pool_api_version)
        ]

    # -- capabilities ------------------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return Capabilities(
            apim_name=self._settings.apim_name,
            resource_group=self._settings.resource_group,
            session_affinity_supported=self._settings.session_affinity_supported,
            id_prefix=self._settings.id_prefix,
            protected_prefixes=self._protected,
            apim_resource_id=self._arm.resource_id,
            apim_principal_id=self._service_value("identity", "principalId"),
            gateway_url=self._service_value("properties", "gatewayUrl"),
            discovery_subscriptions=self._settings.discovery_subscription_list,
        )

    def _service_value(self, section: str, key: str) -> str | None:
        """A field of the APIM service resource. It is cached once the service has a managed
        identity; until then it is re-read, so enabling the identity shows up without a restart."""
        if self._service is None or not (self._service.get("identity") or {}).get("principalId"):
            try:
                found = self._arm.get_json("", api_version=self._settings.api_version)
            except ArmError as error:
                logger.warning("Cannot read the APIM service: %s", error.message)
                return None
            self._service = found[0] if found else {}
        return str((self._service.get(section) or {}).get(key) or "") or None

    def service_plan(self) -> tuple[str, int, str]:
        """The service's tier, units and region, e.g. ("BasicV2", 1, "East US 2")."""
        if self._service is None:
            self._service_value("sku", "name")  # Reads and caches the service resource.
        service = self._service or {}
        sku = service.get("sku") or {}
        return (
            str(sku.get("name") or ""),
            int(sku.get("capacity") or 1),
            str(service.get("location") or ""),
        )

    def apim_identity(self) -> dict[str, Any]:
        """The APIM service's ``identity`` block and provisioning state, read fresh."""
        found = self._arm.get_json("", api_version=self._settings.api_version)
        self._service = found[0] if found else {}
        identity = dict(self._service.get("identity") or {})
        identity["provisioningState"] = (self._service.get("properties") or {}).get(
            "provisioningState"
        )
        return identity

    def forget_service(self) -> None:
        """Re-read the APIM service next time, e.g. after its identity was enabled."""
        self._service = None

    def foundry_accounts(self) -> list[str]:
        """Foundry accounts behind members that authenticate with the APIM managed identity;
        that identity needs a data-plane role on each of them."""
        accounts: dict[str, str] = {}
        for view in self.list_backends():
            if view.type == "Single" and view.auth_type == "managed_identity":
                account = self._models.account_for_url(view.url)
                if account:
                    accounts.setdefault(account.casefold(), account)
        return sorted(accounts.values(), key=str.casefold)

    # -- member backends ---------------------------------------------------------------

    def list_backends(self) -> list[BackendView]:
        try:
            views = [
                parse_backend(body, etag, self._protected) for body, etag in self._all_backends()
            ]
        except ArmError as error:
            raise _translate(error) from error
        return sorted((view for view in views if view.type == "Single"), key=lambda v: v.id)

    def get_backend(self, backend_id: str) -> BackendView:
        self._require_id(backend_id)
        try:
            found = self._get_backend(backend_id)
        except ArmError as error:
            raise _translate(error) from error
        if found is None or (found[0].get("properties") or {}).get("type") == "Pool":
            raise ServiceError(404, f"Backend {backend_id} was not found")
        return parse_backend(found[0], found[1], self._protected)

    def put_backend(self, backend_id: str, write: MemberBackendWrite, actor: str) -> BackendView:
        self._require_writable(backend_id)
        try:
            existing = self._get_backend(backend_id)
            if existing is None and write.etag:
                raise ServiceError(404, f"Backend {backend_id} no longer exists")
            if existing is not None:
                properties = existing[0].get("properties") or {}
                if write.etag is None:
                    raise ServiceError(409, f"Backend {backend_id} already exists")
                if properties.get("type") == "Pool" or not is_managed(properties):
                    raise ServiceError(
                        409, f"Backend {backend_id} was not created by this app and is read-only"
                    )
            previous = (
                parse_backend(existing[0], existing[1], self._protected) if existing else None
            )

            named_value: str | None = None
            if write.auth.type in {"api_key", "bearer"}:
                if write.auth.secret is not None:
                    named_value = credential_named_value_id(backend_id)
                    self._put_named_value(named_value, write)
                elif previous is not None and previous.named_value:
                    named_value = previous.named_value
                else:
                    raise ServiceError(400, "A credential secret is required")

            response = self._arm.request(
                "PUT",
                self._backend_path(backend_id),
                api_version=self._settings.api_version,
                json_body={"properties": single_backend_properties(write, backend_id, named_value)},
                headers=_concurrency_headers(write.etag),
            )
            self._arm.wait(response)
            if (
                previous is not None
                and previous.named_value
                and previous.named_value != named_value
            ):
                self._delete_owned_named_value(backend_id, previous.named_value)
        except ArmError as error:
            raise _translate(error) from error
        logger.info("%s wrote member backend %s", actor, backend_id)
        return self.get_backend(backend_id)

    def _put_named_value(self, named_value: str, write: MemberBackendWrite) -> None:
        secret = write.auth.secret
        assert secret is not None
        properties: dict[str, Any] = {
            "displayName": named_value,
            "secret": True,
            "tags": ["apim-backend-pool"],
        }
        if secret.value is not None:
            properties["value"] = secret.value.get_secret_value()
        else:
            key_vault: dict[str, str] = {"secretIdentifier": str(secret.key_vault_secret_id)}
            if secret.key_vault_identity_client_id:
                key_vault["identityClientId"] = secret.key_vault_identity_client_id
            properties["keyVault"] = key_vault
        response = self._arm.request(
            "PUT",
            f"/namedValues/{segment(named_value)}",
            api_version=self._settings.api_version,
            json_body={"properties": properties},
        )
        self._arm.wait(response)

    def _delete_owned_named_value(self, backend_id: str, named_value: str) -> None:
        # Only the named value this app derived for the backend; never one shared by others.
        if named_value != credential_named_value_id(backend_id):
            return
        self._arm.request(
            "DELETE",
            f"/namedValues/{segment(named_value)}",
            api_version=self._settings.api_version,
            headers={"If-Match": "*"},
            allow_not_found=True,
        )

    def delete_backend(self, backend_id: str, etag: str | None, actor: str) -> None:
        self._require_writable(backend_id)
        backend = self.get_backend(backend_id)
        if not backend.managed:
            raise ServiceError(409, f"Backend {backend_id} was not created by this app")
        referencing = [
            pool.id
            for pool in self.list_pools()
            if any(m.backend_id.casefold() == backend_id.casefold() for m in pool.members)
        ]
        if referencing:
            raise ServiceError(409, f"Backend is a member of pool(s): {', '.join(referencing)}")
        try:
            self._arm.request(
                "DELETE",
                self._backend_path(backend_id),
                api_version=self._settings.api_version,
                headers={"If-Match": etag or "*"},
            )
            if backend.named_value:
                self._delete_owned_named_value(backend_id, backend.named_value)
        except ArmError as error:
            raise _translate(error) from error
        logger.info("%s deleted member backend %s", actor, backend_id)

    # -- pools -------------------------------------------------------------------------

    def list_pools(self) -> list[PoolView]:
        try:
            pools = [
                parse_pool(body, etag, self._protected)
                for body, etag in self._all_backends()
                if (body.get("properties") or {}).get("type") == "Pool"
            ]
        except ArmError as error:
            raise _translate(error) from error
        return sorted(pools, key=lambda pool: pool.id)

    def get_pool(self, pool_id: str) -> PoolView:
        self._require_id(pool_id)
        try:
            found = self._get_backend(pool_id)
        except ArmError as error:
            raise _translate(error) from error
        if found is None or (found[0].get("properties") or {}).get("type") != "Pool":
            raise ServiceError(404, f"Pool {pool_id} was not found")
        return parse_pool(found[0], found[1], self._protected)

    def _single_members(self, backend_ids: list[str]) -> list[BackendView]:
        singles = {view.id.casefold(): view for view in self.list_backends()}
        views = []
        for backend_id in backend_ids:
            view = singles.get(backend_id.casefold())
            if view is None:
                raise ServiceError(400, f"Member {backend_id} is not a Single backend in this APIM")
            views.append(view)
        return views

    def check_models(self, backend_ids: list[str]) -> ModelCheck:
        """The model check ``put_pool`` enforces, without writing anything."""
        views = self._single_members(backend_ids)
        return self._models.check([(view.id, view.url) for view in views])

    def put_pool(self, pool_id: str, write: PoolWrite, actor: str) -> PoolView:
        self._require_writable(pool_id)
        if write.session_affinity and not self._settings.session_affinity_supported:
            raise ServiceError(
                400,
                "Session affinity needs a preview pool API version (POOL_MANAGER_POOL_API_VERSION)",
            )
        try:
            existing = self._get_backend(pool_id)
            if existing is None and write.etag:
                raise ServiceError(404, f"Pool {pool_id} no longer exists")
            if existing is not None:
                if write.etag is None:
                    raise ServiceError(409, f"Backend {pool_id} already exists")
                if (existing[0].get("properties") or {}).get("type") != "Pool":
                    raise ServiceError(409, f"Backend {pool_id} is not a pool")

            views = self._single_members([member.backend_id for member in write.members])
            check = self._models.check([(view.id, view.url) for view in views])
            if not check.ok:
                raise ServiceError(409, f"Pool {pool_id} was not saved: {check.message}")
            previous = (
                [member.backend_id for member in parse_pool(*existing, self._protected).members]
                if existing
                else []
            )
            if write.circuit_breaker is not None:
                self._apply_breaker(views, write.circuit_breaker, actor)
            canonical = [view.id for view in views]
            normalized = write.model_copy(
                update={
                    "members": [
                        member.model_copy(update={"backend_id": backend_id})
                        for member, backend_id in zip(write.members, canonical, strict=True)
                    ]
                }
            )
            properties = pool_properties(
                normalized,
                pool_id,
                self._arm.resource_id,
                existing[0].get("properties") if existing else None,
            )
            self._arm.request(
                "PUT",
                self._backend_path(pool_id),
                api_version=self._settings.pool_api_version,
                json_body={"properties": properties},
                headers=_concurrency_headers(write.etag),
            )
        except ArmError as error:
            raise _translate(error) from error
        logger.info(
            "%s wrote pool %s with %d members (affinity=%s)",
            actor,
            pool_id,
            len(write.members),
            write.session_affinity,
        )
        kept = {backend_id.casefold() for backend_id in canonical}
        self._delete_orphans([b for b in previous if b.casefold() not in kept], actor)
        return self.get_pool(pool_id)

    def _apply_breaker(
        self, members: list[BackendView], breaker: CircuitBreaker, actor: str
    ) -> None:
        """Writes the pool's breaker to the members this app manages whose rule differs.
        Backends the app doesn't own keep theirs: they are read-only here."""
        for view in members:
            if not view.managed or view.protected or view.circuit_breaker == breaker:
                continue
            found = self._get_backend(view.id)
            if found is None:
                continue
            body, etag = found
            properties = {
                **(body.get("properties") or {}),
                "circuitBreaker": circuit_breaker_properties(breaker),
            }
            response = self._arm.request(
                "PUT",
                self._backend_path(view.id),
                api_version=self._settings.api_version,
                json_body={"properties": properties},
                headers=_concurrency_headers(etag),
            )
            self._arm.wait(response)
            logger.info("%s set the pool circuit breaker on %s", actor, view.id)

    def _delete_orphans(self, backend_ids: list[str], actor: str) -> list[str]:
        """Deletes members this app created once no pool uses them, so removing a member or a
        pool leaves nothing behind. Failures are logged, never raised: the pool is saved.
        Returns the members it deleted."""
        deleted: list[str] = []
        if not backend_ids:
            return deleted
        try:
            in_use = {
                member.backend_id.casefold()
                for pool in self.list_pools()
                for member in pool.members
            }
        except ServiceError as error:
            logger.warning("Kept removed members; cannot list pools: %s", error.message)
            return deleted
        for backend_id in backend_ids:
            if backend_id.casefold() in in_use or is_protected(backend_id, self._protected):
                continue
            try:
                found = self._get_backend(backend_id)
                if found is None:
                    continue
                body, etag = found
                properties = body.get("properties") or {}
                if properties.get("type") == "Pool" or not is_managed(properties):
                    continue
                backend = parse_backend(body, etag, self._protected)
                self._arm.request(
                    "DELETE",
                    self._backend_path(backend_id),
                    api_version=self._settings.api_version,
                    headers={"If-Match": etag or "*"},
                    allow_not_found=True,
                )
                if backend.named_value:
                    self._delete_owned_named_value(backend_id, backend.named_value)
            except ArmError as error:
                logger.warning("Could not delete unused member %s: %s", backend_id, error.message)
                continue
            logger.info("%s deleted member %s, which no pool uses any more", actor, backend_id)
            deleted.append(backend_id)
        return deleted

    def delete_pool(self, pool_id: str, etag: str | None, actor: str) -> None:
        self._require_writable(pool_id)
        pool = self.get_pool(pool_id)
        bound = [
            api.display_name
            for api in self.list_apis()
            if api.binding and api.binding.pool_id.casefold() == pool_id.casefold()
        ]
        if bound:
            raise ServiceError(409, f"Pool is bound to API(s): {', '.join(bound)}. Unbind first.")
        try:
            self._delete_pool_resource(pool_id, etag)
        except ArmError as error:
            raise _translate(error) from error
        logger.info("%s deleted pool %s", actor, pool_id)
        self._delete_orphans([member.backend_id for member in pool.members], actor)

    def _delete_pool_resource(self, pool_id: str, etag: str | None) -> None:
        self._arm.request(
            "DELETE",
            self._backend_path(pool_id),
            api_version=self._settings.pool_api_version,
            headers={"If-Match": etag or "*"},
        )

    # -- removing pools with their APIs ------------------------------------------------

    def pool_removal_plan(self, pool_id: str) -> RemovalPlan:
        """What removing one pool would delete and unbind, without changing anything."""
        pool = self.get_pool(pool_id)
        return self._removal_plan([pool], self.list_pools(), cleanup=False)

    def cleanup_plan(self) -> RemovalPlan:
        """Everything this app created in the service: its pools, their members and APIs."""
        pools = self.list_pools()
        owned = [pool for pool in pools if pool.managed and not pool.protected]
        return self._removal_plan(owned, pools, cleanup=True)

    def _removal_plan(
        self, removed: list[PoolView], pools: list[PoolView], *, cleanup: bool
    ) -> RemovalPlan:
        removed_ids = {pool.id.casefold() for pool in removed}
        blocked = [f"Pool {pool.id} is read-only here" for pool in removed if pool.protected]
        delete_apis: list[RemovalApi] = []
        unbind_apis: list[RemovalApi] = []
        for raw, view in self._api_entries():
            bound_to = view.binding.pool_id if view.binding else None
            ours = bound_to is not None and bound_to.casefold() in removed_ids
            # An API this app created for a removed pool that nothing is bound to any more is
            # left over from an earlier unbind; cleanup takes every such API.
            created_for = (api_pool_id(raw.get("properties") or {}) or "").casefold()
            leftover = bound_to is None and (cleanup or created_for in removed_ids)
            if view.managed and (ours or leftover):
                target = delete_apis
            elif ours:
                target = unbind_apis
            else:
                continue
            if is_protected(view.id, self._protected):
                if ours:
                    blocked.append(f"API {view.id} uses pool {bound_to} but is read-only here")
                continue
            target.append(
                RemovalApi(
                    id=view.id, display_name=view.display_name, path=view.path, pool_id=bound_to
                )
            )
        # Members only a removed pool uses; cleanup also takes members no pool uses at all.
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
        kept_pools = [pool.id for pool in pools if pool.id.casefold() not in removed_ids]
        return RemovalPlan(
            pools=[pool.id for pool in removed],
            delete_apis=delete_apis,
            unbind_apis=unbind_apis,
            delete_members=delete_members,
            kept_pools=kept_pools if cleanup else [],
            blocked=blocked,
        )

    def remove_pool(self, pool_id: str, confirm: RemovalConfirm, actor: str) -> RemovalResult:
        """Unbinds the pool's other APIs, deletes the APIs this app created for it, the pool,
        and the members no other pool uses."""
        self._require_writable(pool_id)
        plan = self.pool_removal_plan(pool_id)
        return self._remove(plan, confirm, actor, pool_etag=confirm.etag)

    def cleanup(self, confirm: CleanupConfirm, actor: str) -> RemovalResult:
        """Removes everything this app created in the service; the service itself stays."""
        name = self._settings.apim_name
        if confirm.confirm_name.strip().casefold() != name.casefold():
            raise ServiceError(400, f"Type the service name ({name}) to confirm")
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

        def unseen(planned: list[str], confirmed: list[str]) -> list[str]:
            seen = {item.casefold() for item in confirmed}
            return [item for item in planned if item.casefold() not in seen]

        changed = [
            *unseen(plan.pools, confirm.pools),
            *unseen([api.id for api in plan.delete_apis], confirm.delete_apis),
            *unseen([api.id for api in plan.unbind_apis], confirm.unbind_apis),
        ]
        if changed:
            listed = ", ".join(changed)
            raise ServiceError(409, f"Changed since you reviewed it ({listed}); review it again.")
        result = RemovalResult(pools=[], deleted_apis=[], unbound_apis=[], deleted_members=[])
        try:
            # Unbind first, so no API is left pointing at a pool that no longer exists.
            for api in plan.unbind_apis:
                self.delete_binding(api.id, None, actor)
                result.unbound_apis.append(api.id)
            for api in plan.delete_apis:
                self._delete_api(api.id, actor)
                result.deleted_apis.append(api.id)
            for pool_id in plan.pools:
                self._delete_pool_resource(pool_id, pool_etag)
                logger.info("%s deleted pool %s", actor, pool_id)
                result.pools.append(pool_id)
        except (ArmError, ServiceError) as error:
            failure = _translate(error) if isinstance(error, ArmError) else error
            done = [
                *(f"unbound {item}" for item in result.unbound_apis),
                *(f"deleted API {item}" for item in result.deleted_apis),
                *(f"deleted pool {item}" for item in result.pools),
            ]
            raise ServiceError(
                failure.status,
                f"{failure.message} Already done: {', '.join(done) or 'nothing'}.",
            ) from error
        result.deleted_members = self._delete_orphans(plan.delete_members, actor)
        return result

    def _delete_api(self, api_id: str, actor: str) -> None:
        path = f"/apis/{segment(api_id)}"
        found = self._arm.get_json(path, api_version=self._settings.api_version)
        if found is None:
            return
        body, etag = found
        # Checked again right before the delete: only APIs this app created are ever deleted.
        if not is_managed(body.get("properties") or {}):
            raise ServiceError(409, f"API {api_id} was not created by this app")
        self._arm.request(
            "DELETE",
            path,
            api_version=self._settings.api_version,
            params={"deleteRevisions": "true"},
            headers={"If-Match": etag or "*"},
            allow_not_found=True,
        )
        logger.info("%s deleted API %s", actor, api_id)

    # -- API bindings ------------------------------------------------------------------

    def _policy_path(self, api_id: str) -> str:
        return f"/apis/{segment(api_id)}/policies/policy"

    def _get_policy(self, api_id: str) -> tuple[str | None, str | None]:
        response = self._arm.request(
            "GET",
            self._policy_path(api_id),
            api_version=self._settings.api_version,
            params={"format": "xml"},
            allow_not_found=True,
        )
        if response.status_code == 404:
            return None, None
        return policy_response_value(response), response.headers.get("ETag")

    def _api_view(self, api: Mapping[str, Any]) -> ApiView:
        api_id = str(api.get("name") or "")
        properties = api.get("properties") or {}
        xml, etag = self._get_policy(api_id)
        binding, foreign = read_binding(xml) if xml else (None, False)
        return ApiView(
            id=api_id,
            display_name=str(properties.get("displayName") or api_id),
            path=str(properties.get("path") or ""),
            binding=binding,
            foreign_backend_selection=foreign,
            managed=is_managed(properties),
            subscription_required=properties.get("subscriptionRequired", True) is not False,
            subscription_key_header=str(
                (properties.get("subscriptionKeyParameterNames") or {}).get("header")
                or "Ocp-Apim-Subscription-Key"
            ),
            etag=etag,
        )

    def _api_entries(self) -> list[tuple[dict[str, Any], ApiView]]:
        """Every current API as ARM returns it, with its view (which reads its policy)."""
        try:
            apis = [
                api
                for api in self._arm.list("/apis", api_version=self._settings.api_version)
                # Non-current revisions are listed as ``api;rev=n`` and omit ``isCurrent`` rather
                # than setting it false; bind only the current one.
                if ";rev=" not in str(api.get("name") or "")
            ]
            with ThreadPoolExecutor(max_workers=_POLICY_SCAN_WORKERS) as executor:
                views = list(executor.map(self._api_view, apis))
        except ArmError as error:
            raise _translate(error) from error
        return list(zip(apis, views, strict=True))

    def list_apis(self) -> list[ApiView]:
        views = [view for _, view in self._api_entries()]
        return sorted(views, key=lambda view: view.display_name.casefold())

    def get_api(self, api_id: str) -> ApiView:
        self._require_id(api_id)
        try:
            found = self._arm.get_json(
                f"/apis/{segment(api_id)}", api_version=self._settings.api_version
            )
            if found is None:
                raise ServiceError(404, f"API {api_id} was not found")
            return self._api_view(found[0])
        except ArmError as error:
            raise _translate(error) from error

    def get_policy(self, api_id: str) -> PolicyView:
        self._require_id(api_id)
        try:
            xml, etag = self._get_policy(api_id)
        except ArmError as error:
            raise _translate(error) from error
        return PolicyView(api_id=api_id, xml=xml, etag=etag)

    def _proposed_binding(
        self, api_id: str, write: ApiBindingWrite
    ) -> tuple[PoolView, str | None, str, str | None]:
        """Returns the pool, the current policy, the policy binding would write, and its ETag."""
        self._require_id(api_id)
        pool = self.get_pool(write.pool_id)
        try:
            xml, current_etag = self._get_policy(api_id)
            if write.etag and current_etag and write.etag != current_etag:
                raise ServiceError(
                    409, "The API policy changed since it was loaded. Reload and retry."
                )
            updated = apply_binding(xml, write.model_copy(update={"pool_id": pool.id}))
        except PolicyConflict as error:
            raise ServiceError(409, str(error)) from error
        except ArmError as error:
            raise _translate(error) from error
        return pool, xml, updated, current_etag

    def preview_binding(self, api_id: str, write: ApiBindingWrite) -> PolicyPreview:
        _, xml, updated, _ = self._proposed_binding(api_id, write)
        return PolicyPreview(current=xml, proposed=updated)

    def put_binding(self, api_id: str, write: ApiBindingWrite, actor: str) -> ApiView:
        pool, _, updated, current_etag = self._proposed_binding(api_id, write)
        try:
            self._put_policy(api_id, updated, current_etag)
            api = self._arm.get_json(
                f"/apis/{segment(api_id)}", api_version=self._settings.api_version
            )
        except ArmError as error:
            raise _translate(error) from error
        if api is None:
            raise ServiceError(404, f"API {api_id} was not found")
        logger.info("%s bound API %s to pool %s", actor, api_id, pool.id)
        return self._api_view(api[0])

    def create_pool_api(self, pool_id: str, write: PoolApiCreate, actor: str) -> ApiView:
        """Creates an HTTP API for the pool and binds it with the default resilience settings.

        Retrying is safe: an API this app already created for the pool at the same path is
        completed rather than refused.
        """
        pool = self.get_pool(pool_id)
        self._require_writable(pool.id)
        path = write.path
        try:
            apis = [
                api
                for api in self._arm.list("/apis", api_version=self._settings.api_version)
                if ";rev=" not in str(api.get("name") or "")
            ]
            api_id = write.api_id or _default_api_id(pool.id, path, apis)
            self._require_writable(api_id)
            existing = next(
                (api for api in apis if str(api.get("name") or "").casefold() == api_id.casefold()),
                None,
            )
            taken = next(
                (
                    api
                    for api in apis
                    if api is not existing and _api_path(api).casefold() == path.casefold()
                ),
                None,
            )
            if taken is not None:
                raise ServiceError(
                    409, f"Path /{path} is already used by API {taken.get('name')}; choose another"
                )
            if existing is None:
                name = pool.title or pool.id
                # A pool's further APIs say which path they serve, so APIM lists them apart.
                default_name = (
                    name if api_id.casefold() == pool.id.casefold() else f"{name} (/{path})"
                )
                display_name = write.display_name or default_name
                response = self._arm.request(
                    "PUT",
                    f"/apis/{segment(api_id)}",
                    api_version=self._settings.api_version,
                    json_body={"properties": api_properties(display_name, path, pool.id)},
                    headers={"If-None-Match": "*"},
                )
                self._arm.wait(response)
            else:
                view = self._api_view(existing)
                other_pool = (
                    view.binding is not None
                    and view.binding.pool_id.casefold() != pool.id.casefold()
                )
                same_path = _api_path(existing).casefold() == path.casefold()
                if not view.managed or not same_path or other_pool:
                    raise ServiceError(
                        409, f"API {api_id} already exists; bind it, or choose another API ID"
                    )
            self._ensure_wildcard_operation(api_id)
        except ArmError as error:
            raise _translate(error) from error
        # Members imported from Foundry authenticate as the APIM managed identity.
        backends = {view.id.casefold(): view for view in self.list_backends()}
        uses_identity = any(
            (backend := backends.get(member.backend_id.casefold())) is not None
            and backend.auth_type == "managed_identity"
            for member in pool.members
        )
        logger.info("%s created API %s (/%s) for pool %s", actor, api_id, path, pool.id)
        return self.put_binding(
            api_id,
            ApiBindingWrite(
                pool_id=pool.id,
                managed_identity_resource=COGNITIVE_SERVICES_RESOURCE if uses_identity else None,
            ),
            actor,
        )

    def _ensure_wildcard_operation(self, api_id: str) -> None:
        try:
            self._arm.request(
                "PUT",
                f"/apis/{segment(api_id)}/operations/{WILDCARD_OPERATION_ID}",
                api_version=self._settings.api_version,
                json_body={"properties": WILDCARD_OPERATION},
                headers={"If-None-Match": "*"},
            )
        except ArmError as error:
            if error.status != 412:  # It already exists from an earlier attempt.
                raise

    def delete_binding(self, api_id: str, etag: str | None, actor: str) -> None:
        self._require_id(api_id)
        try:
            xml, current_etag = self._get_policy(api_id)
            if xml is None or read_binding(xml)[0] is None:
                raise ServiceError(404, "The API is not bound to a pool by this app")
            if etag and current_etag and etag != current_etag:
                raise ServiceError(
                    409, "The API policy changed since it was loaded. Reload and retry."
                )
            self._put_policy(api_id, remove_binding(xml), current_etag)
        except PolicyConflict as error:
            raise ServiceError(409, str(error)) from error
        except ArmError as error:
            raise _translate(error) from error
        logger.info("%s unbound API %s", actor, api_id)

    def _put_policy(self, api_id: str, xml: str, etag: str | None) -> None:
        self._arm.request(
            "PUT",
            self._policy_path(api_id),
            api_version=self._settings.api_version,
            json_body={"properties": {"format": "xml", "value": xml}},
            headers={"If-Match": etag or "*"},
        )
