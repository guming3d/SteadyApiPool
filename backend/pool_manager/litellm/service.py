"""Pool operations against one LiteLLM gateway.

A pool is a LiteLLM model group: one row per member, all named after the pool. Saving a pool
writes its rows (new ones first, then changes, then removals, so the pool never runs empty);
LiteLLM has no ETags, so the pool's ETag is a hash of its rows and a save re-reads and compares
it first. Rows this app didn't write are shown but never changed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any

from ..arm import ArmClient, ArmError
from ..config import Settings
from ..discovery import KnownMember
from ..errors import ServiceError
from ..model_check import FoundryModelCheck
from ..models import ModelCheck, is_valid_resource_id
from .aks_deploy import CONTAINER_SERVICE_API_VERSION
from .client import LiteLLMClient, LiteLLMError
from .gateway import GatewayConnection, GatewayInfo, GatewayUnavailable
from .gateway_template import SIZING_TAG, parse_sizing_tag
from .models import (
    AliasView,
    BackendView,
    Capabilities,
    CleanupConfirm,
    ConfigPreview,
    ConfigView,
    MemberAuth,
    MemberBackendWrite,
    PoolEndpoint,
    PoolView,
    PoolWrite,
    RemovalAlias,
    RemovalConfirm,
    RemovalKey,
    RemovalPlan,
    RemovalResult,
)
from .resources import (
    Member,
    config_yaml,
    credential_name,
    is_managed,
    is_protected,
    member_target,
    parse_pool,
    pool_row,
    pool_settings,
    row_member,
    row_metadata,
    row_model_id,
    row_name,
    with_base_model,
)

logger = logging.getLogger(__name__)

# Every gateway replica reloads models from the database on its own schedule
# (proxy_config_reload_interval_seconds: 5 s on gateways this app deploys, 30 s by default), so a
# read just after a save can reach one that hasn't yet. A save reads until this many reads in a
# row show it, for at most SETTLE_SECONDS (one default interval and a margin).
SETTLE_READS = 3
SETTLE_SECONDS = 45.0


def translate_error(error: LiteLLMError | GatewayUnavailable) -> ServiceError:
    if isinstance(error, GatewayUnavailable):
        return ServiceError(error.status, error.message)
    if error.status in {401, 403}:
        return ServiceError(502, f"The gateway refused the master key: {error.message}")
    if error.status in {400, 404, 409, 422}:
        return ServiceError(400 if error.status == 422 else error.status, error.message)
    return ServiceError(502, f"The gateway returned {error.status}: {error.message}")


def test_key_alias(prefix: str, pool_id: str) -> str:
    return f"{prefix}test-{pool_id}"


def alias_target(value: object) -> str | None:
    """The model group an alias points at: ``"pool"`` or ``{"model": "pool", ...}``."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("model"), str):
        return str(value["model"])
    return None


