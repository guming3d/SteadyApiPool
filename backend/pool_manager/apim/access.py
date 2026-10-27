"""Setup checks for an APIM service: it exists and finished provisioning, the reads and writes
the app makes on it, and its system-assigned identity (which calls Foundry); plus the fix that
turns that identity on."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any, Literal

from ..access import (
    FAILED,
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
from .service import ApimService

logger = logging.getLogger(__name__)

_APIM = "Microsoft.ApiManagement/service"
# Mirrors infra/modules/apim-rbac.bicep.
APIM_READ_ACTIONS = (
    f"{_APIM}/read",
    f"{_APIM}/backends/read",
    f"{_APIM}/namedValues/read",
    f"{_APIM}/apis/read",
    f"{_APIM}/apis/policies/read",
)
APIM_WRITE_ACTIONS = (
    f"{_APIM}/backends/write",
    f"{_APIM}/backends/delete",
    f"{_APIM}/namedValues/write",
    f"{_APIM}/namedValues/delete",
    f"{_APIM}/apis/write",
    # Only APIs this app created, when their pool is removed.
    f"{_APIM}/apis/delete",
    f"{_APIM}/apis/operations/write",
    f"{_APIM}/apis/policies/write",
)
APIM_SERVICE_WRITE = f"{_APIM}/write"
APIM_ROLE = "APIM Backend Pool Manager"
APIM_FALLBACK_ROLE = "API Management Service Contributor"
DISCOVERY_ROLE = "APIM Backend Pool Deployment Reader"


class ApimAccessChecker(AccessChecker):
    gateway_role = APIM_ROLE
    gateway_fallback_role = APIM_FALLBACK_ROLE
    discovery_role = DISCOVERY_ROLE
    identity_label = "APIM managed identity"

    def __init__(
        self,
        settings: Settings,
        arm: ArmClient,
        service: ApimService,
        discovered_accounts: Callable[[], Iterable[str]],
        credential: SignInCredential | None = None,
    ) -> None:
        super().__init__(settings, arm, service, discovered_accounts, credential)
        self._service = service

    def user_may_write(self, arm: ArmClient) -> bool:
        """Whether the user's own roles allow every APIM read and write the app makes."""
        permissions = self.permissions(arm, self._arm.resource_id)
        return not missing(permissions, (*APIM_READ_ACTIONS, *APIM_WRITE_ACTIONS))

    def forget_gateway(self) -> None:
        self._service.forget_service()

    def gateway_target(self) -> SetupCheck:
        """Which APIM service the app manages, and whether it has finished provisioning."""
        settings = self._settings
        fix: SetupFix | None = "select_gateway" if settings.can_choose_target else None
        kind: SetupKind = "gateway_target"
        subject = settings.apim_name
        try:
            found = self._arm.get_json("", api_version=settings.api_version)
        except ArmError as error:
            # Missing read access is reported by the APIM access check that follows.
            return SetupCheck(
                id=kind, kind=kind, subject=subject, status="unknown", detail=error.message
            )
        if found is None:
            return SetupCheck(
                id=kind, kind=kind, subject=subject, status="blocked", detail="not_found", fix=fix
            )
        # Only a new service is unusable while provisioning (v2 tiers take minutes, Developer
        # 30-40); an existing one keeps working while it is "Updating".
        properties = found[0].get("properties") or {}
        state = str(properties.get("provisioningState") or "")
        if state.casefold() in {"created", "activating"}:
            # properties.createdAtUtc stays 0001-01-01 until the service is up; ARM's own
            # systemData has the real time.
            started = (found[0].get("systemData") or {}).get("createdAt")
            return SetupCheck(
                id=kind,
                kind=kind,
                subject=subject,
                status="pending",
                detail=state,
                started_at=str(started) if started else None,
            )
        if state.casefold() == "failed":
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
        read = missing(permissions, APIM_READ_ACTIONS)
        write = missing(permissions, APIM_WRITE_ACTIONS)
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
        # Deployed, the app identity does the work; the user only needs leave to change things.
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
        read = missing(permissions, APIM_READ_ACTIONS)
        write = missing(permissions, APIM_WRITE_ACTIONS)
        status: SetupStatus = "blocked" if read else "action" if write else "ok"
        request = None
        if status != "ok":
            request = self.gateway_request(identity, "ServicePrincipal", command="azd provision")
        return SetupCheck(
            id=kind, kind=kind, status=status, missing=[*read, *write], request=request
        ), identity

    def gateway_principal(self) -> str | None:
        try:
            return str(self._service.apim_identity().get("principalId") or "") or None
        except ArmError as error:
            logger.info("Cannot read the APIM identity: %s", error.message)
            return None

    def gateway_identity(
        self, user_permissions: Permissions | None
    ) -> tuple[SetupCheck, str | None]:
        kind: SetupKind = "gateway_identity"
        try:
            identity = self._service.apim_identity()
        except ArmError as error:
            return SetupCheck(id=kind, kind=kind, status="unknown", detail=error.message), None
        principal = str(identity.get("principalId") or "") or None
        if principal:
            return SetupCheck(id=kind, kind=kind, status="ok", subject=principal), principal
        if str(identity.get("provisioningState") or "").casefold() in {"updating", "activating"}:
            return SetupCheck(id=kind, kind=kind, status="pending"), None
        can_enable = user_permissions is not None and allows(user_permissions, APIM_SERVICE_WRITE)
        scope = self._arm.resource_id
        return SetupCheck(
            id=kind,
            kind=kind,
            status="action",
            fix="enable_apim_identity" if can_enable else None,
            request=AdminRequest(
                assignee=self._settings.apim_name,
                assignee_id=None,
                role=None,
                scope=scope,
                command=f"az apim update -g {self._settings.resource_group} "
                f"-n {self._settings.apim_name} --enable-managed-identity true",
                portal_url=portal(scope),
            ),
        ), None

    def enable_apim_identity(self, token: UserToken | None, actor: str) -> SetupCheck:
        user = self.require_user(token)
        try:
            identity = self._service.apim_identity()
            if identity.get("principalId"):
                return SetupCheck(id="gateway_identity", kind="gateway_identity", status="ok")
            current = str(identity.get("type") or "None")
            assigned = dict(identity.get("userAssignedIdentities") or {})
            # Keep any user-assigned identities; PATCH replaces the whole identity block.
            body: dict[str, Any] = {
                "type": "SystemAssigned, UserAssigned"
                if "userassigned" in current.casefold()
                else "SystemAssigned"
            }
            if assigned:
                body["userAssignedIdentities"] = {key: {} for key in assigned}
            user.request(
                "PATCH", "", api_version=self._settings.api_version, json_body={"identity": body}
            )
        except ArmError as error:
            if error.status in {401, 403}:
                raise ServiceError(
                    403, f"Your Azure account cannot change the APIM service: {error.message}"
                ) from error
            raise ServiceError(502, f"Enabling the identity failed: {error.message}") from error
        logger.info(
            "%s enabled the system-assigned identity of %s", actor, self._settings.apim_name
        )
        self._service.forget_service()
        check, _ = self.gateway_identity(None)
        return check
