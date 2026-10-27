"""The error every layer raises for the API to turn into a JSON error with its status."""

from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
