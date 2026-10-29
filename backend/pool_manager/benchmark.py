"""A load test for a pool: simulated users calling the gateway like real clients.

The load is closed-loop: each of ``users`` sends a request, waits for the answer, then sends the
next, until the duration or the request cap. Users can join over a ramp-up, and a rate cap
spaces out the requests they start. Streaming requests measure the time to the first token.
Every request records a ``Sample``; ``summarize`` turns them into throughput, latency and
time-to-first-token percentiles, errors and 429s, failovers, and which members answered.

Both gateways use it; each says where and how to send the requests (a ``BenchmarkTarget``: the
URL, the key's header, the response header naming the member that answered). It runs from the
pool's call guide (``BenchmarkRunner``, one run at a time, in memory) and from the terminal
(scripts/benchmark.py). The key is only sent to the gateway; response bodies are read for their
token usage and dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .errors import ServiceError
from .models import (
    BenchmarkMember,
    BenchmarkPoint,
    BenchmarkRun,
    BenchmarkSpec,
    BenchmarkSummary,
    PromptSize,
)

logger = logging.getLogger(__name__)

PRESETS: dict[str, dict[str, Any]] = {
    "smoke": {"users": 2, "duration_seconds": 30, "ramp_up_seconds": 0},
    "typical": {"users": 10, "duration_seconds": 120, "ramp_up_seconds": 10},
    "stress": {"users": 30, "duration_seconds": 180, "ramp_up_seconds": 30},
}
# Roughly how many input tokens each prompt size sends.
PROMPT_TOKENS: dict[PromptSize, int] = {"short": 50, "medium": 500, "long": 4000}
_TIMEOUT = httpx.Timeout(180.0, connect=10.0)
_KEPT_RUNS = 5
_FILLER = (
    "The gateway spreads requests across deployments in several regions, retries a failed "
    "attempt on the next member and benches a member that keeps failing for a while. "
)


@dataclass(frozen=True)
class BenchmarkTarget:
    """Where and how a run sends its requests; each product fills it in."""

    # The full chat completions URL.
    url: str
    # The model name in the body.
    model: str
    # They hold the key; never logged.
    headers: Mapping[str, str]
    params: Mapping[str, str] = field(default_factory=dict)
    # The response header naming the member that answered, when the gateway passes one on.
    member_header: str | None = None
    # The response header counting failed attempts on other members before the answer.
    fallbacks_header: str | None = None
    # LiteLLM turns max_tokens into what each model takes; APIM forwards the body as it is,
    # and newer Azure OpenAI models refuse max_tokens.
    tokens_field: str = "max_tokens"


@dataclass
class Sample:
    """One request: when it started (seconds since the run began), how it went."""

    start: float
    latency_ms: float
    ttft_ms: float | None = None
    status: int | None = None
    # "timeout" or "connection" when there was no HTTP answer.
    error: str | None = None
    member: str | None = None
    fallbacks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def prompt(size: PromptSize) -> str:
    """A prompt of about ``PROMPT_TOKENS[size]`` input tokens (a token is ~0.75 words)."""
    words = max(1, round(PROMPT_TOKENS[size] * 0.75))
    filler = (_FILLER * (words // len(_FILLER.split()) + 1)).split()[:words]
    return "Summarize in two sentences:\n" + " ".join(filler)


def request_body(
    spec: BenchmarkSpec, model: str, tokens_field: str = "max_tokens"
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt(spec.prompt_size)}],
        tokens_field: spec.max_tokens,
        "stream": spec.stream,
    }
    if spec.stream:
        body["stream_options"] = {"include_usage": True}
    return body


def _usage(sample: Sample, usage: Any) -> None:
    if isinstance(usage, dict):
        sample.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        sample.completion_tokens = int(usage.get("completion_tokens") or 0)


async def _call(
    client: httpx.AsyncClient,
    target: BenchmarkTarget,
    body: dict[str, Any],
    started: float,
    clock: Callable[[], float],
) -> Sample:
    begin = clock()
    sample = Sample(start=begin - started, latency_ms=0.0)
    try:
        async with client.stream(
            "POST", target.url, params=dict(target.params), json=body, headers=target.headers
        ) as response:
            sample.status = response.status_code
            if target.member_header:
                sample.member = response.headers.get(target.member_header)
            if target.fallbacks_header:
                try:
                    sample.fallbacks = int(response.headers.get(target.fallbacks_header) or 0)
                except ValueError:
                    sample.fallbacks = 0
            if response.status_code == 200 and body.get("stream"):
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or []
                    delta = (choices[0].get("delta") or {}) if choices else {}
                    if sample.ttft_ms is None and delta.get("content"):
                        sample.ttft_ms = (clock() - begin) * 1000
                    _usage(sample, chunk.get("usage"))
            else:
                raw = await response.aread()
                if response.status_code == 200:
                    with contextlib.suppress(ValueError, AttributeError):
                        _usage(sample, json.loads(raw).get("usage"))
    except httpx.TimeoutException:
        sample.error = "timeout"
    except httpx.HTTPError:
        sample.error = "connection"
    sample.latency_ms = (clock() - begin) * 1000
    return sample


async def run_benchmark(
    spec: BenchmarkSpec,
    target: BenchmarkTarget,
    samples: list[Sample],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> float:
    """Runs the load against the target, appending a sample per request to ``samples`` as it
    goes (so a stopped or interrupted run keeps what it did). Returns how long it ran, in
    seconds."""
    started = clock()
    deadline = started + spec.duration_seconds
    body = request_body(spec, target.model, target.tokens_field)
    issued = 0
    lock = asyncio.Lock()
    next_slot = started
    interval = 1.0 / spec.rate if spec.rate else 0.0

    async def nap(seconds: float) -> None:
        """Waits in short steps, so a stop needn't wait for a user still ramping up."""
        end = clock() + seconds
        while not should_stop() and (left := end - clock()) > 0:
            await sleep(min(0.25, left))

    async def turn() -> bool:
        """Takes the next request's turn; False when the run is over."""
        nonlocal issued, next_slot
        async with lock:
            if should_stop() or clock() >= deadline or issued >= spec.max_requests:
                return False
            issued += 1
            wait = 0.0
            if interval:
                slot = max(next_slot, clock())
                next_slot = slot + interval
                wait = slot - clock()
        if wait > 0:
            await nap(wait)
        return not should_stop() and clock() < deadline

    async def user(index: int, client: httpx.AsyncClient) -> None:
        if spec.ramp_up_seconds and spec.users > 1:
            await nap(spec.ramp_up_seconds * index / spec.users)
        while await turn():
            samples.append(await _call(client, target, body, started, clock))

    limits = httpx.Limits(max_connections=spec.users, max_keepalive_connections=spec.users)
    async with httpx.AsyncClient(transport=transport, timeout=_TIMEOUT, limits=limits) as client:
        await asyncio.gather(*(user(index, client) for index in range(spec.users)))
    return clock() - started


