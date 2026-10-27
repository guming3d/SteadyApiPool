"""Creates OpenAI model deployments in existing Foundry / Azure OpenAI accounts.

Everything here runs with the **user's** ARM client (``AccessChecker.require_user``), never the
app identity: Azure decides whether the user may read an account's models and write its
deployments (``Microsoft.CognitiveServices/accounts/deployments/write``, e.g. Cognitive Services
Contributor), and the app identity keeps its read-only discovery role.

Only accounts the discovery scan finds (``discovery_subscriptions``, kinds OpenAI and
AIServices) take a deployment, and the model, version, deployment type and capacity are checked
against what that account's ``models`` list offers before anything is written. Partner models
(Anthropic, ...) are not offered: they sit on other APIs that pools can't front yet.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, get_args

from .arm import ArmClient, ArmError, segment
from .config import Settings
from .discovery import DeploymentDiscovery
from .errors import ServiceError
from .models import (
    DeployableModel,
    DeploymentSku,
    FoundryAccountView,
    FoundryDeploymentCreate,
    FoundryDeploymentStatus,
    ModelCapacity,
    ModelSkuOption,
)

logger = logging.getLogger(__name__)

# In the order Azure documents them: Global, Data Zone, then single-region Standard.
_SKU_ORDER: tuple[str, ...] = get_args(DeploymentSku)
_SKUS = frozenset(_SKU_ORDER)
_MODEL_FORMAT = "OpenAI"
# Not deployable any more; Legacy and Deprecating still are, and are labelled as such.
_RETIRED = frozenset({"deprecated"})


def _translate(error: ArmError) -> ServiceError:
    # 403 means the user lacks rights on that account: say so instead of a generic failure.
    if error.status == 403:
        return ServiceError(
            403,
            "Your Azure account can't do this on that account. Deploying needs "
            "Microsoft.CognitiveServices/accounts/deployments/write (for example Cognitive "
            f"Services Contributor). {error.message}",
        )
    return ServiceError(error.status if 400 <= error.status < 600 else 502, error.message)


def _int(value: object, fallback: int) -> int:
    return value if isinstance(value, int) and value > 0 else fallback


def _sku_option(sku: Mapping[str, Any]) -> ModelSkuOption | None:
    name = str(sku.get("name") or "")
    # Types are listed again for fine-tuned deployments (usage OpenAI.X.<model>-finetune).
    if name not in _SKUS or "finetune" in str(sku.get("usageName") or "").casefold():
        return None
    capacity = sku.get("capacity") or {}
    maximum = _int(capacity.get("maximum"), 1_000_000)
    minimum = min(_int(capacity.get("minimum"), 1), maximum)
    default = min(max(_int(capacity.get("default"), minimum), minimum), maximum)
    return ModelSkuOption(
        name=name,  # type: ignore[arg-type]
        default_capacity=default,
        min_capacity=minimum,
        max_capacity=maximum,
        step=_int(capacity.get("step"), 1),
    )


def parse_models(items: Sequence[Mapping[str, Any]]) -> list[DeployableModel]:
    """The account's OpenAI models that have a pay-per-token deployment type, newest first."""
    models = []
    for item in items:
        if str(item.get("format") or "") != _MODEL_FORMAT:
            continue
        lifecycle = item.get("lifecycleStatus")
        if str(lifecycle or "").casefold() in _RETIRED:
            continue
        skus: list[ModelSkuOption] = []
        for sku in item.get("skus") or []:
            option = _sku_option(sku)
            if option and all(option.name != seen.name for seen in skus):
                skus.append(option)
        name, version = str(item.get("name") or ""), str(item.get("version") or "")
        if not skus or not name or not version:
            continue
        capabilities = item.get("capabilities") or {}
        deprecation = item.get("deprecation") or {}
        models.append(
            DeployableModel(
                name=name,
                version=version,
                is_default_version=item.get("isDefaultVersion") is True,
                lifecycle_status=str(lifecycle) if lifecycle else None,
                retires_on=str(deprecation["inference"]) if deprecation.get("inference") else None,
                # What the model does; fine-tuning flags don't matter for a base deployment.
                capabilities=sorted(
                    key
                    for key, value in capabilities.items()
                    if value == "true" and "finetune" not in key.casefold()
                ),
                skus=sorted(skus, key=lambda option: _SKU_ORDER.index(option.name)),
            )
        )
    # One model's versions together: the default first, then newer versions before older.
    models.sort(key=lambda model: model.version, reverse=True)
    models.sort(key=lambda model: (model.name, not model.is_default_version))
    return models


