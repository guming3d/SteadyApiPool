"""Setup checks for the first-run wizard, and the fixes the app can make itself.

Three identities matter:

- The *operator* reads and changes the gateway: the user's ``az login`` locally, the app's
  managed identity in the Easy Auth deployment, and in the public app (APIM only) the user
  again, through the token exchanged for them (entra.py).
- The *user* acts for fixes (granting the gateway's identity a role on Foundry, enabling APIM's
  identity) with their own Azure rights. Deployed, that is the ARM token Easy Auth obtained for
  them; the app identity never gets permission to change role assignments.
- The gateway's own *managed identity* (APIM's system-assigned one, or LiteLLM's user-assigned
  one in AZURE_CLIENT_ID) calls the Foundry members and needs a data-plane role on each.

Effective permissions come from ``Microsoft.Authorization/permissions`` (what the caller may do
at a scope), so a check never needs more than read access. Fixes are limited to one role
(Cognitive Services OpenAI User), granted to the gateway's identity on accounts this app knows
about.

``AccessChecker`` runs the report; ``apim/access.py`` and ``litellm/access.py`` check their
gateway (its target, access, health and identity).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any, Literal, Protocol

from .arm import ArmClient, ArmError, jwt_claims, segment
from .config import Settings
from .errors import ServiceError
from .models import (
    AdminRequest,
    DeviceCodePrompt,
    DeviceCodeStatus,
    LocalSignIn,
    SetupCheck,
    SetupFix,
    SetupIdentity,
    SetupKind,
    SetupProgress,
    SetupProgressStep,
    SetupReport,
    SetupStatus,
    SetupStepKind,
)

logger = logging.getLogger(__name__)

AUTHORIZATION_API_VERSION = "2022-04-01"

# Mirrors infra/modules/discovery-rbac.bicep.
DISCOVERY_ACTIONS = (
    "Microsoft.CognitiveServices/accounts/read",
    "Microsoft.CognitiveServices/accounts/deployments/read",
)
ROLE_ASSIGNMENT_WRITE = "Microsoft.Authorization/roleAssignments/write"
# What the gateway calls on a member. Every inference role (OpenAI User, Cognitive Services User,
# Azure AI User, ...) allows it, so the check reads role definitions instead of hard-coding them.
INFERENCE_DATA_ACTION = (
    "Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action"
)
OPENAI_USER_ROLE = "Cognitive Services OpenAI User"
OPENAI_USER_ROLE_ID = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
DISCOVERY_FALLBACK_ROLE = "Reader"
ARM_AUDIENCES = frozenset({"https://management.azure.com", "https://management.core.windows.net"})

_CACHE_SECONDS = 300.0
# How long a role this app just assigned counts as there while Azure's role assignment list
# catches up (it can miss a new assignment for a while after it is created).
_GRANT_GRACE_SECONDS = 900.0
_WORKERS = 8
NOT_READY: frozenset[SetupStatus] = frozenset({"action", "blocked", "pending"})
# The gateway (or its deployment) after Azure could not finish creating or updating it.
FAILED = "Failed"

Permissions = list[Mapping[str, Any]]


@dataclass(frozen=True)
class UserToken:
    """The signed-in user's ARM access token, as Easy Auth injected it."""

    token: str
    expires_on: float

    @property
    def expired(self) -> bool:
        return self.expires_on - 60 < time.time()


class SignInCredential(Protocol):
    """credentials.LocalCredential; tests may provide only ``sign_in``."""

    def sign_in(self, tenant_id: str | None, scope: str) -> None: ...


class MemberAccounts(Protocol):
    """The product's pool service, as far as setup needs it."""

    def foundry_accounts(self) -> list[str]: ...


