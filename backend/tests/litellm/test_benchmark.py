"""The load test: the numbers, the engine against the fake gateway, and its routes."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pool_manager.app import app
from pool_manager.litellm.benchmark import (
    BenchmarkRunner,
    Sample,
    prompt,
    report,
    run_benchmark,
    summarize,
    verdict,
)
from pool_manager.litellm.models import BenchmarkSpec
from tests.fake_azure import FakeAzure
from tests.litellm.fake_litellm import URL, FakeLiteLLM
from tests.litellm.test_app import WRITE, _client, _settings, create_pool

CHAT = f"{URL}/v1/chat/completions"


def _key(litellm: FakeLiteLLM, models: list[str] | None = None) -> str:
    created = litellm._generate({"key_alias": "bench", "models": models or ["gpt6"]})
    return str(created.json()["key"])


def _run(spec: BenchmarkSpec, litellm: FakeLiteLLM, key: str, **options: Any) -> list[Sample]:
    samples: list[Sample] = []
    asyncio.run(
        run_benchmark(spec, CHAT, key, "gpt6", samples, transport=litellm.transport(), **options)
    )
    return samples


@pytest.fixture
def litellm() -> FakeLiteLLM:
    fake = FakeLiteLLM()
    fake.add_row("gpt6", "azure/gpt6", "https://east.openai.azure.com", id="east", order=1)
    fake.add_row("gpt6", "azure/gpt6", "https://west.openai.azure.com", id="west", order=2)
    return fake


# -- the numbers -------------------------------------------------------------------------------


def test_percentiles_errors_members_and_the_timeline() -> None:
    samples = [
        Sample(
            start=float((index - 1) // 50),
            latency_ms=float(index),
            ttft_ms=index / 2,
            status=200,
            member="east" if index % 4 else "west",
            fallbacks=1 if index <= 3 else 0,
            prompt_tokens=10,
            completion_tokens=5,
        )
        for index in range(1, 101)
    ]
    samples += [
        Sample(start=0.5, latency_ms=3.0, status=429),
        Sample(start=2.5, latency_ms=3.0, status=500, member="west"),
        Sample(start=2.9, latency_ms=9000.0, error="timeout"),
    ]
    spec = BenchmarkSpec(users=4, duration_seconds=3, ramp_up_seconds=2)

    summary = summarize(samples, 3.0, spec)

    assert summary.requests == 103 and summary.successes == 100
    assert summary.throttled == 1
    assert summary.errors == {"500": 1, "timeout": 1}
    assert (summary.latency_p50_ms, summary.latency_p90_ms) == (50.0, 90.0)
    assert (summary.latency_p95_ms, summary.latency_p99_ms, summary.latency_max_ms) == (
        95.0,
        99.0,
        100.0,
    )
    assert summary.ttft_p50_ms == 25.0
    assert summary.requests_per_second == round(103 / 3, 2)
    assert summary.success_rate == round(100 / 103, 4)
    assert (summary.prompt_tokens, summary.completion_tokens) == (1000, 500)
    assert summary.output_tokens_per_second == round(500 / 3, 1)
    assert summary.failovers == 3
    east, west = summary.members
    assert (east.member, east.requests, east.errors) == ("east", 75, 0)
    assert (west.member, west.requests, west.errors) == ("west", 26, 1)
    assert east.share == round(75 / 101, 4)
    assert [point.second for point in summary.timeline] == [0, 1, 2]
    # Counted when they finished: the timeout started at 2.9 s and ended after the run.
    assert [point.requests for point in summary.timeline] == [51, 50, 2]
    assert summary.timeline[0].throttled == 1
    assert summary.timeline[2].errors == 2
    # Four users joining over two seconds: 1 at the start, then 3, then all of them.
    assert [point.users for point in summary.timeline] == [1, 3, 4]


def test_a_live_timeline_leaves_out_the_second_in_progress() -> None:
    samples = [
        Sample(start=0.2, latency_ms=500.0, status=200),
        Sample(start=0.9, latency_ms=900.0, status=200),
        Sample(start=1.5, latency_ms=900.0, status=200),
    ]

    live = summarize(samples, 2.5, BenchmarkSpec(), live=True)
    done = summarize(samples, 2.5, BenchmarkSpec())

    assert [point.requests for point in live.timeline] == [1, 1]
    assert [point.requests for point in done.timeline] == [1, 1, 1]
    assert live.requests == 3


def test_an_empty_run() -> None:
    summary = summarize([], 0.0, BenchmarkSpec())

    assert summary.requests == 0 and summary.success_rate is None
    assert summary.latency_p50_ms is None and summary.members == []
    assert verdict(summary) == "No requests finished."


def test_the_verdict_names_throttling_and_failovers() -> None:
    samples = [Sample(start=0, latency_ms=2000.0, status=200, member="a") for _ in range(9)]
    samples[0].fallbacks = 1
    samples.append(Sample(start=0, latency_ms=10.0, status=429))

    text = verdict(summarize(samples, 1.0, BenchmarkSpec()))

    assert text.startswith("Handled 10 requests/s, p95 latency 2.0 s")
    assert "1 requests (10%) got 429" in text
    assert "moved 1 requests to another member" in text


def test_prompt_sizes() -> None:
    assert len(prompt("short").split()) < 60
    assert 300 < len(prompt("medium").split()) < 450
    assert 2900 < len(prompt("long").split()) < 3100


# -- the engine against the gateway --------------------------------------------------------------


def test_streaming_measures_the_first_token_and_reads_usage(litellm: FakeLiteLLM) -> None:
    key = _key(litellm)
    samples = _run(BenchmarkSpec(users=3, max_requests=12), litellm, key)

    assert len(samples) == 12
    assert all(sample.status == 200 and sample.member == "east" for sample in samples)
    assert all(sample.ttft_ms is not None for sample in samples)
    assert all(sample.completion_tokens == 2 and sample.prompt_tokens > 10 for sample in samples)
    body = json.loads(litellm.requests[-1].content)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["model"] == "gpt6" and body["max_tokens"] == 128
    assert litellm.requests[-1].headers["Authorization"] == f"Bearer {key}"


def test_without_streaming(litellm: FakeLiteLLM) -> None:
    samples = _run(BenchmarkSpec(max_requests=4, stream=False), litellm, _key(litellm))

    assert len(samples) == 4
    assert all(sample.ttft_ms is None and sample.completion_tokens == 2 for sample in samples)
    assert "stream_options" not in json.loads(litellm.requests[-1].content)


def test_failovers_and_throttling(litellm: FakeLiteLLM) -> None:
    key = _key(litellm)
    litellm.failing["east"] = 429
    moved = _run(BenchmarkSpec(max_requests=4), litellm, key)
    litellm.failing["west"] = 429
    refused = _run(BenchmarkSpec(max_requests=4), litellm, key)

    assert {(s.status, s.member, s.fallbacks) for s in moved} == {(200, "west", 1)}
    assert summarize(moved, 1, BenchmarkSpec()).failovers == 4
    assert summarize(refused, 1, BenchmarkSpec()).throttled == 4


def test_a_wrong_key_is_counted_as_errors(litellm: FakeLiteLLM) -> None:
    samples = _run(BenchmarkSpec(max_requests=3), litellm, "sk-wrong")

    assert summarize(samples, 1, BenchmarkSpec()).errors == {"401": 3}


def test_connection_errors(litellm: FakeLiteLLM) -> None:
    litellm.unreachable_hosts.add("gw.example.azurecontainerapps.io")

    samples = _run(BenchmarkSpec(max_requests=2), litellm, "sk-x")

    assert [sample.error for sample in samples] == ["connection", "connection"]


def test_the_rate_cap_spaces_requests(litellm: FakeLiteLLM) -> None:
    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    samples = _run(
        BenchmarkSpec(users=4, duration_seconds=3, rate=2),
        litellm,
        _key(litellm),
        clock=lambda: now[0],
        sleep=sleep,
    )

    # Two a second for three seconds; the request due at 3.0 s is past the end.
    assert sorted(round(sample.start, 2) for sample in samples) == [0, 0.5, 1, 1.5, 2, 2.5]


def test_should_stop_ends_the_run(litellm: FakeLiteLLM) -> None:
    samples: list[Sample] = []

    asyncio.run(
        run_benchmark(
            BenchmarkSpec(users=2, duration_seconds=60),
            CHAT,
            _key(litellm),
            "gpt6",
            samples,
            transport=litellm.transport(),
            should_stop=lambda: len(samples) >= 5,
        )
    )

    assert 5 <= len(samples) <= 6


def test_a_stop_does_not_wait_for_users_ramping_up(litellm: FakeLiteLLM) -> None:
    samples: list[Sample] = []
    began = time.monotonic()

    asyncio.run(
        run_benchmark(
            BenchmarkSpec(users=10, duration_seconds=60, ramp_up_seconds=60),
            CHAT,
            _key(litellm),
            "gpt6",
            samples,
            transport=litellm.transport(),
            should_stop=lambda: len(samples) >= 3,
        )
    )

    assert time.monotonic() - began < 2


def test_the_report(litellm: FakeLiteLLM) -> None:
    spec = BenchmarkSpec(max_requests=4)
    samples = _run(spec, litellm, _key(litellm))
    samples[0].member = "gpt6--pool-west"

    text = report(summarize(samples, 2.0, spec), model="gpt6", spec=spec)

    assert text.startswith("Load test of gpt6: 2 users, 2 s")
    assert "4 ok, 0 throttled (429)" in text
    assert "first token" in text
    assert "east: 3 (75%)" in text and "    pool-west: 1 (25%)" in text
    assert text.endswith("every request succeeded.")


def test_limits() -> None:
    with pytest.raises(ValueError):
        BenchmarkSpec(users=51)
    with pytest.raises(ValueError):
        BenchmarkSpec(duration_seconds=601)
    with pytest.raises(ValueError):
        BenchmarkSpec(max_requests=5001)
    with pytest.raises(ValueError):
        BenchmarkSpec(rate=0)


# -- from the web tab -----------------------------------------------------------------------------


class _Gate(httpx.AsyncBaseTransport):
    """A gateway that holds every request until the test opens it."""

    def __init__(self, inner: httpx.MockTransport) -> None:
        self.inner = inner
        self.open = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        while not self.open:
            await asyncio.sleep(0.01)
        return await self.inner.handle_async_request(request)


def _wait(client: TestClient, path: str) -> dict[str, Any]:
    for _ in range(500):
        run: dict[str, Any] = client.get(path).json()
        if run["state"] != "running":
            return run
        time.sleep(0.01)
    raise AssertionError("the load test did not finish")


@pytest.fixture
def azure() -> FakeAzure:
    return FakeAzure()


@pytest.fixture
def client(azure: FakeAzure) -> Iterator[TestClient]:
    yield _client(azure, _settings())
    app.dependency_overrides.clear()


def _test_key(client: TestClient) -> str:
    return str(client.post("/api/litellm/pools/gpt6/keys", headers=WRITE).json()["key"])


def test_a_load_test_from_the_web_tab(client: TestClient, azure: FakeAzure) -> None:
    members = create_pool(client, azure)
    key = _test_key(client)
    spec = {"users": 2, "max_requests": 10, "duration_seconds": 30}

    started = client.post(
        "/api/litellm/pools/gpt6/benchmarks", json={"spec": spec, "key": key}, headers=WRITE
    )

    assert started.status_code == 200, started.text
    assert key not in started.text
    run = _wait(client, f"/api/litellm/pools/gpt6/benchmarks/{started.json()['id']}")
    assert run["state"] == "done" and run["model"] == "gpt6"
    assert run["summary"]["requests"] == 10 and run["summary"]["successes"] == 10
    assert run["summary"]["members"][0]["member"] == f"gpt6--{members[0]}"
    assert key not in json.dumps(run)
    assert all(r.url.path == "/v1/chat/completions" for r in azure.litellm.requests[-10:])


def test_the_model_must_be_one_of_the_pools_names(client: TestClient, azure: FakeAzure) -> None:
    create_pool(client, azure)
    body = {"spec": {"model": "gpt-other"}, "key": "sk-x"}

    response = client.post("/api/litellm/pools/gpt6/benchmarks", json=body, headers=WRITE)

    assert response.status_code == 400
    assert "model must be one of the pool's names" in response.json()["detail"]


def test_one_load_test_at_a_time_and_stop(azure: FakeAzure) -> None:
    gate = _Gate(azure.litellm.transport())
    client = _client(azure, _settings(), benchmarks=BenchmarkRunner(gate))
    try:
        create_pool(client, azure)
        body = {"spec": {"users": 2, "duration_seconds": 60}, "key": _test_key(client)}
        first = client.post("/api/litellm/pools/gpt6/benchmarks", json=body, headers=WRITE).json()
        second = client.post("/api/litellm/pools/gpt6/benchmarks", json=body, headers=WRITE)
        stopped = client.post(
            f"/api/litellm/pools/gpt6/benchmarks/{first['id']}/stop", headers=WRITE
        )
        gate.open = True
        run = _wait(client, f"/api/litellm/pools/gpt6/benchmarks/{first['id']}")
    finally:
        gate.open = True
        app.dependency_overrides.clear()

    assert first["state"] == "running"
    assert second.status_code == 409
    assert stopped.status_code == 200
    # The two requests already sent finish; nothing new starts.
    assert run["state"] == "stopped" and run["summary"]["requests"] == 2


def test_runs_are_per_pool(client: TestClient, azure: FakeAzure) -> None:
    create_pool(client, azure)
    body = {"spec": {"max_requests": 1}, "key": _test_key(client)}
    run = client.post("/api/litellm/pools/gpt6/benchmarks", json=body, headers=WRITE).json()

    assert client.get(f"/api/litellm/pools/other/benchmarks/{run['id']}").status_code == 404
    assert client.get("/api/litellm/pools/gpt6/benchmarks/nope").status_code == 404


def test_only_owners_start_load_tests(azure: FakeAzure) -> None:
    client = _client(azure, _settings(auth_mode="easy_auth", owners="owner@contoso.com"))
    try:
        reader = {"X-MS-CLIENT-PRINCIPAL-NAME": "reader@contoso.com"}
        owner = {"X-MS-CLIENT-PRINCIPAL-NAME": "owner@contoso.com"}
        body = {"spec": {}, "key": "sk-x"}
        path = "/api/litellm/pools/gpt6/benchmarks"
        as_reader = client.post(path, json=body, headers={**reader, **WRITE})
        without_csrf = client.post(path, json=body, headers=owner)
        reader_poll = client.get(f"{path}/x", headers=reader)
    finally:
        app.dependency_overrides.clear()

    assert as_reader.status_code == 403
    assert without_csrf.status_code == 403
    assert reader_poll.status_code == 403
