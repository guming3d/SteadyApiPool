"""A small client for a LiteLLM proxy's admin API, authenticated with its master key.

Models (``/model/*``) are the gateway's model table, which needs a database
(``STORE_MODEL_IN_DB``); router settings (``/config/update``) are stored there too. Transient
429/5xx answers are retried with backoff, like ``arm.py``. The master key is sent only to the
gateway's own URL.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_TIMEOUT_SECONDS = 30.0
_KEY_PAGE_SIZE = 100


class LiteLLMError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _error(response: httpx.Response) -> LiteLLMError:
    """LiteLLM answers ``{"error": {"message": ...}}`` or FastAPI's ``{"detail": ...}``."""
    message = response.text[:500] or response.reason_phrase
    try:
        body = response.json()
    except ValueError:
        return LiteLLMError(response.status_code, message)
    if isinstance(body, Mapping):
        error = body.get("error")
        detail = body.get("detail")
        if isinstance(error, Mapping) and error.get("message"):
            message = str(error["message"])
        elif isinstance(error, str):
            message = error
        elif isinstance(detail, Mapping) and detail.get("error"):
            message = str(detail["error"])
        elif detail:
            message = str(detail)
    return LiteLLMError(response.status_code, message[:1000])


class LiteLLMClient:
    def __init__(
        self,
        base_url: str,
        master_key: str,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=_TIMEOUT_SECONDS,
            transport=transport,
            # Never follow a redirect with the master key to another host.
            follow_redirects=False,
            headers={"Authorization": f"Bearer {master_key}"},
        )

    def close(self) -> None:
        self._http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self._http.request(method, path, json=json_body, params=params)
            except httpx.TimeoutException as error:
                raise LiteLLMError(504, f"The gateway did not answer {path} in time") from error
            except httpx.HTTPError as error:
                raise LiteLLMError(502, f"Cannot reach the gateway: {error}") from error
            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                delay = min(2.0 ** (attempt - 1), 4.0)
                logger.warning(
                    "LiteLLM %s %s returned %s; retrying in %.1fs",
                    method,
                    path,
                    response.status_code,
                    delay,
                )
                self._sleep(delay)
                continue
            break
        if response.status_code >= 400:
            raise _error(response)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as error:
            raise LiteLLMError(502, f"The gateway answered {path} with something else") from error

    # -- health ------------------------------------------------------------------------

    def readiness(self) -> dict[str, Any]:
        """Whether the proxy's database is connected, and its version.

        Recent LiteLLM keeps the details (version, callbacks) behind the master key at
        ``/health/readiness/details``; plain ``/health/readiness`` then says only status and db.
        Older ones answer everything there.
        """
        try:
            body = self.request("GET", "/health/readiness/details")
        except LiteLLMError as error:
            if error.status not in {404, 405}:
                raise
            body = self.request("GET", "/health/readiness")
        return body if isinstance(body, dict) else {}

    # -- models ------------------------------------------------------------------------

    def models(self) -> list[dict[str, Any]]:
        """Every row of the model table (and of config.yaml); secrets are left out."""
        body = self.request("GET", "/model/info")
        rows = body.get("data") if isinstance(body, Mapping) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    def add_model(self, row: Mapping[str, Any]) -> None:
        self.request("POST", "/model/new", json_body=dict(row))

    def update_model(self, model_id: str, row: Mapping[str, Any]) -> None:
        """Merges ``litellm_params`` and ``model_info`` into the row; omitted keys stay."""
        self.request("PATCH", f"/model/{quote(model_id, safe='')}/update", json_body=dict(row))

    def delete_model(self, model_id: str) -> None:
        self.request("POST", "/model/delete", json_body={"id": model_id})

    # -- router settings ---------------------------------------------------------------

    def router_settings(self) -> dict[str, Any]:
        """The router settings in effect (config.yaml merged with the database), by name."""
        body = self.request("GET", "/router/settings")
        fields = body.get("fields") if isinstance(body, Mapping) else None
        return {
            str(field["field_name"]): field.get("field_value")
            for field in fields or []
            if isinstance(field, Mapping) and field.get("field_name")
        }

    def update_router_settings(self, settings: Mapping[str, Any]) -> None:
        self.request("POST", "/config/update", json_body={"router_settings": dict(settings)})

    # -- virtual keys ------------------------------------------------------------------

    def list_keys(self, *, key_alias: str | None = None) -> Iterator[dict[str, Any]]:
        params = {"return_full_object": "true", "size": str(_KEY_PAGE_SIZE)}
        if key_alias:
            params["key_alias"] = key_alias
        page = 1
        while True:
            body = self.request("GET", "/key/list", params={**params, "page": str(page)})
            if not isinstance(body, Mapping):
                return
            for key in body.get("keys") or []:
                if isinstance(key, dict):
                    yield key
            total_pages = body.get("total_pages")
            if not isinstance(total_pages, int) or page >= total_pages:
                return
            page += 1

    def generate_key(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Creates a virtual key. The answer is the only time the key itself is returned."""
        result = self.request("POST", "/key/generate", json_body=dict(body))
        if not isinstance(result, dict) or not result.get("key"):
            raise LiteLLMError(502, "The gateway created no key")
        return result

    def delete_keys(self, tokens: list[str]) -> None:
        """Deletes keys by their ID (``token``, the hash LiteLLM keeps)."""
        if tokens:
            self.request("POST", "/key/delete", json_body={"keys": tokens})
