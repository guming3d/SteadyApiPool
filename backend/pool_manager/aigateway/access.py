"""Setup checks for an Azure AI Gateway: it exists and finished provisioning, the reads and
writes the app makes on it, and its system-assigned identity (which calls Foundry, and needs
Foundry User on each member account); plus the fix that turns that identity on."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Literal

from ..access import (
    FAILED,
    FOUNDRY_USER_ROLE,
    FOUNDRY_USER_ROLE_ID,
    AccessChecker,
    Permissions,
    SignInCredential,
    UserToken,
    allows,
    identity_of,
    missing,
    portal,
)
from ..arm import ArmClient, ArmError
from ..config import Settings
from ..errors import ServiceError
from ..models import AdminRequest, SetupCheck, SetupFix, SetupIdentity, SetupKind, SetupStatus
from .service import AiGatewayService

logger = logging.getLogger(__name__)

_GATEWAY = "Microsoft.ApiManagement/aigateways"
_WORKSPACE = f"{_GATEWAY}/workspaces"
# Mirrors infra/modules/aigateway-rbac.bicep.
AIGATEWAY_READ_ACTIONS = (
    f"{_GATEWAY}/read",
    f"{_WORKSPACE}/read",
    f"{_WORKSPACE}/aliases/read",
    f"{_WORKSPACE}/modelProviders/read",
    f"{_WORKSPACE}/modelProviders/models/read",
    f"{_WORKSPACE}/models/read",
    f"{_WORKSPACE}/telemetryExporters/read",
)
AIGATEWAY_WRITE_ACTIONS = (
    f"{_WORKSPACE}/aliases/write",
    # Only aliases this app created, when their pool is removed.
    f"{_WORKSPACE}/aliases/delete",
    f"{_WORKSPACE}/modelProviders/write",
    f"{_WORKSPACE}/modelProviders/delete",
    f"{_WORKSPACE}/modelProviders/models/write",
    f"{_WORKSPACE}/modelProviders/models/delete",
    # Only the test key this app created, when the gateway is cleaned up.
    f"{_GATEWAY}/apiKeys/delete",
)
# The test key is read and created with the user's own rights, never the app's.
AIGATEWAY_KEY_ACTIONS = (
    f"{_GATEWAY}/apiKeys/read",
    f"{_GATEWAY}/apiKeys/write",
    f"{_GATEWAY}/apiKeys/listSecrets/action",
)
AIGATEWAY_WRITE = f"{_GATEWAY}/write"
AIGATEWAY_ROLE = "SteadyApiPool AI Gateway Manager"
AIGATEWAY_FALLBACK_ROLE = "Contributor"
DISCOVERY_ROLE = "SteadyApiPool AI Gateway Deployment Reader"


class AiGatewayAccessChecker(AccessChecker):
    gateway_role = AIGATEWAY_ROLE
    gateway_fallback_role = AIGATEWAY_FALLBACK_ROLE
    discovery_role = DISCOVERY_ROLE
    identity_label = "AI Gateway managed identity"
    foundry_role = FOUNDRY_USER_ROLE
    foundry_role_id = FOUNDRY_USER_ROLE_ID

    def __init__(
        self,
        settings: Settings,
        arm: ArmClient,
        service: AiGatewayService,
        discovered_accounts: Callable[[], Iterable[str]],
        credential: SignInCredential | None = None,
    ) -> None:
        super().__init__(settings, arm, service, discovered_accounts, credential)
        self._service = service

    def user_may_write(self, arm: ArmClient) -> bool:
        """Whether the user's own roles allow every read and write the app makes."""
        permissions = self.permissions(arm, self._arm.resource_id)
        return not missing(permissions, (*AIGATEWAY_READ_ACTIONS, *AIGATEWAY_WRITE_ACTIONS))

    def forget_gateway(self) -> None:
        self._service.forget_gateway()

    def gateway_target(self) -> SetupCheck:
        """Which AI Gateway the app manages, and whether it has finished provisioning."""
        settings = self._settings
        fix: SetupFix | None = "select_gateway" if settings.can_choose_target else None
        kind: SetupKind = "gateway_target"
        subject = settings.aigateway_name
        try:
            found = self._arm.get_json("", api_version=settings.aigateway_api_version)
        except ArmError as error:
            # Missing read access is reported by the access check that follows.
            return SetupCheck(
                id=kind, kind=kind, subject=subject, status="unknown", detail=error.message
            )
        if found is None:
            return SetupCheck(
                id=kind, kind=kind, subject=subject, status="blocked", detail="not_found", fix=fix
            )
        state = str((found[0].get("properties") or {}).get("provisioningState") or "")
        if state.casefold() in {"created", "creating", "activating", "accepted", "provisioning"}:
            started = (found[0].get("systemData") or {}).get("createdAt")
            return SetupCheck(
                id=kind,
                kind=kind,
                subject=subject,
                status="pending",
                detail=state,
                started_at=str(started) if started else None,
            )
        if state.casefold() in {"failed", "canceled"}:
            return SetupCheck(
                id=kind, kind=kind, subject=subject, status="action", detail=FAILED, fix=fix
            )
        return SetupCheck(id=kind, kind=kind, subject=subject, status="ok")

    def gateway_access(
        self, arm: ArmClient | None, identity: SetupIdentity | None, owner: bool
    ) -> tuple[SetupCheck, Permissions | None]:
        kind: SetupKind = "gateway_access"
        scope = self._arm.resource_id
        if arm is None:
            if owner:
                return SetupCheck(id=kind, kind=kind, status="ok", granted_by="owners"), None
            return SetupCheck(id=kind, kind=kind, status="unknown"), None
        try:
            permissions = self.permissions(arm, scope)
        except ArmError as error:
            status: SetupStatus = "blocked" if self._settings.operator_is_user else "unknown"
            return SetupCheck(id=kind, kind=kind, status=status, detail=error.message), None
        read = missing(permissions, AIGATEWAY_READ_ACTIONS)
        write = missing(permissions, AIGATEWAY_WRITE_ACTIONS)
        request = self.gateway_request(identity, "User")
        if self._settings.operator_is_user:
            status = "blocked" if read else "action" if write else "ok"
            return SetupCheck(
                id=kind,
                kind=kind,
                status=status,
                missing=[*read, *write],
                request=request if status != "ok" else None,
            ), permissions
        granted: Literal["owners", "azure_rbac"] | None = (
            "owners" if owner else "azure_rbac" if not read and not write else None
        )
        return SetupCheck(
            id=kind,
            kind=kind,
            status="ok" if granted else "action",
            missing=[] if granted else [*read, *write],
            request=None if granted else request,
            granted_by=granted,
        ), permissions

    def app_gateway(self) -> tuple[SetupCheck, SetupIdentity | None]:
        kind: SetupKind = "app_gateway"
        try:
            identity = identity_of(self._arm.token_claims())
            permissions = self.permissions(self._arm, self._arm.resource_id)
        except ArmError as error:
            return SetupCheck(id=kind, kind=kind, status="blocked", detail=error.message), None
        read = missing(permissions, AIGATEWAY_READ_ACTIONS)
        write = missing(permissions, AIGATEWAY_WRITE_ACTIONS)
        status: SetupStatus = "blocked" if read else "action" if write else "ok"
        request = None
        if status != "ok":
            request = self.gateway_request(identity, "ServicePrincipal", command="azd provision")
        return SetupCheck(
            id=kind, kind=kind, status=status, missing=[*read, *write], request=request
        ), identity

    def gateway_principal(self) -> str | None:
        try:
            return str(self._service.identity().get("principalId") or "") or None
        except ArmError as error:
            logger.info("Cannot read the AI Gateway identity: %s", error.message)
            return None

    def gateway_identity(
        self, user_permissions: Permissions | None
    ) -> tuple[SetupCheck, str | None]:
        kind: SetupKind = "gateway_identity"
        try:
            identity = self._service.identity()
        except ArmError as error:
            return SetupCheck(id=kind, kind=kind, status="unknown", detail=error.message), None
        principal = str(identity.get("principalId") or "") or None
        if principal:
            return SetupCheck(id=kind, kind=kind, status="ok", subject=principal), principal
        can_enable = user_permissions is not None and allows(user_permissions, AIGATEWAY_WRITE)
        scope = self._arm.resource_id
        return SetupCheck(
            id=kind,
            kind=kind,
            status="action",
            fix="enable_aigateway_identity" if can_enable else None,
            request=AdminRequest(
                assignee=self._settings.aigateway_name,
                assignee_id=None,
                role=None,
                scope=scope,
                command=f"az aigateway identity assign -g {self._settings.resource_group} "
                f"-n {self._settings.aigateway_name} --system-assigned true",
                portal_url=portal(scope),
            ),
        ), None

    def enable_identity(self, token: UserToken | None, actor: str) -> SetupCheck:
        user = self.require_user(token)
        try:
            identity = self._service.identity()
            if identity.get("principalId"):
                return SetupCheck(id="gateway_identity", kind="gateway_identity", status="ok")
            current = str(identity.get("type") or "None")
            assigned = dict(identity.get("userAssignedIdentities") or {})
            # Keep any user-assigned identities; PATCH replaces the whole identity block.
            body: dict[str, object] = {
                "type": "SystemAssigned, UserAssigned"
                if "userassigned" in current.casefold()
                else "SystemAssigned"
            }
            if assigned:
                body["userAssignedIdentities"] = {key: {} for key in assigned}
            response = user.request(
                "PATCH",
                "",
                api_version=self._settings.aigateway_api_version,
                json_body={"identity": body},
            )
            user.wait(response)
        except ArmError as error:
            if error.status in {401, 403}:
                raise ServiceError(
                    403, f"Your Azure account cannot change the AI Gateway: {error.message}"
                ) from error
            raise ServiceError(502, f"Enabling the identity failed: {error.message}") from error
        logger.info(
            "%s enabled the system-assigned identity of %s", actor, self._settings.aigateway_name
        )
        self._service.forget_gateway()
        check, _ = self.gateway_identity(None)
        return check