class LiteLLMService:
    def __init__(
        self,
        settings: Settings,
        arm: ArmClient,
        gateway: GatewayConnection,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._clock = clock
        self._arm = arm
        self.gateway = gateway
        self._protected = settings.protected_prefix_list
        self._models = FoundryModelCheck(settings, arm)
        # Members added (e.g. imported from Foundry) that no saved pool uses yet. In memory
        # only: an api_key member's key lives here until a pool save stores it in LiteLLM.
        self._pending: dict[str, Member] = {}
        self._pending_lock = Lock()

    # -- helpers -----------------------------------------------------------------------

    @property
    def arm(self) -> ArmClient:
        """The operator's ARM client (e.g. for reading an AKS gateway's cluster)."""
        return self._arm

    def client(self) -> LiteLLMClient:
        """The gateway's admin client; ``ServiceError`` while it can't be reached."""
        try:
            return self.gateway.client()
        except GatewayUnavailable as error:
            raise translate_error(error) from error

    def _rows(self) -> list[dict[str, Any]]:
        try:
            return self.client().models()
        except LiteLLMError as error:
            raise translate_error(error) from error

    def _require_id(self, resource_id: str) -> None:
        if not is_valid_resource_id(resource_id):
            raise ServiceError(400, "IDs use letters, digits and hyphens (max 80 characters)")

    def _require_writable(self, resource_id: str) -> None:
        self._require_id(resource_id)
        if is_protected(resource_id, self._protected):
            raise ServiceError(
                403, f"{resource_id} is owned by another control plane and is read-only here"
            )

    @staticmethod
    def _groups(rows: list[dict[str, Any]]) -> dict[str, tuple[str, list[dict[str, Any]]]]:
        """Rows by lower-case model group, with the group's own spelling."""
        groups: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        for row in rows:
            name = row_name(row)
            if name:
                groups.setdefault(name.casefold(), (name, []))[1].append(row)
        return groups

    def _aliases(self) -> dict[str, str]:
        """Every alias on the gateway, mapped to the model group it points at."""
        try:
            raw = self.client().router_settings().get("model_group_alias")
        except LiteLLMError as error:
            raise translate_error(error) from error
        if not isinstance(raw, Mapping):
            return {}
        return {str(name): target for name, value in raw.items() if (target := alias_target(value))}

    # -- capabilities ------------------------------------------------------------------

    def capabilities(self) -> Capabilities:
        try:
            info = self.gateway.info()
        except GatewayUnavailable as error:
            raise translate_error(error) from error
        return Capabilities(
            gateway_name=info.name,
            gateway_kind="external" if self._settings.external else "aca",
            resource_group=self._settings.resource_group or None,
            id_prefix=self._settings.id_prefix,
            protected_prefixes=self._protected,
            gateway_resource_id=info.resource_id,
            gateway_principal_id=info.principal_id,
            gateway_url=info.url,
            litellm_version=self.gateway.version(),
            sized_for=parse_sizing_tag(self._gateway_tags(info).get(SIZING_TAG)),
            app_insights_id=info.app_insights_id,
            discovery_subscriptions=self._settings.discovery_subscription_list,
        )

    def _gateway_tags(self, info: GatewayInfo) -> Mapping[str, Any]:
        """The Container App's tags, or for AKS the cluster's (read with the operator)."""
        if not self._settings.gateway_cluster_id:
            return info.tags
        try:
            cluster = self.arm.get_arm_json(
                self._settings.gateway_cluster_id, api_version=CONTAINER_SERVICE_API_VERSION
            )
        except ArmError as error:
            logger.info("Cannot read the gateway's cluster tags: %s", error.message)
            return {}
        return (cluster or {}).get("tags") or {}

    def foundry_accounts(self) -> list[str]:
        """Foundry accounts behind members the gateway calls with its managed identity; that
        identity needs a data-plane role on each of them."""
        accounts: dict[str, str] = {}
        for view in self.list_backends():
            if view.provider == "azure" and view.auth_type == "managed_identity":
                account = self._models.account_for_url(view.url)
                if account:
                    accounts.setdefault(account.casefold(), account)
        return sorted(accounts.values(), key=str.casefold)

    # -- members -----------------------------------------------------------------------

    def _members(self, rows: list[dict[str, Any]]) -> dict[str, Member]:
        """Members of every pool, then the pending ones no pool uses yet, by lower-case ID."""
        members: dict[str, Member] = {}
        for row in rows:
            member = row_member(row)
            known = members.setdefault(member.id.casefold(), member)
            if row_name(row) not in known.pools:
                known.pools.append(row_name(row))
        with self._pending_lock:
            for key, member in list(self._pending.items()):
                if key in members:
                    # Saved into a pool since; the rows describe it now.
                    if members[key].managed:
                        del self._pending[key]
                    continue
                members[key] = Member(**{**member.__dict__, "pools": []})
        return members

    def list_backends(self) -> list[BackendView]:
        members = self._members(self._rows())
        return sorted(
            (member.view(self._protected) for member in members.values()), key=lambda v: v.id
        )

    def get_backend(self, backend_id: str) -> BackendView:
        self._require_id(backend_id)
        member = self._members(self._rows()).get(backend_id.casefold())
        if member is None:
            raise ServiceError(404, f"Member {backend_id} was not found")
        return member.view(self._protected)

    def known_members(self) -> list[KnownMember]:
        """Members to match deployments against; one no saved pool uses is a pending
        import."""
        return [
            KnownMember(id=view.id, url=view.url, imported=bool(view.pools))
            for view in self.list_backends()
        ]

    def add_deployment_member(self, member_id: str, title: str, url: str, actor: str) -> str:
        write = MemberBackendWrite(
            title=title,
            url=url,  # type: ignore[arg-type]
            auth=MemberAuth(type="managed_identity"),
        )
        return self.put_backend(member_id, write, actor).id

    def put_backend(self, backend_id: str, write: MemberBackendWrite, actor: str) -> BackendView:
        """Adds a member for pools to use, or changes one: every row of it is rewritten."""
        self._require_writable(backend_id)
        try:
            provider, _, model = member_target(str(write.url), write.model)
        except ValueError as error:
            raise ServiceError(400, str(error)) from error
        rows = self._rows()
        current = self._members(rows).get(backend_id.casefold())
        if current is not None and not current.managed:
            raise ServiceError(409, f"Member {backend_id} was not added by this app")
        member = Member(
            id=current.id if current else backend_id,
            title=write.title or (current.title if current else None),
            url=str(write.url).rstrip("/"),
            provider=provider,
            model=model,
            auth=write.auth.type,
            # A pending member's key stays until a pool stores it.
            secret=write.auth.secret or (current.secret if current else None),
        )
        stored = current is not None and current.auth == "api_key" and bool(current.pools)
        if member.auth == "api_key" and member.secret is None and not stored:
            raise ServiceError(400, "An API key is required")
        in_rows = [row for row in rows if row_member(row).id.casefold() == member.id.casefold()]
        if not in_rows:
            with self._pending_lock:
                self._pending[member.id.casefold()] = member
            logger.info("%s added member %s", actor, member.id)
            return member.view(self._protected)
        client = self.client()
        try:
            self._store_credential(client, member)
            for row in in_rows:
                pool_id = row_name(row)
                pool = parse_pool(pool_id, [r for r in rows if row_name(r) == pool_id], [])
                entry = next(m for m in pool.members if m.backend_id == row_member(row).id)
                position = [m.backend_id for m in pool.members].index(entry.backend_id)
                body = pool_row(
                    pool_id,
                    member,
                    priority=entry.priority,
                    weight=entry.weight,
                    position=position,
                    settings=pool_settings(pool),
                )
                client.update_model(row_model_id(row), body)
        except LiteLLMError as error:
            raise translate_error(error) from error
        logger.info("%s changed member %s in %d pool(s)", actor, member.id, len(in_rows))
        return self.get_backend(member.id)

    def delete_backend(self, backend_id: str, actor: str) -> None:
        """Forgets a member no pool uses yet; members in pools go when their pools drop them."""
        self._require_writable(backend_id)
        member = self._members(self._rows()).get(backend_id.casefold())
        if member is None:
            raise ServiceError(404, f"Member {backend_id} was not found")
        if member.pools:
            raise ServiceError(409, f"Member is in pool(s): {', '.join(member.pools)}")
        with self._pending_lock:
            self._pending.pop(backend_id.casefold(), None)
        logger.info("%s removed member %s", actor, backend_id)

    def _store_credential(self, client: LiteLLMClient, member: Member) -> None:
        """Keeps an api_key member's key in a LiteLLM credential its rows refer to."""
        if member.auth != "api_key" or member.secret is None:
            return
        name = credential_name(member.id)
        body = {
            "credential_name": name,
            "credential_values": {"api_key": member.secret.get_secret_value()},
            "credential_info": {"managed_by": "AI Gateway Pool Manager", "member": member.id},
        }
        try:
            client.request("DELETE", f"/credentials/{name}")
        except LiteLLMError as error:
            if error.status not in {400, 404}:
                raise
        client.request("POST", "/credentials", json_body=body)
        member.secret = None

    def _delete_unused_credentials(self, client: LiteLLMClient, member_ids: list[str]) -> None:
        """Deletes the credentials of members no row uses any more. Failures are logged."""
        if not member_ids:
            return
        try:
            in_use = {row_member(row).id.casefold() for row in client.models()}
        except LiteLLMError as error:
            logger.warning("Kept member credentials; cannot list models: %s", error.message)
            return
        for member_id in member_ids:
            if member_id.casefold() in in_use:
                continue
            try:
                client.request("DELETE", f"/credentials/{credential_name(member_id)}")
            except LiteLLMError as error:
                if error.status not in {400, 404}:
                    logger.warning("Could not delete the key of %s: %s", member_id, error.message)

    # -- pools -------------------------------------------------------------------------

    def list_pools(self) -> list[PoolView]:
        groups = self._groups(self._rows())
        return sorted(
            (parse_pool(name, rows, self._protected) for name, rows in groups.values()),
            key=lambda pool: pool.id.casefold(),
        )

    def _pool_rows(
        self, pool_id: str, rows: list[dict[str, Any]] | None = None
    ) -> tuple[str, list[dict[str, Any]]] | None:
        group = self._groups(rows if rows is not None else self._rows()).get(pool_id.casefold())
        return group

    def get_pool(self, pool_id: str) -> PoolView:
        self._require_id(pool_id)
        group = self._pool_rows(pool_id)
        if group is None:
            raise ServiceError(404, f"Pool {pool_id} was not found")
        return parse_pool(group[0], group[1], self._protected)

    def check_models(self, backend_ids: list[str]) -> ModelCheck:
        """The model check ``put_pool`` enforces, without writing anything."""
        members = self._resolve(backend_ids, self._members(self._rows()))
        return self._models.check([(member.id, member.url) for member in members])

    def _resolve(self, backend_ids: list[str], members: dict[str, Member]) -> list[Member]:
        resolved = []
        for backend_id in backend_ids:
            member = members.get(backend_id.casefold())
            if member is None:
                raise ServiceError(
                    400, f"Member {backend_id} is not known here (any more); add it again"
                )
            if not member.managed:
                raise ServiceError(
                    409, f"Member {backend_id} was not added by this app and can't join a pool"
                )
            resolved.append(member)
        return resolved

    def _desired_rows(
        self, pool_id: str, write: PoolWrite, current: PoolView | None, members: list[Member]
    ) -> list[dict[str, Any]]:
        settings = pool_settings(
            current,
            title=write.title if write.title is not None else (current.title if current else None),
            description=(
                write.description
                if write.description is not None
                else (current.description if current else None)
            ),
            breaker=write.circuit_breaker,
            retries=write.retries,
            timeout_seconds=write.timeout_seconds,
        )
        return [
            pool_row(
                pool_id,
                member,
                priority=entry.priority,
                weight=entry.weight,
                position=position,
                settings=settings,
            )
            for position, (entry, member) in enumerate(zip(write.members, members, strict=True))
        ]

    def _prepare(
        self, pool_id: str, write: PoolWrite
    ) -> tuple[str, PoolView | None, list[dict[str, Any]], list[Member], list[dict[str, Any]]]:
        """Checks a save and returns what it would write, without writing."""
        self._require_writable(pool_id)
        rows = self._rows()
        group = self._pool_rows(pool_id, rows)
        current = parse_pool(group[0], group[1], self._protected) if group else None
        if current is None and write.etag:
            raise ServiceError(404, f"Pool {pool_id} no longer exists")
        if current is not None:
            if write.etag is None:
                raise ServiceError(409, f"Pool {pool_id} already exists")
            if not current.managed:
                raise ServiceError(409, f"Pool {pool_id} was not created by this app")
            if write.etag != current.etag:
                raise ServiceError(409, "The pool changed since it was loaded. Reload and retry.")
        name = group[0] if group else pool_id
        if group is None and name.casefold() in {a.casefold() for a in self._aliases()}:
            raise ServiceError(409, f"{name} is already an alias of another pool")
        members = self._resolve([m.backend_id for m in write.members], self._members(rows))
        existing = group[1] if group else []
        return name, current, existing, members, self._desired_rows(name, write, current, members)

    def _with_base_models(
        self, desired: list[dict[str, Any]], check: ModelCheck
    ) -> list[dict[str, Any]]:
        """The rows with the model each Foundry member's deployment serves (``base_model``),
        as the model check read it from the deployment."""
        names = {m.backend_id.casefold(): m.model_name for m in check.members if m.model_name}
        out = []
        for row in desired:
            model_name = names.get(row_member(row).id.casefold())
            out.append(with_base_model(row, model_name) if model_name else row)
        return out

    def preview_pool(self, pool_id: str, write: PoolWrite) -> ConfigPreview:
        name, current, existing, members, desired = self._prepare(pool_id, write)
        check = self._models.check([(member.id, member.url) for member in members])
        desired = self._with_base_models(desired, check)
        aliases = current.aliases if current else []
        return ConfigPreview(
            current=config_yaml(name, existing, aliases) if existing else None,
            proposed=config_yaml(name, desired, aliases),
        )

    def put_pool(self, pool_id: str, write: PoolWrite, actor: str) -> PoolView:
        name, _, existing, members, desired = self._prepare(pool_id, write)
        check = self._models.check([(member.id, member.url) for member in members])
        if not check.ok:
            raise ServiceError(409, f"Pool {name} was not saved: {check.message}")
        desired = self._with_base_models(desired, check)
        client = self.client()
        have = {row_model_id(row).casefold(): row for row in existing}
        want = {str(row["model_info"]["id"]).casefold() for row in desired}
        try:
            for member in members:
                self._store_credential(client, member)
            for row in desired:
                if str(row["model_info"]["id"]).casefold() not in have:
                    client.add_model(row)
            for row in desired:
                old = have.get(str(row["model_info"]["id"]).casefold())
                if old is not None:
                    client.update_model(row_model_id(old), row)
            dropped = [row for key, row in have.items() if key not in want]
            for row in dropped:
                client.delete_model(row_model_id(row))
        except LiteLLMError as error:
            raise translate_error(error) from error
        logger.info("%s wrote pool %s with %d members", actor, name, len(members))
        self._delete_unused_credentials(client, [row_member(row).id for row in dropped])
        return self._settled(name, write)

    def _settled(self, name: str, write: PoolWrite) -> PoolView:
        """The pool as just saved, once SETTLE_READS reads in a row show its members, priorities
        and weights (a replica that hasn't reloaded yet shows the old pool, or none)."""
        wanted = sorted((m.backend_id.casefold(), m.priority, m.weight) for m in write.members)
        deadline = self._clock() + SETTLE_SECONDS
        streak = 0
        while True:
            group = self._pool_rows(name)
            view = parse_pool(group[0], group[1], self._protected) if group else None
            shown = (
                sorted((m.backend_id.casefold(), m.priority, m.weight) for m in view.members)
                if view
                else None
            )
            streak = streak + 1 if shown == wanted else 0
            if view is not None and streak >= SETTLE_READS:
                return view
            if self._clock() >= deadline:
                if view is not None and streak:
                    return view
                raise ServiceError(
                    503,
                    f"Pool {name} was saved, but not every gateway replica has loaded it yet; "
                    "reload in a minute",
                )
            if not streak:
                self._sleep(1.0)

    # -- endpoint and aliases ----------------------------------------------------------

    def endpoint(self, pool_id: str) -> PoolEndpoint:
        pool = self.get_pool(pool_id)
        aliases = self._alias_views(pool)
        try:
            url = self.gateway.info().url
        except GatewayUnavailable as error:
            raise translate_error(error) from error
        return PoolEndpoint(
            pool_id=pool.id,
            base_url=f"{url}/v1" if url else None,
            model_names=[pool.id, *(alias.name for alias in aliases)],
            aliases=aliases,
        )

    def _alias_views(
        self, pool: PoolView, aliases: dict[str, str] | None = None
    ) -> list[AliasView]:
        managed = {name.casefold() for name in pool.aliases}
        found = aliases if aliases is not None else self._aliases()
        return sorted(
            (
                AliasView(name=name, pool_id=pool.id, managed=name.casefold() in managed)
                for name, target in found.items()
                if target.casefold() == pool.id.casefold()
            ),
            key=lambda alias: alias.name.casefold(),
        )

    def list_aliases(self) -> list[AliasView]:
        pools = {pool.id.casefold(): pool for pool in self.list_pools()}
        managed = {
            (alias.casefold(), pool_id) for pool_id, pool in pools.items() for alias in pool.aliases
        }
        return sorted(
            (
                AliasView(
                    name=name,
                    pool_id=pools[target.casefold()].id if target.casefold() in pools else target,
                    managed=(name.casefold(), target.casefold()) in managed,
                )
                for name, target in self._aliases().items()
            ),
            key=lambda alias: alias.name.casefold(),
        )

    def config(self, pool_id: str) -> ConfigView:
        group = self._pool_rows(pool_id)
        if group is None:
            raise ServiceError(404, f"Pool {pool_id} was not found")
        pool = parse_pool(group[0], group[1], self._protected)
        aliases = [alias.name for alias in self._alias_views(pool)]
        return ConfigView(pool_id=pool.id, yaml=config_yaml(pool.id, group[1], aliases))

    def _record_aliases(
        self, pool: PoolView, rows: list[dict[str, Any]], aliases: list[str]
    ) -> None:
        """Stores which aliases this app added in the pool's rows."""
        client = self.client()
        for row in rows:
            if not is_managed(row):
                continue
            metadata = dict(row_metadata(row))
            metadata["pool"] = {**dict(metadata.get("pool") or {}), "aliases": aliases}
            client.update_model(row_model_id(row), {"model_info": {"pool_manager": metadata}})

    def add_alias(self, pool_id: str, name: str, actor: str) -> PoolEndpoint:
        self._require_writable(pool_id)
        rows = self._rows()
        group = self._pool_rows(pool_id, rows)
        if group is None:
            raise ServiceError(404, f"Pool {pool_id} was not found")
        pool = parse_pool(group[0], group[1], self._protected)
        if not pool.managed:
            raise ServiceError(409, f"Pool {pool.id} was not created by this app")
        if name.casefold() in self._groups(rows):
            raise ServiceError(409, f"{name} is already a pool or model on the gateway")
        aliases = self._aliases()
        existing = next((key for key in aliases if key.casefold() == name.casefold()), None)
        if existing is not None and aliases[existing].casefold() != pool.id.casefold():
            raise ServiceError(409, f"{name} already points at {aliases[existing]}")
        try:
            if existing is None:
                self.client().update_router_settings(
                    {"model_group_alias": {**aliases, name: pool.id}}
                )
            if name.casefold() not in {alias.casefold() for alias in pool.aliases}:
                self._record_aliases(pool, group[1], [*pool.aliases, existing or name])
        except LiteLLMError as error:
            raise translate_error(error) from error
        logger.info("%s added alias %s for pool %s", actor, name, pool.id)
        return self.endpoint(pool.id)

    def remove_alias(self, pool_id: str, name: str, actor: str) -> PoolEndpoint:
        self._require_writable(pool_id)
        rows = self._rows()
        group = self._pool_rows(pool_id, rows)
        if group is None:
            raise ServiceError(404, f"Pool {pool_id} was not found")
        pool = parse_pool(group[0], group[1], self._protected)
        aliases = self._aliases()
        existing = next((key for key in aliases if key.casefold() == name.casefold()), None)
        if existing is None or aliases[existing].casefold() != pool.id.casefold():
            raise ServiceError(404, f"{name} is not an alias of {pool.id}")
        try:
            self.client().update_router_settings(
                {"model_group_alias": {k: v for k, v in aliases.items() if k != existing}}
            )
            if pool.managed:
                kept = [alias for alias in pool.aliases if alias.casefold() != name.casefold()]
                if kept != pool.aliases:
                    self._record_aliases(pool, group[1], kept)
        except LiteLLMError as error:
            raise translate_error(error) from error
        logger.info("%s removed alias %s of pool %s", actor, existing, pool.id)
        return self.endpoint(pool.id)

    # -- removing pools ------------------------------------------------------------------

    def _test_keys(self, pool_ids: set[str], *, cleanup: bool) -> list[RemovalKey]:
        """Test keys this app created for these pools (cleanup: for any pool)."""
        prefix = test_key_alias(self._settings.id_prefix, "")
        keys = []
        try:
            for key in self.client().list_keys():
                alias = str(key.get("key_alias") or "")
                metadata = (key.get("metadata") or {}).get("pool_manager") or {}
                pool = str(metadata.get("pool") or "") if isinstance(metadata, Mapping) else ""
                if not alias.startswith(prefix) or not pool:
                    continue
                if cleanup or pool.casefold() in pool_ids:
                    keys.append(RemovalKey(id=str(key.get("token")), alias=alias, pool_id=pool))
        except LiteLLMError as error:
            raise translate_error(error) from error
        return sorted(keys, key=lambda key: key.alias or key.id)

    def pool_removal_plan(self, pool_id: str) -> RemovalPlan:
        """What removing one pool would delete, without changing anything."""
        pool = self.get_pool(pool_id)
        return self._removal_plan([pool], self.list_pools(), cleanup=False)

    def cleanup_plan(self) -> RemovalPlan:
        """Everything this app created on the gateway: its pools, their aliases, test keys."""
        pools = self.list_pools()
        owned = [pool for pool in pools if pool.managed and not pool.protected]
        return self._removal_plan(owned, pools, cleanup=True)

    def _removal_plan(
        self, removed: list[PoolView], pools: list[PoolView], *, cleanup: bool
    ) -> RemovalPlan:
        removed_ids = {pool.id.casefold() for pool in removed}
        blocked = [f"Pool {pool.id} is read-only here" for pool in removed if pool.protected]
        managed = {
            (alias.casefold(), pool.id.casefold()) for pool in removed for alias in pool.aliases
        }
        delete_aliases: list[RemovalAlias] = []
        unbind_aliases: list[RemovalAlias] = []
        for name, target in sorted(self._aliases().items(), key=lambda item: item[0].casefold()):
            if target.casefold() not in removed_ids:
                continue
            pool = next(pool.id for pool in removed if pool.id.casefold() == target.casefold())
            entry = RemovalAlias(name=name, pool_id=pool)
            if (name.casefold(), target.casefold()) in managed:
                delete_aliases.append(entry)
            else:
                unbind_aliases.append(entry)
        kept_pools = [pool.id for pool in pools if pool.id.casefold() not in removed_ids]
        return RemovalPlan(
            pools=[pool.id for pool in removed],
            delete_aliases=delete_aliases,
            unbind_aliases=unbind_aliases,
            delete_keys=self._test_keys(removed_ids, cleanup=cleanup),
            kept_pools=kept_pools if cleanup else [],
            blocked=blocked,
        )

    def remove_pool(self, pool_id: str, confirm: RemovalConfirm, actor: str) -> RemovalResult:
        """Deletes the pool's rows, its aliases and its test keys."""
        self._require_writable(pool_id)
        plan = self.pool_removal_plan(pool_id)
        if confirm.etag and confirm.etag != self.get_pool(pool_id).etag:
            raise ServiceError(409, "The pool changed since it was loaded. Reload and retry.")
        return self._remove(plan, confirm, actor)

    def cleanup(self, confirm: CleanupConfirm, actor: str) -> RemovalResult:
        """Removes everything this app created on the gateway; the gateway itself stays."""
        name = self._settings.gateway_name
        if confirm.confirm_name.strip().casefold() != name.casefold():
            raise ServiceError(400, f"Type the gateway name ({name}) to confirm")
        return self._remove(self.cleanup_plan(), confirm, actor)

    def _remove(self, plan: RemovalPlan, confirm: RemovalConfirm, actor: str) -> RemovalResult:
        if plan.blocked:
            raise ServiceError(409, " ".join(plan.blocked))

        def unseen(planned: list[str], confirmed: list[str]) -> list[str]:
            seen = {item.casefold() for item in confirmed}
            return [item for item in planned if item.casefold() not in seen]

        changed = [
            *unseen(plan.pools, confirm.pools),
            *unseen([alias.name for alias in plan.delete_aliases], confirm.delete_aliases),
            *unseen([alias.name for alias in plan.unbind_aliases], confirm.unbind_aliases),
            *unseen([key.alias or key.id for key in plan.delete_keys], confirm.delete_keys),
        ]
        if changed:
            listed = ", ".join(changed)
            raise ServiceError(409, f"Changed since you reviewed it ({listed}); review it again.")
        result = RemovalResult(pools=[], deleted_aliases=[], unbound_aliases=[], deleted_keys=[])
        client = self.client()
        dropped_members: list[str] = []
        try:
            # Aliases first, so none is left pointing at a pool that no longer exists.
            gone = {alias.name for alias in [*plan.delete_aliases, *plan.unbind_aliases]}
            if gone:
                aliases = self._aliases()
                client.update_router_settings(
                    {"model_group_alias": {k: v for k, v in aliases.items() if k not in gone}}
                )
                result.deleted_aliases = [alias.name for alias in plan.delete_aliases]
                result.unbound_aliases = [alias.name for alias in plan.unbind_aliases]
            if plan.delete_keys:
                client.delete_keys([key.id for key in plan.delete_keys])
                result.deleted_keys = [key.alias or key.id for key in plan.delete_keys]
            groups = self._groups(client.models())
            for pool_id in plan.pools:
                for row in (groups.get(pool_id.casefold()) or (pool_id, []))[1]:
                    client.delete_model(row_model_id(row))
                    dropped_members.append(row_member(row).id)
                logger.info("%s deleted pool %s", actor, pool_id)
                result.pools.append(pool_id)
        except LiteLLMError as error:
            failure = translate_error(error)
            done = [
                *(
                    f"removed alias {item}"
                    for item in [*result.deleted_aliases, *result.unbound_aliases]
                ),
                *(f"deleted key {item}" for item in result.deleted_keys),
                *(f"deleted pool {item}" for item in result.pools),
            ]
            raise ServiceError(
                failure.status,
                f"{failure.message} Already done: {', '.join(done) or 'nothing'}.",
            ) from error
        self._delete_unused_credentials(client, dropped_members)
        return result