def user_token(headers: Mapping[str, str]) -> UserToken | None:
    token = headers.get("X-MS-TOKEN-AAD-ACCESS-TOKEN", "").strip()
    if not token:
        return None
    # Only the signed-in user's own token: Easy Auth sets the principal ID and clients cannot.
    principal = headers.get("X-MS-CLIENT-PRINCIPAL-ID", "").strip().casefold()
    oid = str(jwt_claims(token).get("oid") or "").casefold()
    if principal and oid != principal:
        return None
    expires_on = _iso_timestamp(headers.get("X-MS-TOKEN-AAD-EXPIRES-ON", ""))
    if expires_on is None:
        exp = jwt_claims(token).get("exp")
        expires_on = float(exp) if isinstance(exp, int | float) else time.time() + 300
    return UserToken(token, expires_on)


def _iso_timestamp(value: str) -> float | None:
    # Easy Auth writes e.g. 2026-10-24T01:28:11.0000000Z; fromisoformat wants 6 fraction digits.
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z?", value.strip())
    if not match:
        return None
    fraction = (match.group(2) or ".0")[:7]
    return datetime.fromisoformat(f"{match.group(1)}{fraction}+00:00").timestamp()


def _matches(pattern: str, action: str) -> bool:
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, action, re.IGNORECASE) is not None


def allows(permissions: Iterable[Mapping[str, Any]], action: str, *, data: bool = False) -> bool:
    """Whether any permission entry (one per role assignment) allows ``action``."""
    key, deny = ("dataActions", "notDataActions") if data else ("actions", "notActions")
    return any(
        any(_matches(pattern, action) for pattern in entry.get(key) or [])
        and not any(_matches(pattern, action) for pattern in entry.get(deny) or [])
        for entry in permissions
    )


def missing(permissions: Permissions, actions: Iterable[str]) -> list[str]:
    return [action for action in actions if not allows(permissions, action)]


def identity_of(claims: Mapping[str, Any]) -> SetupIdentity:
    name = claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name")
    return SetupIdentity(
        name=str(name) if name else None,
        object_id=str(claims["oid"]) if claims.get("oid") else None,
        tenant_id=str(claims["tid"]) if claims.get("tid") else None,
    )


def portal(scope: str, blade: str = "overview") -> str:
    return f"https://portal.azure.com/#resource{scope}/{blade}"


def _account_name(account_id: str) -> str:
    return account_id.rstrip("/").rpartition("/")[2]


def _no_tick() -> None:
    pass


class _Tracker:
    """The steps of one setup report as it runs; each starts when the previous one ends."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._steps: list[SetupProgressStep] = []
        self._running = True

    def step(
        self, kind: SetupStepKind, *, subject: str | None = None, total: int | None = None
    ) -> None:
        with self._lock:
            for step in self._steps:
                step.state = "done"
            self._steps.append(
                SetupProgressStep(
                    kind=kind,
                    state="running",
                    subject=subject,
                    done=None if total is None else 0,
                    total=total,
                )
            )

    def tick(self) -> None:
        """One more of the current step's subscriptions or accounts is done; any thread."""
        with self._lock:
            if self._steps:
                self._steps[-1].done = (self._steps[-1].done or 0) + 1

    def finish(self) -> None:
        with self._lock:
            for step in self._steps:
                step.state = "done"
            self._running = False

    def view(self) -> SetupProgress:
        with self._lock:
            return SetupProgress(
                running=self._running, steps=[step.model_copy() for step in self._steps]
            )


