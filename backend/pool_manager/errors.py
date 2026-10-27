"""The error every layer raises for the API to turn into a JSON error with its status."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .arm import ArmError


class ServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def arm_failure(error: ArmError, action: str) -> ServiceError:
    """An ARM call made with the user's own rights failed while trying to ``action``."""
    if error.status == 401:
        return ServiceError(401, f"Sign in again to {action}: {error.message}")
    if error.status == 403:
        return ServiceError(403, f"Your Azure account cannot {action}: {error.message}")
    if error.status in {400, 404, 409}:
        return ServiceError(error.status, error.message)
    return ServiceError(502, f"Azure returned {error.status} {error.code}: {error.message}")