# -- the numbers ------------------------------------------------------------------------------


def _percentile(values: list[float], percent: float) -> float | None:
    """Nearest-rank percentile; None without values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return round(ordered[rank - 1], 1)


def _users_at(spec: BenchmarkSpec, second: int) -> int:
    if not spec.ramp_up_seconds or spec.users <= 1:
        return spec.users
    joined = math.floor(second * spec.users / spec.ramp_up_seconds) + 1
    return min(spec.users, joined)


def summarize(
    samples: list[Sample], elapsed: float, spec: BenchmarkSpec, *, live: bool = False
) -> BenchmarkSummary:
    """The run's numbers. The timeline counts each request in the second it finished (what a
    client sees as throughput); ``live`` leaves out the second still in progress."""
    done = list(samples)
    ok = [sample for sample in done if sample.status == 200]
    throttled = sum(1 for sample in done if sample.status == 429)
    errors: dict[str, int] = {}
    for sample in done:
        if sample.status in {200, 429}:
            continue
        label = sample.error or str(sample.status)
        errors[label] = errors.get(label, 0) + 1
    latencies = [sample.latency_ms for sample in ok]
    ttfts = [sample.ttft_ms for sample in ok if sample.ttft_ms is not None]
    elapsed = max(elapsed, 1e-6)
    members: dict[str | None, list[Sample]] = {}
    for sample in done:
        if sample.member is not None or sample.status == 200:
            members.setdefault(sample.member, []).append(sample)
    answered = sum(1 for sample in done if sample.member is not None) or 1
    member_views = [
        BenchmarkMember(
            member=member,
            requests=len(group),
            share=round(len(group) / answered, 4),
            latency_p50_ms=_percentile([s.latency_ms for s in group if s.status == 200], 50),
            errors=sum(1 for s in group if s.status != 200),
        )
        for member, group in sorted(members.items(), key=lambda item: -len(item[1]))
    ]
    timeline: list[BenchmarkPoint] = []
    seconds = max(1, math.floor(elapsed) if live else math.ceil(elapsed))
    buckets: list[list[Sample]] = [[] for _ in range(seconds)]
    for sample in done:
        finished = int(sample.start + sample.latency_ms / 1000)
        if live and finished >= seconds:
            continue
        buckets[min(seconds - 1, max(0, finished))].append(sample)
    for second, bucket in enumerate(buckets):
        timeline.append(
            BenchmarkPoint(
                second=second,
                requests=len(bucket),
                errors=sum(1 for s in bucket if s.status not in {200, 429}),
                throttled=sum(1 for s in bucket if s.status == 429),
                latency_p50_ms=_percentile([s.latency_ms for s in bucket if s.status == 200], 50),
                users=_users_at(spec, second),
            )
        )
    completion = sum(sample.completion_tokens for sample in ok)
    return BenchmarkSummary(
        elapsed_seconds=round(elapsed, 2),
        requests=len(done),
        successes=len(ok),
        throttled=throttled,
        errors=errors,
        requests_per_second=round(len(done) / elapsed, 2),
        success_rate=round(len(ok) / len(done), 4) if done else None,
        latency_p50_ms=_percentile(latencies, 50),
        latency_p90_ms=_percentile(latencies, 90),
        latency_p95_ms=_percentile(latencies, 95),
        latency_p99_ms=_percentile(latencies, 99),
        latency_max_ms=round(max(latencies), 1) if latencies else None,
        ttft_p50_ms=_percentile(ttfts, 50),
        ttft_p95_ms=_percentile(ttfts, 95),
        prompt_tokens=sum(sample.prompt_tokens for sample in ok),
        completion_tokens=completion,
        output_tokens_per_second=round(completion / elapsed, 1),
        failovers=sum(1 for sample in ok if sample.fallbacks > 0),
        members=member_views,
        timeline=timeline,
    )


def verdict(summary: BenchmarkSummary) -> str:
    """One plain sentence about the run (the web tab says the same in its own language)."""
    if not summary.requests:
        return "No requests finished."
    parts = [f"Handled {summary.requests_per_second:g} requests/s"]
    if summary.latency_p95_ms is not None:
        parts.append(f"p95 latency {summary.latency_p95_ms / 1000:.1f} s")
    text = ", ".join(parts)
    rate = summary.success_rate or 0.0
    if summary.throttled:
        text += (
            f"; {summary.throttled} requests ({summary.throttled / summary.requests:.0%}) got "
            "429: every member was rate-limited at once, so the pool needs more capacity"
        )
    elif rate < 0.99:
        text += f"; only {rate:.0%} succeeded, see the errors"
    else:
        text += "; every request succeeded"
    if summary.failovers:
        text += (
            f". The gateway moved {summary.failovers} requests to another member after a failure"
        )
    return text + "."


def report(summary: BenchmarkSummary, *, model: str, spec: BenchmarkSpec) -> str:
    """The run as text, for the terminal."""

    def ms(value: float | None) -> str:
        return "-" if value is None else f"{value:,.0f} ms"

    lines = [
        f"Load test of {model}: {spec.users} users, {summary.elapsed_seconds:.0f} s",
        f"  requests      {summary.requests} ({summary.requests_per_second:g}/s), "
        f"{summary.successes} ok, {summary.throttled} throttled (429)",
        f"  latency       p50 {ms(summary.latency_p50_ms)}  p90 {ms(summary.latency_p90_ms)}  "
        f"p95 {ms(summary.latency_p95_ms)}  p99 {ms(summary.latency_p99_ms)}  "
        f"max {ms(summary.latency_max_ms)}",
    ]
    if spec.stream:
        lines.append(
            f"  first token   p50 {ms(summary.ttft_p50_ms)}  p95 {ms(summary.ttft_p95_ms)}"
        )
    lines.append(
        f"  tokens        {summary.prompt_tokens:,} in, {summary.completion_tokens:,} out "
        f"({summary.output_tokens_per_second:g} out/s)"
    )
    if summary.errors:
        listed = ", ".join(f"{label}: {count}" for label, count in summary.errors.items())
        lines.append(f"  errors        {listed}")
    lines.append(f"  failovers     {summary.failovers}")
    if summary.members:
        lines.append("  members")
        for member in summary.members:
            # Rows are "<pool>--<member>", as the web tab shows them.
            name = member.member.partition("--")[2] or member.member if member.member else None
            lines.append(
                f"    {name or '(not reported)'}: {member.requests} "
                f"({member.share:.0%}), p50 {ms(member.latency_p50_ms)}, {member.errors} errors"
            )
    lines.append("")
    lines.append(verdict(summary))
    return "\n".join(lines)


# -- runs from the web tab --------------------------------------------------------------------


def _scope(pool_id: str | None, api_id: str | None) -> str:
    """What the routes find a run by: the API on APIM (a key opens an API), else the pool."""
    return (
        f"api:{api_id.casefold()}" if api_id is not None else f"pool:{(pool_id or '').casefold()}"
    )


@dataclass
class _Run:
    id: str
    scope: str
    pool_id: str
    api_id: str | None
    spec: BenchmarkSpec
    model: str
    started_at: str
    started: float
    samples: list[Sample] = field(default_factory=list)
    state: str = "running"
    elapsed: float | None = None
    error: str | None = None
    stop: bool = False


class BenchmarkRunner:
    """Runs one load test at a time, each on its own thread and event loop, and keeps the last
    few in memory; nothing is written anywhere."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._lock = threading.Lock()
        self._runs: OrderedDict[str, _Run] = OrderedDict()

    def start(
        self,
        pool_id: str,
        spec: BenchmarkSpec,
        target: BenchmarkTarget,
        actor: str,
        *,
        api_id: str | None = None,
    ) -> BenchmarkRun:
        """Starts a run of the pool, through ``api_id`` on APIM."""
        with self._lock:
            busy = next((run for run in self._runs.values() if run.state == "running"), None)
            if busy is not None:
                raise ServiceError(
                    409, f"A load test of {busy.pool_id} is running; stop it or wait for it"
                )
            run = _Run(
                id=secrets.token_hex(6),
                scope=_scope(pool_id, api_id),
                pool_id=pool_id,
                api_id=api_id,
                spec=spec,
                model=target.model,
                started_at=datetime.now(UTC).isoformat(timespec="seconds"),
                started=time.monotonic(),
            )
            self._runs[run.id] = run
            while len(self._runs) > _KEPT_RUNS:
                self._runs.popitem(last=False)
        logger.info(
            "%s started a load test of %s%s: %d users, %d s",
            actor,
            pool_id,
            f" through API {api_id}" if api_id else "",
            spec.users,
            spec.duration_seconds,
        )
        threading.Thread(target=self._run, args=(run, target), daemon=True).start()
        return self._view(run)

    def _run(self, run: _Run, target: BenchmarkTarget) -> None:
        try:
            run.elapsed = asyncio.run(
                run_benchmark(
                    run.spec,
                    target,
                    run.samples,
                    transport=self._transport,
                    should_stop=lambda: run.stop,
                )
            )
            run.state = "stopped" if run.stop else "done"
        except Exception as error:  # The run fails; the app goes on.
            logger.warning("Load test %s failed: %s", run.id, error)
            run.elapsed = time.monotonic() - run.started
            run.error = str(error) or type(error).__name__
            run.state = "failed"

    def _find(self, pool_id: str | None, run_id: str, api_id: str | None) -> _Run:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None or run.scope != _scope(pool_id, api_id):
            raise ServiceError(404, "No such load test (runs are kept in memory only)")
        return run

    def get(self, pool_id: str | None, run_id: str, *, api_id: str | None = None) -> BenchmarkRun:
        """A run of the pool, or of the API when ``api_id`` is given (``pool_id`` unused)."""
        return self._view(self._find(pool_id, run_id, api_id))

    def stop(self, pool_id: str | None, run_id: str, *, api_id: str | None = None) -> BenchmarkRun:
        run = self._find(pool_id, run_id, api_id)
        run.stop = True
        return self._view(run)

    def close(self) -> None:
        """Stops every run (a public app session ending)."""
        with self._lock:
            for run in self._runs.values():
                run.stop = True

    def _view(self, run: _Run) -> BenchmarkRun:
        elapsed = run.elapsed if run.elapsed is not None else time.monotonic() - run.started
        return BenchmarkRun(
            id=run.id,
            pool_id=run.pool_id,
            api_id=run.api_id,
            state=run.state,  # type: ignore[arg-type]
            spec=run.spec,
            model=run.model,
            started_at=run.started_at,
            error=run.error,
            summary=summarize(run.samples, elapsed, run.spec, live=run.state == "running"),
        )