class AccessChecker:
    """The setup report and its fixes. Each product says how to check its gateway by
    overriding the ``gateway_*`` hooks; without a gateway chosen this base class reports that
    one needs choosing."""

    # This app's custom role on the gateway (infra/modules), and the built-in role an
    # administrator can assign instead.
    gateway_role = ""
    gateway_fallback_role = ""
    # This app's custom role for discovery (infra/modules/discovery-rbac.bicep).
    discovery_role = ""
    # Who calls the Foundry members, for the role request.
    identity_label = "gateway managed identity"
    # The gateway has its own health to check (LiteLLM: its admin API and database).
    checks_health = False

    def __init__(
        self,
        settings: Settings,
        arm: ArmClient,
        service: MemberAccounts | None,
        discovered_accounts: Callable[[], Iterable[str]],
        credential: SignInCredential | None = None,
    ) -> None:
        self._settings = settings
        self._arm = arm
        self._members = service
        self._discovered_accounts = discovered_accounts
        self._credential = credential
        self._lock = Lock()
        self._writers: dict[str, tuple[float, bool]] = {}
        self._roles: dict[str, Permissions] = {}
        self._role_names: dict[str, str | None] = {}
        # (account, principal) -> when this app assigned the gateway's inference role there.
        self._granted: dict[tuple[str, str], float] = {}
        # The report being built for each caller, while it runs.
        self._reports: dict[str, _Tracker] = {}

    @property
    def local(self) -> bool:
        return self._settings.auth_mode == "disabled"

    @property
    def _mode(self) -> Literal["local", "deployed", "public", "guest"]:
        settings = self._settings
        if settings.guest:
            return "guest"
        return "local" if settings.local else "public" if settings.public else "deployed"

    @property
    def _subscription_scope(self) -> str:
        return f"/subscriptions/{segment(self._settings.subscription_id)}"

    @property
    def _resource_group_scope(self) -> str:
        return f"{self._subscription_scope}/resourceGroups/{segment(self._settings.resource_group)}"

    def user_arm(self, token: UserToken | None) -> ArmClient | None:
        """ARM as the signed-in user: the operator locally and in the public app, Easy Auth's
        token in the Easy Auth deployment."""
        if self._settings.operator_is_user:
            return self._arm
        if self._settings.guest:
            return None  # Guests act as nobody; a token header they send means nothing.
        if token is None or token.expired:
            return None
        return self._arm.with_token(token.token, token.expires_on)

    def permissions(self, arm: ArmClient, scope: str) -> Permissions:
        return list(
            arm.list_arm(
                f"{scope}/providers/Microsoft.Authorization/permissions",
                api_version=AUTHORIZATION_API_VERSION,
            )
        )

    # -- what each product checks about its gateway ---------------------------------------

    def gateway_target(self) -> SetupCheck:
        """Which gateway the app manages, and whether it has finished deploying."""
        fix: SetupFix | None = "select_gateway" if self._settings.can_choose_target else None
        return SetupCheck(id="gateway_target", kind="gateway_target", status="action", fix=fix)

    def gateway_access(
        self, arm: ArmClient | None, identity: SetupIdentity | None, owner: bool
    ) -> tuple[SetupCheck, Permissions | None]:
        """Whether the user may work with the gateway; their permissions on it, if read."""
        raise NotImplementedError

    def app_gateway(self) -> tuple[SetupCheck, SetupIdentity | None]:
        """Deployed: whether the app's own identity may work with the gateway."""
        raise NotImplementedError

    def gateway_health(self) -> SetupCheck:
        """Whether the gateway itself answers; only called when ``checks_health``."""
        raise NotImplementedError

    def gateway_identity(
        self, user_permissions: Permissions | None
    ) -> tuple[SetupCheck, str | None]:
        """The managed identity the gateway calls Foundry as, and its principal ID."""
        raise NotImplementedError

    def gateway_principal(self) -> str | None:
        """The principal ID of the gateway's managed identity, if it has one."""
        raise NotImplementedError

    def user_may_write(self, arm: ArmClient) -> bool:
        """Whether the user's own Azure roles let them manage the gateway's pools."""
        return False

    def forget_gateway(self) -> None:
        """Drops what the product caches about the gateway, for "Check again"."""

    # -- write access for the deployed app ----------------------------------------------

    def user_can_write(self, token: UserToken) -> bool:
        """Whether the user's own Azure roles let them manage the gateway's pools."""
        # Keyed by the token itself: its claims are not verified here, only ARM verifies them.
        key = hashlib.sha256(token.token.encode()).hexdigest()
        with self._lock:
            cached = self._writers.get(key)
        if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
            return cached[1]
        arm = self.user_arm(token)
        allowed = False
        if arm is not None and self._settings.has_target:
            try:
                allowed = self.user_may_write(arm)
            except ArmError as error:
                logger.info("Cannot read the user's gateway permissions: %s", error.message)
        with self._lock:
            self._writers[key] = (time.monotonic(), allowed)
        return allowed

    def forget(self) -> None:
        with self._lock:
            self._writers.clear()
            self._roles.clear()
            self._role_names.clear()
        self.forget_gateway()
        # "Check again" also retries sign-in tools that were missing a minute ago.
        forget_misses = getattr(self._credential, "forget_misses", None)
        if callable(forget_misses):
            forget_misses()

    # -- report -------------------------------------------------------------------------

    def report(self, token: UserToken | None, *, principal: str | None, owner: bool) -> SetupReport:
        key = principal or ""
        tracker = _Tracker()
        with self._lock:
            self._reports[key] = tracker
        try:
            return self._report(token, principal=principal, owner=owner, tracker=tracker)
        finally:
            tracker.finish()
            with self._lock:
                if self._reports.get(key) is tracker:
                    del self._reports[key]

    def progress(self, principal: str | None) -> SetupProgress:
        """What the caller's report is waiting for; nothing once it has been sent."""
        with self._lock:
            tracker = self._reports.get(principal or "")
        return tracker.view() if tracker else SetupProgress(running=False, steps=[])

    def _report(
        self, token: UserToken | None, *, principal: str | None, owner: bool, tracker: _Tracker
    ) -> SetupReport:
        name = self._settings.target_name or None
        if self._settings.guest:
            # Nothing to sign in to or fix: the gateway is the one the deployment shows guests.
            tracker.step("gateway_target", subject=name)
            target = self._target()
            return self._finish("guest", None, [target], target.status == "ok")
        checks: list[SetupCheck] = []
        user = self.user_arm(token)
        tracker.step("sign_in")
        if self._settings.operator_is_user:
            sign_in, identity = self._local_sign_in()
            checks.append(sign_in)
            if sign_in.status != "ok":
                return self._finish(self._mode, identity, checks, can_operate=False)
        else:
            checks.append(SetupCheck(id="sign_in", kind="sign_in", status="ok", detail=principal))
            token_check, identity = self._token_check(token)
            checks.append(token_check)
            if token_check.status != "ok":
                user = None

        tracker.step("gateway_target", subject=name)
        target = self._target()
        checks.append(target)
        # A failed update usually leaves the gateway running, so let the checks below decide
        # whether the app can work with it; a failed deployment fails those too.
        if target.status in NOT_READY and target.detail != FAILED:
            return self._finish(self._mode, identity, checks, False)

        tracker.step("gateway_access")
        access_check, user_permissions = self.gateway_access(user, identity, owner)
        checks.append(access_check)
        discovery_total = len(self._settings.discovery_subscription_list)
        discovery_kind: SetupKind = "discovery"
        discovery_identity = identity
        if self._settings.operator_is_user:
            can_operate = access_check.status != "blocked"
        else:
            tracker.step("app_gateway")
            app_check, discovery_identity = self.app_gateway()
            checks.append(app_check)
            can_operate = app_check.status != "blocked"
            discovery_kind = "app_discovery"
        if can_operate and self.checks_health:
            tracker.step("gateway_health", subject=name)
            health = self.gateway_health()
            checks.append(health)
            can_operate = health.status == "ok"
        tracker.step("discovery", total=discovery_total)
        checks.extend(
            self._discovery(self._arm, discovery_identity, kind=discovery_kind, tick=tracker.tick)
        )

        tracker.step("gateway_identity")
        identity_check, principal_id = self.gateway_identity(user_permissions)
        checks.append(identity_check)
        if can_operate:
            tracker.step("members")
            accounts = self._member_accounts()
            if accounts:
                tracker.step("foundry_access", total=len(accounts))
            checks.extend(self._foundry(accounts, principal_id, user, tick=tracker.tick))
        return self._finish(self._mode, identity, checks, can_operate)

    def _target(self) -> SetupCheck:
        if not self._settings.has_target:
            return AccessChecker.gateway_target(self)
        return self.gateway_target()

    def _member_accounts(self) -> list[str]:
        if self._members is None:
            return []
        try:
            return self._members.foundry_accounts()
        except ServiceError as error:
            logger.warning("Cannot list pool members: %s", error.message)
            return []

    def _finish(
        self, mode: Any, identity: SetupIdentity | None, checks: list[SetupCheck], can_operate: bool
    ) -> SetupReport:
        return SetupReport(
            mode=mode,
            product=self._settings.product,
            user=identity,
            checks=checks,
            ready=not any(check.status in NOT_READY for check in checks),
            can_operate=can_operate,
            local_sign_in=self._local_sign_in_view() if self.local else None,
        )

    def _local_sign_in_view(self) -> LocalSignIn | None:
        credential = self._credential
        if credential is None:
            return None
        from .credentials import installed_tools

        return LocalSignIn(
            source=getattr(credential, "source", None),
            remembered=bool(getattr(credential, "remembered", False)),
            persistent=bool(getattr(credential, "persistent", False)),
            tools=installed_tools(),  # type: ignore[arg-type]
        )

    def _local_sign_in(self) -> tuple[SetupCheck, SetupIdentity | None]:
        """The operator's own sign-in: locally the developer's, in the public app the user's
        (signing in again in the browser fixes it, or consenting to Azure access)."""
        public = self._settings.public
        fix: SetupFix | None = "sign_in" if self._credential is not None or public else None
        try:
            claims = self._arm.token_claims()
        except ArmError as error:  # The credential could not get a token.
            detail = error.message
            if public and error.code == "ConsentRequired":
                fix = "consent"
            check = SetupCheck(
                id="sign_in", kind="sign_in", status="action", detail=detail, fix=fix
            )
            return check, None
        identity = identity_of(claims)
        tenant = self._arm.subscription_tenant()
        if tenant and identity.tenant_id and tenant.casefold() != identity.tenant_id.casefold():
            detail = (
                f"signed in to tenant {identity.tenant_id}; "
                f"the gateway's subscription is in {tenant}"
            )
            # The public app signs the browser in again to this tenant.
            check = SetupCheck(
                id="sign_in",
                kind="sign_in",
                status="action",
                detail=detail,
                fix=fix,
                subject=tenant if public else None,
            )
            return check, identity
        return SetupCheck(id="sign_in", kind="sign_in", status="ok", detail=identity.name), identity

    def _token_check(self, token: UserToken | None) -> tuple[SetupCheck, SetupIdentity | None]:
        def check(status: SetupStatus, fix: Any = None, detail: str | None = None) -> SetupCheck:
            return SetupCheck(
                id="azure_token", kind="azure_token", status=status, fix=fix, detail=detail
            )

        if token is None:
            if not self._settings.delegated_access:
                # Only an administrator can turn this on; see README "Delegated Azure access".
                request = AdminRequest(
                    assignee=self._settings.target_name,
                    assignee_id=None,
                    role=None,
                    scope=None,
                    command="azd env set ENABLE_DELEGATED_AZURE_ACCESS true\nazd provision",
                    portal_url=None,
                )
                return SetupCheck(
                    id="azure_token",
                    kind="azure_token",
                    status="action",
                    detail="delegated Azure access is off",
                    request=request,
                ), None
            return check("action", "consent", "Easy Auth provided no Azure token"), None
        claims = jwt_claims(token.token)
        identity = identity_of(claims)
        audience = str(claims.get("aud") or "").rstrip("/")
        if audience and audience not in ARM_AUDIENCES:
            return check("action", "consent", f"token is for {audience}, not Azure"), identity
        if token.expired:
            return check("action", "refresh_token"), identity
        return check("ok"), identity

    def gateway_request(
        self, identity: SetupIdentity | None, principal_type: str, *, command: str | None = None
    ) -> AdminRequest:
        """The role on the gateway to ask an administrator for."""
        role = (
            self._custom_role(self.gateway_role, self._resource_group_scope)
            or self.gateway_fallback_role
        )
        return self.role_request(
            identity, principal_type, role, self._arm.resource_id, command=command
        )

    def _discovery(
        self,
        arm: ArmClient,
        identity: SetupIdentity | None,
        *,
        kind: Any,
        tick: Callable[[], None] = _no_tick,
    ) -> list[SetupCheck]:
        principal_type = "User" if kind == "discovery" else "ServicePrincipal"

        def check(subscription: str) -> SetupCheck:
            scope = f"/subscriptions/{segment(subscription)}"
            gaps: list[str] = []
            try:
                gaps = missing(self.permissions(arm, scope), DISCOVERY_ACTIONS)
            except ArmError as error:
                logger.info("Cannot read permissions on %s: %s", subscription, error.message)
                gaps = list(DISCOVERY_ACTIONS)
            if gaps:
                # Access granted on resource groups only shows up when listing, not at this scope.
                try:
                    if any(
                        arm.list_arm(
                            f"{scope}/providers/Microsoft.CognitiveServices/accounts",
                            api_version=self._settings.cognitive_api_version,
                        )
                    ):
                        gaps = []
                except ArmError:
                    pass
            request = None
            if gaps:
                role = self._custom_role(self.discovery_role, scope) or DISCOVERY_FALLBACK_ROLE
                request = self.role_request(
                    identity,
                    principal_type,
                    role,
                    scope,
                    command="azd provision" if kind == "app_discovery" else None,
                )
            return SetupCheck(
                id=f"{kind}:{subscription}",
                kind=kind,
                subject=subscription,
                status="action" if gaps else "ok",
                missing=gaps,
                request=request,
            )

        def counted(subscription: str) -> SetupCheck:
            try:
                return check(subscription)
            finally:
                tick()

        subscriptions = self._settings.discovery_subscription_list
        if not subscriptions:
            return []
        with ThreadPoolExecutor(max_workers=min(_WORKERS, len(subscriptions))) as executor:
            return list(executor.map(counted, subscriptions))

    def _foundry(
        self,
        accounts: list[str],
        principal: str | None,
        user: ArmClient | None,
        *,
        tick: Callable[[], None] = _no_tick,
    ) -> list[SetupCheck]:
        if not accounts:
            return []

        def counted(account: str) -> SetupCheck:
            try:
                return self._foundry_check(account, principal, user)
            finally:
                tick()

        with ThreadPoolExecutor(max_workers=min(_WORKERS, len(accounts))) as executor:
            return list(executor.map(counted, accounts))

    def _foundry_check(
        self, account: str, principal: str | None, user: ArmClient | None
    ) -> SetupCheck:
        def check(status: SetupStatus, **values: Any) -> SetupCheck:
            return SetupCheck(
                id=f"foundry_access:{account.casefold()}",
                kind="foundry_access",
                subject=account,
                status=status,
                **values,
            )

        if principal is None:
            return check("skipped")
        reader = user or self._arm
        try:
            has_role = self._has_inference_role(reader, account, principal)
        except ArmError as error:
            return check(
                "unknown", detail=error.message, request=self._foundry_request(account, principal)
            )
        if has_role:
            return check("ok")
        with self._lock:
            granted = self._granted.get((account.casefold(), principal))
        if granted is not None and time.monotonic() - granted < _GRANT_GRACE_SECONDS:
            # Assigned moments ago; Azure's list doesn't show it yet.
            return check("ok", detail="granted")
        can_grant = False
        if user is not None:
            try:
                can_grant = allows(self.permissions(user, account), ROLE_ASSIGNMENT_WRITE)
            except ArmError as error:
                logger.info("Cannot read permissions on %s: %s", account, error.message)
        return check(
            "action",
            fix="grant_foundry_access" if can_grant else None,
            request=self._foundry_request(account, principal),
        )

    def _has_inference_role(self, arm: ArmClient, account: str, principal: str) -> bool:
        assignments = arm.list_arm(
            f"{account}/providers/Microsoft.Authorization/roleAssignments",
            api_version=AUTHORIZATION_API_VERSION,
            params={"$filter": f"principalId eq '{principal}'"},
        )
        for assignment in assignments:
            properties = assignment.get("properties") or {}
            scope = str(properties.get("scope") or "").rstrip("/").casefold()
            # The filter also returns assignments below the account; only those at or above count.
            if not account.casefold().startswith(scope):
                continue
            if allows(
                self._role_permissions(arm, str(properties.get("roleDefinitionId") or "")),
                INFERENCE_DATA_ACTION,
                data=True,
            ):
                return True
        return False

    def _role_permissions(self, arm: ArmClient, role_definition_id: str) -> Permissions:
        key = role_definition_id.casefold()
        with self._lock:
            if key in self._roles:
                return self._roles[key]
        body = arm.get_arm_json(role_definition_id, api_version=AUTHORIZATION_API_VERSION) or {}
        permissions = list((body.get("properties") or {}).get("permissions") or [])
        with self._lock:
            self._roles[key] = permissions
        return permissions

    def _custom_role(self, prefix: str, scope: str) -> str | None:
        """The name of this app's custom role (created by azd provision), if it exists."""
        if not prefix:
            return None
        key = f"{prefix}|{scope}".casefold()
        with self._lock:
            if key in self._role_names:
                return self._role_names[key]
        name = None
        try:
            for role in self._arm.list_arm(
                f"{scope}/providers/Microsoft.Authorization/roleDefinitions",
                api_version=AUTHORIZATION_API_VERSION,
                params={"$filter": "type eq 'CustomRole'"},
            ):
                role_name = str((role.get("properties") or {}).get("roleName") or "")
                if role_name.startswith(prefix):
                    name = role_name
                    break
        except ArmError as error:
            logger.info("Cannot list custom roles at %s: %s", scope, error.message)
        with self._lock:
            self._role_names[key] = name
        return name

    def role_request(
        self,
        identity: SetupIdentity | None,
        principal_type: str,
        role: str,
        scope: str,
        *,
        command: str | None = None,
    ) -> AdminRequest:
        object_id = identity.object_id if identity else None
        assignee = (identity.name if identity else None) or object_id or "<your account>"
        target = (
            f"--assignee-object-id {object_id} --assignee-principal-type {principal_type}"
            if object_id
            else f'--assignee "{assignee}"'
        )
        role_command = f'az role assignment create {target} --role "{role}" --scope "{scope}"'
        return AdminRequest(
            assignee=assignee,
            assignee_id=object_id,
            role=role,
            scope=scope,
            command=f"{command}\n# or: {role_command}" if command else role_command,
            portal_url=portal(scope, "users"),
        )

    def _foundry_request(self, account: str, principal: str) -> AdminRequest:
        return AdminRequest(
            assignee=f"{self._settings.target_name} ({self.identity_label})",
            assignee_id=principal,
            role=OPENAI_USER_ROLE,
            scope=account,
            command=f"az role assignment create --assignee-object-id {principal} "
            f'--assignee-principal-type ServicePrincipal --role "{OPENAI_USER_ROLE}" '
            f'--scope "{account}"',
            portal_url=portal(account, "users"),
        )

    # -- fixes --------------------------------------------------------------------------

    def _principal(self) -> str | None:
        if not self._settings.has_target:
            return None
        return self.gateway_principal()

    def foundry_checks(self, token: UserToken | None, account_ids: list[str]) -> list[SetupCheck]:
        """Read-only: whether the gateway's identity can call these accounts, e.g. before
        importing them."""
        return self._foundry(account_ids, self._principal(), self.user_arm(token))

    def grant_foundry_access(
        self, token: UserToken | None, account_ids: list[str], actor: str
    ) -> list[SetupCheck]:
        user = self.require_user(token)
        known = {account.casefold() for account in self._member_accounts()}
        known.update(account.casefold() for account in self._discovered_accounts())
        unknown = [account for account in account_ids if account.casefold() not in known]
        if unknown:
            raise ServiceError(
                400, f"Not a member or discovered Foundry account: {', '.join(unknown)}"
            )
        principal = self._principal()
        if principal is None:
            raise ServiceError(409, "The gateway has no managed identity to grant access to")
        for account in account_ids:
            subscription = account.split("/")[2]
            name = uuid.uuid5(
                uuid.NAMESPACE_URL, f"{account.casefold()}|{principal}|{OPENAI_USER_ROLE_ID}"
            )
            try:
                user.request(
                    "PUT",
                    user.arm_url(
                        f"{account}/providers/Microsoft.Authorization/roleAssignments/{name}"
                    ),
                    api_version=AUTHORIZATION_API_VERSION,
                    json_body={
                        "properties": {
                            "roleDefinitionId": f"/subscriptions/{subscription}/providers/"
                            f"Microsoft.Authorization/roleDefinitions/{OPENAI_USER_ROLE_ID}",
                            "principalId": principal,
                            "principalType": "ServicePrincipal",
                        }
                    },
                )
            except ArmError as error:
                if error.code == "RoleAssignmentExists":
                    self._remember_grant(account, principal)
                    continue
                if error.status in {401, 403}:
                    raise ServiceError(
                        403,
                        f"Your Azure account cannot assign roles on {_account_name(account)}: "
                        f"{error.message}",
                    ) from error
                raise ServiceError(502, f"Assigning the role failed: {error.message}") from error
            self._remember_grant(account, principal)
            logger.info(
                "%s granted %s to gateway identity %s on %s",
                actor,
                OPENAI_USER_ROLE,
                principal,
                account,
            )
        return self._foundry(account_ids, principal, user)

    def _remember_grant(self, account: str, principal: str) -> None:
        with self._lock:
            self._granted[(account.casefold(), principal)] = time.monotonic()

    def sign_in(self, tenant_id: str | None = None) -> None:
        """Local only: opens the browser sign-in on this machine and waits for it. Without a
        tenant it signs in to the gateway's tenant, or the home tenant if there is none."""
        if not self.local or self._credential is None:
            raise ServiceError(400, "Browser sign-in is only available when running locally")
        tenant = tenant_id or self._settings.tenant_id or self._arm.subscription_tenant()
        try:
            self._credential.sign_in(tenant, self._arm.token_scope)
        except Exception as error:
            raise ServiceError(401, f"Sign-in did not complete: {error}") from error
        self._arm.reset_token()
        self.forget()

    def _local_credential(self) -> Any:
        if not self.local or self._credential is None:
            raise ServiceError(
                400, "Signing in from the app is only available when running locally"
            )
        return self._credential

    def start_device_code(self, tenant_id: str | None = None) -> DeviceCodePrompt:
        """Local only: starts a device-code sign-in for machines without a local browser (SSH,
        servers, WSL, containers). It finishes in the background; poll device_code_status."""
        credential = self._local_credential()
        tenant = tenant_id or self._settings.tenant_id or self._arm.subscription_tenant()
        try:
            prompt = credential.start_device_code(tenant, self._arm.token_scope)
        except Exception as error:
            raise ServiceError(502, f"Device-code sign-in did not start: {error}") from error
        return DeviceCodePrompt(
            verification_uri=prompt.verification_uri,
            user_code=prompt.user_code,
            expires_on=prompt.expires_on,
        )

    def device_code_status(self) -> DeviceCodeStatus:
        credential = self._local_credential()
        flow = credential.device_flow(acknowledge=True)
        if flow.state == "done" and flow.unacknowledged:
            # Tokens cached for the previous account (or a failed attempt) must go.
            self._arm.reset_token()
            self.forget()
        return DeviceCodeStatus(state=flow.state, error=flow.error)

    def sign_out(self) -> None:
        """Local only: forgets the app's own sign-in; az / azd / PowerShell are left alone."""
        self._local_credential().sign_out()
        self._arm.reset_token()
        self.forget()

    def require_user(self, token: UserToken | None) -> ArmClient:
        if self._settings.guest:
            raise ServiceError(
                403, "The guest view is read-only: sign in with your own Microsoft account for this"
            )
        user = self.user_arm(token)
        if user is None:
            raise ServiceError(401, "Sign in to Azure first: the app has no Azure token for you")
        return user
