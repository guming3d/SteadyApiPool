"""Setup checks for a LiteLLM gateway: its Container App (or deployment), the master key, the
gateway's own health and the user-assigned identity it calls Foundry as."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any, Literal

from ..access import FAILED, AccessChecker, Permissions, SignInCredential, identity_of, missing
from ..access import portal as _portal
from ..arm import ArmClient, ArmError
from ..config import Settings
from ..errors import ServiceError
from ..models import AdminRequest, SetupCheck, SetupFix, SetupIdentity, SetupKind, SetupStatus
from .client import LiteLLMError
from .gateway import GatewayUnavailable
from .gateway_deploy import GatewayDeployer
from .service import LiteLLMService

logger = logging.getLogger(__name__)

# Reading the gateway's Container App and its master key is all the app needs in Azure to
# manage pools: everything else goes through LiteLLM's admin API. Mirrors
# infra/modules/litellm-rbac.bicep.
GATEWAY_ACTIONS = (
    "Microsoft.App/containerApps/read",
    "Microsoft.App/containerApps/listSecrets/action",
)
GATEWAY_ROLE = "AI Gateway Pool Manager"
GATEWAY_FALLBACK_ROLE = "Container Apps Contributor"
DISCOVERY_ROLE = "AI Gateway Pool Deployment Reader"
# Its first deployment failed before the Container App existed: there is nothing to check yet.
_DEPLOY_FAILED = "DeployFailed"


class LiteLLMAccessChecker(AccessChecker):
    gateway_role = GATEWAY_ROLE
    gateway_fallback_role = GATEWAY_FALLBACK_ROLE
    discovery_role = DISCOVERY_ROLE
    identity_label = "gateway managed identity"
    checks_health = True

    def __init__(
        self,
        settings: Settings,
        arm: ArmClient,
        service: LiteLLMService,
        discovered_accounts: Callable[[], Iterable[str]],
        credential: SignInCredential | None = None,
    ) -> None:
        super().__init__(settings, arm, service, discovered_accounts, credential)
        self._service = service
        self._deployer = GatewayDeployer(settings)

    def user_may_write(self, arm: ArmClient) -> bool:
        # An external gateway has no Azure resource to hold roles on: owners only.
        if self._settings.external:
            return False
        return not missing(self.permissions(arm, self._arm.resource_id), GATEWAY_ACTIONS)

    def forget_gateway(self) -> None:
        self._service.gateway.forget()

    def gateway_target(self) -> SetupCheck:
        settings = self._settings
        fix: SetupFix | None = "select_gateway" if settings.can_choose_target else None
        kind: SetupKind = "gateway_target"
        subject = settings.gateway_name
        if settings.gateway_provisioning:
            # A new AKS gateway the app is still installing: the gateway step's progress
            # (its deploy status) drives the install on.
            return SetupCheck(
                id=kind,
                kind=kind,
                subject=subject,
                status="pending",
                detail="DeployingAks",
                started_at=settings.gateway_started_at or None,
            )
        if settings.external:
            # Nothing in Azure to look at; the health check reaches the gateway itself.
            return SetupCheck(id=kind, kind=kind, subject=subject, status="ok")
        try:
            found = self._arm.get_json("", api_version=settings.container_apps_api_version)
            state = (
                str((found[0].get("properties") or {}).get("provisioningState") or "")
                if found
                else ""
            )
            deployment = (
                self._deployer.status(
                    self._arm, settings.subscription_id, settings.resource_group, subject
                )
                if found is None or state.casefold() in {"inprogress", "failed"}
                else None
            )
        except (ArmError, ServiceError) as error:
            # Missing read access is reported by the gateway access check that follows.
            return SetupCheck(
                id=kind, kind=kind, subject=subject, status="unknown", detail=error.message
            )
        if deployment is not None and deployment.state == "running":
            return SetupCheck(
                id=kind,
                kind=kind,
                subject=subject,
                status="pending",
                detail="Deploying",
                started_at=deployment.started_at,
            )
        if deployment is not None and deployment.state == "failed":
            return SetupCheck(
                id=kind,
                kind=kind,
                subject=subject,
                status="action",
                detail=FAILED if found else _DEPLOY_FAILED,
                missing=[deployment.error] if deployment.error else [],
                fix=fix,
            )
        if found is None:
            return SetupCheck(
                id=kind, kind=kind, subject=subject, status="blocked", detail="not_found", fix=fix
            )
        if state.casefold() == "inprogress":
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

    def gateway_health(self) -> SetupCheck:
        """The gateway itself: reachable, its database connected, the master key accepted."""
        kind: SetupKind = "gateway_health"
        subject = self._settings.gateway_name

        def check(status: SetupStatus, **values: Any) -> SetupCheck:
            return SetupCheck(id=kind, kind=kind, subject=subject, status=status, **values)

        fix: SetupFix | None = (
            "connect_gateway"
            if self._settings.external and self._settings.can_choose_target
            else None
        )
        try:
            client = self._service.gateway.client()
            health = client.readiness()
            client.models()  # Needs the master key; readiness does not.
        except GatewayUnavailable as error:
            return check("blocked", detail=error.message, fix=fix if error.status == 409 else None)
        except LiteLLMError as error:
            if error.status in {401, 403}:
                return check("blocked", detail="master_key_refused", fix=fix)
            return check("blocked", detail=error.message)
        database = str(health.get("db") or "")
        if database and database.casefold() != "connected":
            return check("blocked", detail=f"database {database}")
        return check("ok", detail=str(health.get("litellm_version") or "") or None)

    def gateway_access(
        self, arm: ArmClient | None, identity: SetupIdentity | None, owner: bool
    ) -> tuple[SetupCheck, Permissions | None]:
        kind: SetupKind = "gateway_access"
        granted: Literal["owners", "azure_rbac"] | None
        if self._settings.external:
            # The master key is the access; the health check tries it.
            granted = "owners" if owner and not self._settings.operator_is_user else None
            return SetupCheck(id=kind, kind=kind, status="ok", granted_by=granted), None
        if arm is None:
            if owner:
                return SetupCheck(id=kind, kind=kind, status="ok", granted_by="owners"), None
            return SetupCheck(id=kind, kind=kind, status="unknown"), None
        try:
            permissions = self.permissions(arm, self._arm.resource_id)
        except ArmError as error:
            status: SetupStatus = "blocked" if self._settings.operator_is_user else "unknown"
            return SetupCheck(id=kind, kind=kind, status=status, detail=error.message), None
        gaps = missing(permissions, GATEWAY_ACTIONS)
        if self._settings.operator_is_user:
            # Without the master key nothing can be managed.
            return SetupCheck(
                id=kind,
                kind=kind,
                status="blocked" if gaps else "ok",
                missing=gaps,
                request=self.gateway_request(identity, "User") if gaps else None,
            ), permissions
        # Deployed, the app identity does the work; the user only needs leave to change things.
        granted = "owners" if owner else "azure_rbac" if not gaps else None
        return SetupCheck(
            id=kind,
            kind=kind,
            status="ok" if granted else "action",
            missing=[] if granted else gaps,
            request=None if granted else self.gateway_request(identity, "User"),
            granted_by=granted,
        ), permissions

    def app_gateway(self) -> tuple[SetupCheck, SetupIdentity | None]:
        kind: SetupKind = "app_gateway"
        try:
            identity = identity_of(self._arm.token_claims())
            if self._settings.external:
                return SetupCheck(id=kind, kind=kind, status="ok"), identity
            permissions = self.permissions(self._arm, self._arm.resource_id)
        except ArmError as error:
            return SetupCheck(id=kind, kind=kind, status="blocked", detail=error.message), None
        gaps = missing(permissions, GATEWAY_ACTIONS)
        request = (
            self.gateway_request(identity, "ServicePrincipal", command="azd provision")
            if gaps
            else None
        )
        return SetupCheck(
            id=kind,
            kind=kind,
            status="blocked" if gaps else "ok",
            missing=gaps,
            request=request,
        ), identity

    def gateway_principal(self) -> str | None:
        try:
            return self._service.gateway.info().principal_id
        except GatewayUnavailable as error:
            logger.info("Cannot read the gateway's identity: %s", error.message)
            return None

    def gateway_identity(
        self, user_permissions: Permissions | None
    ) -> tuple[SetupCheck, str | None]:
        kind: SetupKind = "gateway_identity"
        try:
            principal = self._service.gateway.info().principal_id
        except GatewayUnavailable as error:
            return SetupCheck(id=kind, kind=kind, status="unknown", detail=error.message), None
        if principal:
            return SetupCheck(id=kind, kind=kind, status="ok", subject=principal), principal
        settings = self._settings
        if settings.external:
            # Without it Foundry access can't be checked; the gateway may still work.
            return SetupCheck(
                id=kind,
                kind=kind,
                status="action",
                detail="external_unknown",
                fix="connect_gateway" if settings.can_choose_target else None,
            ), None
        scope = self._arm.resource_id
        group, name = settings.resource_group, settings.gateway_name
        return SetupCheck(
            id=kind,
            kind=kind,
            status="action",
            request=AdminRequest(
                assignee=name,
                assignee_id=None,
                role=None,
                scope=scope,
                command=(
                    f"az identity create -g {group} -n id-{name}\n"
                    f"az containerapp identity assign -g {group} -n {name} "
                    f"--user-assigned id-{name}\n"
                    f"az containerapp update -g {group} -n {name} "
                    "--set-env-vars AZURE_CLIENT_ID=<the identity's client ID>"
                ),
                portal_url=_portal(scope, "identity"),
            ),
        ), None
