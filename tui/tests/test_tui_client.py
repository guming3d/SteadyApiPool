"""The TUI's API client against the real backend routes."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import tui_client
from tests.fake_apim import FakeApim
from tests.test_api_test import API

from pool_tui.client import ApiClient, ApiError


def test_reads_and_writes_go_through_the_backend(seeded: FakeApim) -> None:
    async def scenario() -> None:
        client = tui_client()
        pools = await client.pools()
        assert [pool["id"] for pool in pools] == [API]
        pool = await client.pool(API)
        # Writes carry the CSRF header; without it the backend would answer 403.
        saved = await client.save_pool(
            "pool-copy",
            {"members": pool["members"], "title": "copy", "session_affinity": False, "etag": None},
        )
        assert saved["id"] == "pool-copy"
        assert "pool-copy" in seeded.backends
        await client.aclose()

    asyncio.run(scenario())


def test_errors_carry_the_backends_message(seeded: FakeApim) -> None:
    async def scenario() -> None:
        client = tui_client()
        with pytest.raises(ApiError) as missing:
            await client.pool("nope")
        assert missing.value.status == 404
        with pytest.raises(ApiError) as invalid:
            await client.save_pool("pool-bad", {"members": [], "etag": None})
        assert invalid.value.status == 422
        assert "members" in invalid.value.message
        await client.aclose()

    asyncio.run(scenario())


def test_unreachable_backend_is_a_clear_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async def scenario() -> None:
        client = ApiClient("http://127.0.0.1:9", transport=httpx.MockTransport(refuse))
        with pytest.raises(ApiError) as error:
            await client.me()
        assert error.value.status == 0
        assert "Cannot reach the backend" in error.value.message
        await client.aclose()

    asyncio.run(scenario())
