"""The public retail-price reader recovers from transient failures without caching them."""

from __future__ import annotations

import httpx
import pytest

from pool_manager.cost import CostReader
from tests.litellm.test_app import _settings

SERVICE = "Azure Container Apps"
REGION = "swedencentral"
TRANSIENT_ERRORS = [429, 500, 502, 503, 504, httpx.ReadTimeout, httpx.ConnectError]


@pytest.mark.parametrize("error", TRANSIENT_ERRORS)
def test_retail_prices_retry_transient_failures(
    monkeypatch: pytest.MonkeyPatch, error: int | type[httpx.TransportError]
) -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []
    monkeypatch.setattr("pool_manager.cost.time.sleep", delays.append)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "Authorization" not in request.headers
        if len(requests) < 3:
            if isinstance(error, int):
                return httpx.Response(error)
            raise error("temporary failure", request=request)
        return httpx.Response(200, json={"Items": [{"retailPrice": 0.1}]})

    reader = CostReader(_settings(), transport=httpx.MockTransport(handle))
    try:
        assert reader.retail_prices(SERVICE, REGION, False) == [{"retailPrice": 0.1}]
        assert reader.retail_prices(SERVICE, REGION, False) == [{"retailPrice": 0.1}]
    finally:
        reader.close()
    assert len(requests) == 3
    assert all(request.url == requests[0].url for request in requests)
    assert delays == [1.0, 2.0]


@pytest.mark.parametrize(
    ("retry_after", "delay"), [("3", 3.0), ("300", 4.0), ("invalid", 1.0), ("-1", 1.0), ("0", 0.0)]
)
def test_retail_prices_bound_retry_after(
    monkeypatch: pytest.MonkeyPatch, retry_after: str, delay: float
) -> None:
    delays: list[float] = []
    monkeypatch.setattr("pool_manager.cost.time.sleep", delays.append)

    def handle(request: httpx.Request) -> httpx.Response:
        if not delays:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"Items": []})

    reader = CostReader(_settings(), transport=httpx.MockTransport(handle))
    try:
        assert reader.retail_prices(SERVICE, REGION, False) == []
    finally:
        reader.close()
    assert delays == [delay]


@pytest.mark.parametrize("error", [503, httpx.ReadTimeout])
def test_failed_retail_prices_are_bounded_and_not_cached(
    monkeypatch: pytest.MonkeyPatch, error: int | type[httpx.TransportError]
) -> None:
    calls = 0
    delays: list[float] = []
    monkeypatch.setattr("pool_manager.cost.time.sleep", delays.append)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 3:
            if isinstance(error, int):
                return httpx.Response(error)
            raise error("temporary failure", request=request)
        return httpx.Response(200, json={"Items": [{"retailPrice": 0.1}]})

    reader = CostReader(_settings(), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(httpx.HTTPError):
            reader.retail_prices(SERVICE, REGION, False)
        assert calls == 3
        assert reader.retail_prices(SERVICE, REGION, False) == [{"retailPrice": 0.1}]
    finally:
        reader.close()
    assert calls == 4
    assert delays == [1.0, 2.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_retail_prices_do_not_retry_permanent_errors(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls = 0
    delays: list[float] = []
    monkeypatch.setattr("pool_manager.cost.time.sleep", delays.append)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    reader = CostReader(_settings(), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            reader.retail_prices(SERVICE, REGION, False)
    finally:
        reader.close()
    assert calls == 1
    assert delays == []


def test_retail_prices_retry_the_failed_page_without_duplicating_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []
    monkeypatch.setattr("pool_manager.cost.time.sleep", delays.append)
    next_link = "https://prices.azure.com/api/retail/prices?$skip=1000"

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200, json={"Items": [{"retailPrice": 0.1}], "NextPageLink": next_link}
            )
        assert str(request.url) == next_link
        if len(requests) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"Items": [{"retailPrice": 0.2}]})

    reader = CostReader(_settings(), transport=httpx.MockTransport(handle))
    try:
        assert reader.retail_prices(SERVICE, REGION, False) == [
            {"retailPrice": 0.1},
            {"retailPrice": 0.2},
        ]
    finally:
        reader.close()
    assert len(requests) == 3
    assert delays == [1.0]