class ModelDeployer:
    def __init__(self, settings: Settings, discovery: DeploymentDiscovery) -> None:
        self._settings = settings
        self._discovery = discovery

    @property
    def _api_version(self) -> str:
        return self._settings.cognitive_api_version

    def accounts(self) -> list[FoundryAccountView]:
        views = []
        for account in self._discovery.accounts():
            account_id = str(account.get("id") or "")
            parts = account_id.split("/")
            if len(parts) < 9:
                continue
            views.append(
                FoundryAccountView(
                    id=account_id,
                    name=str(account.get("name") or parts[-1]),
                    kind=str(account.get("kind") or ""),
                    location=str(account.get("location") or ""),
                    resource_group=parts[4],
                    subscription_id=parts[2],
                )
            )
        return sorted(views, key=lambda view: (view.name.casefold(), view.id))

    def _account(self, account_id: str) -> FoundryAccountView:
        """Only accounts the discovery scan finds; never an arbitrary ARM ID from the client."""
        account = next(
            (view for view in self.accounts() if view.id.casefold() == account_id.casefold()),
            None,
        )
        if account is None:
            raise ServiceError(
                404,
                "That account isn't a Foundry / Azure OpenAI account in the scanned subscriptions",
            )
        return account

    def models(self, user: ArmClient, account_id: str) -> list[DeployableModel]:
        account = self._account(account_id)
        try:
            items = list(user.list_arm(f"{account.id}/models", api_version=self._api_version))
        except ArmError as error:
            raise _translate(error) from error
        return parse_models(items)

    def capacity(self, user: ArmClient, account_id: str, model: str, version: str) -> ModelCapacity:
        account = self._account(account_id)
        path = (
            f"/subscriptions/{segment(account.subscription_id)}/providers"
            f"/Microsoft.CognitiveServices/locations/{segment(account.location)}/modelCapacities"
        )
        params = {"modelFormat": _MODEL_FORMAT, "modelName": model, "modelVersion": version}
        try:
            items = list(user.list_arm(path, api_version=self._api_version, params=params))
        except ArmError as error:
            # Reading quota needs its own permission; the form works without the number.
            return ModelCapacity(available={}, error=error.message)
        available: dict[str, int] = {}
        for item in items:
            properties = item.get("properties") or {}
            sku = str(properties.get("skuName") or "")
            value = properties.get("availableCapacity")
            if sku in _SKUS and isinstance(value, int):
                available[sku] = max(available.get(sku, 0), value)
        return ModelCapacity(available=available)

    def create(
        self, user: ArmClient, request: FoundryDeploymentCreate, actor: str
    ) -> FoundryDeploymentStatus:
        account = self._account(request.account_id)
        offered = {(model.name, model.version): model for model in self.models(user, account.id)}
        model = offered.get((request.model_name, request.model_version))
        if model is None:
            raise ServiceError(
                400, f"{account.name} doesn't offer {request.model_name} {request.model_version}"
            )
        sku = next((option for option in model.skus if option.name == request.sku), None)
        if sku is None:
            raise ServiceError(
                400, f"{request.model_name} {request.model_version} has no {request.sku} deployment"
            )
        if not sku.min_capacity <= request.capacity <= sku.max_capacity:
            raise ServiceError(
                400, f"Capacity must be between {sku.min_capacity} and {sku.max_capacity}"
            )
        path = f"{account.id}/deployments/{segment(request.deployment)}"
        try:
            # Never replace a deployment: an existing name is the user's to keep.
            if user.get_arm_json(path, api_version=self._api_version) is not None:
                raise ServiceError(
                    409, f"{account.name} already has a deployment {request.deployment}"
                )
            response = user.request(
                "PUT",
                user.arm_url(path),
                api_version=self._api_version,
                json_body={
                    "sku": {"name": request.sku, "capacity": request.capacity},
                    "properties": {
                        "model": {
                            "format": _MODEL_FORMAT,
                            "name": request.model_name,
                            "version": request.model_version,
                        },
                        "versionUpgradeOption": request.version_upgrade,
                    },
                },
            )
        except ArmError as error:
            raise _translate(error) from error
        logger.info(
            "%s started deployment %s (%s %s, %s x%d) in %s",
            actor,
            request.deployment,
            request.model_name,
            request.model_version,
            request.sku,
            request.capacity,
            account.name,
        )
        # Long-running: the client polls status() until Succeeded or Failed.
        return self._status(account.id, request.deployment, response.json())

    def status(self, user: ArmClient, account_id: str, deployment: str) -> FoundryDeploymentStatus:
        account = self._account(account_id)
        path = f"{account.id}/deployments/{segment(deployment)}"
        try:
            body = user.get_arm_json(path, api_version=self._api_version)
        except ArmError as error:
            raise _translate(error) from error
        if body is None:
            raise ServiceError(404, f"{account.name} has no deployment {deployment}")
        status = self._status(account.id, deployment, body)
        if status.state == "Succeeded":
            # The next deployments list scans again, so the new deployment shows up.
            self._discovery.forget()
        return status

    @staticmethod
    def _status(
        account_id: str, deployment: str, body: Mapping[str, Any]
    ) -> FoundryDeploymentStatus:
        properties = body.get("properties") or {}
        state = str(properties.get("provisioningState") or "Accepted")
        error = None
        if state.casefold() in {"failed", "canceled"}:
            detail = properties.get("error") or body.get("error") or {}
            error = str(detail.get("message") or "") or f"The deployment {state.casefold()}"
        return FoundryDeploymentStatus(
            account_id=account_id, deployment=deployment, state=state, error=error
        )
