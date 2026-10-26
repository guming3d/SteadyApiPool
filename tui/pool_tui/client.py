"""Async client for the pool manager's ``/api``: the same endpoints the web UI calls.

Every rule (ETags, protected prefixes, the same-model check, who may write) stays in the
backend; the TUI only renders what it answers. Writes carry ``X-Pool-Manager: 1``, the CSRF
header the backend requires of every non-GET request.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

Json = dict[str, Any]
WRITE_HEADER = "X-Pool-Manager"
# Test calls wait up to 150 s on the gateway; leave room for the backend around them.
_TIMEOUT = httpx.Timeout(180.0, connect=5.0)
# The browser sign-in waits for the user.
_SIGN_IN_TIMEOUT = httpx.Timeout(600.0, connect=5.0)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _detail(response: httpx.Response) -> str:
    """The backend's error message: ``detail`` as text, or FastAPI's validation list."""
    try:
        detail = response.json().get("detail")
    except ValueError:
        return f"{response.status_code} {response.reason_phrase}"
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        return "; ".join(
            f"{'.'.join(str(part) for part in (item.get('loc') or [])[1:])}: {item.get('msg')}"
            for item in detail
            if isinstance(item, dict)
        )
    return f"{response.status_code} {response.reason_phrase}"


def _id(value: str) -> str:
    return quote(value, safe="")


class ApiClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url, transport=transport, timeout=_TIMEOUT
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> Any:
        headers = {WRITE_HEADER: "1"} if method != "GET" else {}
        try:
            response = await self._http.request(
                method,
                f"/api{path}",
                json=json,
                params=params,
                headers=headers,
                timeout=timeout or _TIMEOUT,
            )
        except httpx.TimeoutException as error:
            raise ApiError(0, f"The backend did not answer in time ({path})") from error
        except httpx.HTTPError as error:
            raise ApiError(0, f"Cannot reach the backend at {self.base_url}: {error}") from error
        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    @staticmethod
    def _etag(etag: str | None) -> dict[str, str] | None:
        return {"etag": etag} if etag else None

    # -- identity and setup -------------------------------------------------------------

    async def me(self) -> Json:
        result: Json = await self._request("GET", "/me")
        return result

    async def capabilities(self) -> Json:
        result: Json = await self._request("GET", "/capabilities")
        return result

    async def setup(self, refresh: bool = False) -> Json:
        params = {"refresh": "true"} if refresh else None
        result: Json = await self._request("GET", "/setup", params=params)
        return result

    async def sign_in(self, tenant_id: str | None = None) -> None:
        body = {"tenant_id": tenant_id} if tenant_id else None
        await self._request("POST", "/setup/sign-in", json=body, timeout=_SIGN_IN_TIMEOUT)

    async def start_device_code(self, tenant_id: str | None = None) -> Json:
        """For machines without a local browser; returns the code to enter elsewhere."""
        body = {"tenant_id": tenant_id} if tenant_id else None
        result: Json = await self._request("POST", "/setup/sign-in/device-code", json=body)
        return result

    async def device_code_status(self) -> Json:
        result: Json = await self._request("GET", "/setup/sign-in/device-code")
        return result

    async def sign_out(self) -> None:
        """Forgets the app's own sign-in; az, azd and PowerShell sign-ins stay."""
        await self._request("POST", "/setup/sign-out")

    async def enable_apim_identity(self) -> Json:
        result: Json = await self._request("POST", "/setup/apim-identity")
        return result

    async def grant_foundry_access(self, account_ids: list[str]) -> list[Json]:
        result: list[Json] = await self._request(
            "POST", "/setup/foundry-access/grant", json={"account_ids": account_ids}
        )
        return result

    # -- the APIM service ---------------------------------------------------------------

    async def target(self) -> Json:
        result: Json = await self._request("GET", "/target")
        return result

    async def tenants(self) -> list[Json]:
        result: list[Json] = await self._request("GET", "/target/tenants")
        return result

    async def subscriptions(self, tenant_id: str | None) -> list[Json]:
        params = {"tenant_id": tenant_id} if tenant_id else None
        result: list[Json] = await self._request("GET", "/target/subscriptions", params=params)
        return result

    async def services(self, tenant_id: str | None, subscription_id: str) -> list[Json]:
        params = {"subscription_id": subscription_id}
        if tenant_id:
            params["tenant_id"] = tenant_id
        result: list[Json] = await self._request("GET", "/target/services", params=params)
        return result

    async def select_target(self, body: Json) -> Json:
        result: Json = await self._request("PUT", "/target", json=body)
        return result

    async def delete_check(self, body: Json) -> Json:
        result: Json = await self._request("POST", "/target/services/delete-check", json=body)
        return result

    async def delete_service(self, body: Json) -> None:
        await self._request("POST", "/target/services/delete", json=body)

    async def deleted_services(self, tenant_id: str | None, subscription_id: str) -> list[Json]:
        params = {"subscription_id": subscription_id}
        if tenant_id:
            params["tenant_id"] = tenant_id
        result: list[Json] = await self._request("GET", "/target/deleted", params=params)
        return result

    async def restore_service(self, body: Json) -> Json:
        result: Json = await self._request("POST", "/target/deleted/restore", json=body)
        return result

    # -- backends and pools -------------------------------------------------------------

    async def backends(self) -> list[Json]:
        result: list[Json] = await self._request("GET", "/backends")
        return result

    async def delete_backend(self, backend_id: str, etag: str | None) -> None:
        await self._request("DELETE", f"/backends/{_id(backend_id)}", params=self._etag(etag))

    async def pools(self) -> list[Json]:
        result: list[Json] = await self._request("GET", "/pools")
        return result

    async def pool(self, pool_id: str) -> Json:
        result: Json = await self._request("GET", f"/pools/{_id(pool_id)}")
        return result

    async def save_pool(self, pool_id: str, body: Json) -> Json:
        result: Json = await self._request("PUT", f"/pools/{_id(pool_id)}", json=body)
        return result

    async def delete_pool(self, pool_id: str, etag: str | None) -> None:
        await self._request("DELETE", f"/pools/{_id(pool_id)}", params=self._etag(etag))

    async def check_models(self, members: list[str]) -> Json:
        result: Json = await self._request("POST", "/pools/model-check", json={"members": members})
        return result

    async def create_pool_api(self, pool_id: str, body: Json) -> Json:
        result: Json = await self._request("POST", f"/pools/{_id(pool_id)}/api", json=body)
        return result

    async def pool_metrics(self, pool_id: str, range_: str, refresh: bool = False) -> Json:
        params = {"range": range_, **({"refresh": "true"} if refresh else {})}
        result: Json = await self._request("GET", f"/pools/{_id(pool_id)}/metrics", params=params)
        return result

    # -- APIs ---------------------------------------------------------------------------

    async def apis(self) -> list[Json]:
        result: list[Json] = await self._request("GET", "/apis")
        return result

    async def bind(self, api_id: str, body: Json) -> Json:
        result: Json = await self._request("PUT", f"/apis/{_id(api_id)}/binding", json=body)
        return result

    async def unbind(self, api_id: str, etag: str | None) -> None:
        await self._request("DELETE", f"/apis/{_id(api_id)}/binding", params=self._etag(etag))

    async def api_keys(self, api_id: str) -> list[Json]:
        result: list[Json] = await self._request("GET", f"/apis/{_id(api_id)}/keys")
        return result

    async def create_api_key(self, api_id: str) -> Json:
        result: Json = await self._request("POST", f"/apis/{_id(api_id)}/keys")
        return result

    async def read_api_key(self, api_id: str, key_id: str) -> str:
        secret: Json = await self._request("POST", f"/apis/{_id(api_id)}/keys/{_id(key_id)}/secret")
        return str(secret["primary_key"])

    async def test_api(self, api_id: str, body: Json) -> Json:
        result: Json = await self._request("POST", f"/apis/{_id(api_id)}/test", json=body)
        return result

    # -- deployments --------------------------------------------------------------------

    async def deployments(self, refresh: bool = False) -> list[Json]:
        params = {"refresh": "true"} if refresh else None
        result: list[Json] = await self._request("GET", "/deployments", params=params)
        return result

    async def import_deployments(self, refs: list[Json]) -> Json:
        result: Json = await self._request(
            "POST", "/deployments/import", json={"deployments": refs}
        )
        return result

    # Deploying models runs with the user's own Azure rights (backend model_deploy.py).
    async def foundry_accounts(self) -> list[Json]:
        result: list[Json] = await self._request("GET", "/foundry/accounts")
        return result

    async def foundry_models(self, account_id: str) -> list[Json]:
        params = {"account_id": account_id}
        result: list[Json] = await self._request("GET", "/foundry/models", params=params)
        return result

    async def foundry_capacity(self, account_id: str, model: str, version: str) -> Json:
        params = {"account_id": account_id, "model": model, "version": version}
        result: Json = await self._request("GET", "/foundry/capacity", params=params)
        return result

    async def create_foundry_deployment(self, body: Json) -> Json:
        result: Json = await self._request("POST", "/foundry/deployments", json=body)
        return result

    async def foundry_deployment_status(self, account_id: str, deployment: str) -> Json:
        params = {"account_id": account_id, "deployment": deployment}
        result: Json = await self._request("GET", "/foundry/deployments/status", params=params)
        return result
