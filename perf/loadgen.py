"""Open-loop load against an OpenAI-style chat completions endpoint, from several processes.

Requests start on a fixed schedule (``rpm`` a minute, spread over the workers) whether or not
the earlier ones have answered, as independent clients do; a closed loop of users waiting for
their answers would send less as the gateway slows down and hide the latency it adds. Each
request records a sample: when it started, how long it took, the time to its first token when
streamed, its status or error, which member answered (``member_header``) and its tokens.
``summarize`` turns the samples that started after the warm-up into the step's numbers.

    PERF_KEY=... python perf/loadgen.py --url https://<gateway>/v1/chat/completions \
        --model <pool> --shape chat --rpm 600 --duration 180 --warmup 60 --workers 4 \
        --key-header Authorization --member-header x-litellm-model-id --out step.json

The key comes from ``PERF_KEY`` and is only sent to the endpoint, never written anywhere.
``--key-header Authorization`` sends ``Bearer <key>``; any other header gets the key as is
(``api-key`` for APIM).
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import math
import multiprocessing as mp
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

# Input tokens, output tokens, streamed.
SHAPES: dict[str, tuple[int, int, bool]] = {
    "short": (500, 100, False),
    "chat": (2_000, 500, True),
    "rag": (8_000, 300, True),
    "agent": (50_000, 400, False),
}
SHAPE_NAMES = {
    "short": "Short answer",
    "chat": "Chat, streamed",
    "rag": "RAG, streamed",
    "agent": "Agent loop step",
}
# A chunk that carries text: "content" followed by a non-empty string, spaced or not.
_CONTENT = re.compile(rb'"content": ?"[^"]')
_FILLER = (
    "The gateway spreads requests across deployments in several regions, retries a failed "
    "attempt on the next member and benches a member that keeps failing for a while. "
)


@dataclass
class Config:
    url: str
    model: str
    shape: str
    rpm: float
    duration: float
    warmup: float = 0.0
    workers: int = 1
    key_header: str = "Authorization"
    member_header: str = "x-litellm-model-id"
    timeout: float = 300.0
    max_in_flight: int = 20_000
    # Extra query string (APIM's api-version), already encoded, without "?".
    query: str = ""
    # Extra request fields, e.g. {"reasoning_effort": "none"} for a real reasoning model.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Sample:
    start: float
    latency_ms: float = 0.0
    ttft_ms: float | None = None
    status: int | None = None
    # "timeout", "connection" or "dropped" (the client's own in-flight cap) without an answer.
    error: str | None = None
    member: str | None = None
    fallbacks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    chunks: int = 0


@dataclass
class Result:
    config: dict[str, Any]
    started_at: float
    samples: list[Sample] = field(default_factory=list)
    client_cpu: list[float] = field(default_factory=list)


def prompt(tokens_in: int, tokens_out: int) -> str:
    """About ``tokens_in`` input tokens (four characters a token, as the mock counts them),
    asking a real model for an answer of about ``tokens_out`` tokens (0.75 words a token)."""
    ask = f"Write an answer of about {round(tokens_out * 0.75)} words explaining the text below.\n"
    text = (_FILLER * (tokens_in * 4 // len(_FILLER) + 1))[: max(0, tokens_in * 4 - len(ask))]
    return ask + text


def body(model: str, shape: str, extra: dict[str, Any] | None = None) -> bytes:
    tokens_in, tokens_out, stream = SHAPES[shape]
    # max_completion_tokens: newer Azure OpenAI models refuse max_tokens; the mock reads either.
    request: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt(tokens_in, tokens_out)}],
        "max_completion_tokens": tokens_out,
        "stream": stream,
        **(extra or {}),
    }
    if stream:
        request["stream_options"] = {"include_usage": True}
    return json.dumps(request).encode()


def _usage(sample: Sample, raw: bytes) -> None:
    try:
        usage = json.loads(raw).get("usage") or {}
    except (ValueError, AttributeError):
        return
    sample.prompt_tokens = int(usage.get("prompt_tokens") or 0)
    sample.completion_tokens = int(usage.get("completion_tokens") or 0)


async def _call(
    session: aiohttp.ClientSession,
    url: str,
    payload: bytes,
    headers: dict[str, str],
    member_header: str,
    stream: bool,
    sample: Sample,
) -> None:
    begin = time.monotonic()
    try:
        async with session.post(url, data=payload, headers=headers) as response:
            sample.status = response.status
            sample.member = response.headers.get(member_header)
            try:
                sample.fallbacks = int(response.headers.get("x-litellm-attempted-fallbacks") or 0)
            except ValueError:
                sample.fallbacks = 0
            if response.status == 200 and stream:
                async for line in response.content:
                    if not line.startswith(b"data:"):
                        continue
                    sample.chunks += 1
                    if sample.ttft_ms is None and _CONTENT.search(line):
                        sample.ttft_ms = (time.monotonic() - begin) * 1000
                    if b'"prompt_tokens"' in line:
                        _usage(sample, line[5:].strip())
            else:
                raw = await response.read()
                if response.status == 200:
                    _usage(sample, raw)
    except TimeoutError:
        sample.error = "timeout"
    except (aiohttp.ClientError, OSError):
        sample.error = "connection"
    sample.latency_ms = (time.monotonic() - begin) * 1000


async def _worker_loop(config: Config, index: int, epoch: float, key: str) -> list[Sample]:
    _, _, stream = SHAPES[config.shape]
    payload = body(config.model, config.shape, config.extra)
    url = config.url + (f"?{config.query}" if config.query else "")
    headers = {"Content-Type": "application/json"}
    if config.key_header.lower() == "authorization":
        headers["Authorization"] = f"Bearer {key}"
    elif key:
        headers[config.key_header] = key
    interval = 60.0 * config.workers / config.rpm
    end = epoch + config.warmup + config.duration
    samples: list[Sample] = []
    tasks: set[asyncio.Task[None]] = set()
    connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300, enable_cleanup_closed=True)
    timeout = aiohttp.ClientTimeout(total=config.timeout, sock_connect=10)
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, read_bufsize=2**18
    ) as session:
        k = 0
        while True:
            at = epoch + interval * (k + index / config.workers)
            if at >= end:
                break
            wait = at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            sample = Sample(start=at - epoch)
            samples.append(sample)
            if len(tasks) >= config.max_in_flight:
                sample.error = "dropped"
            else:
                task = asyncio.create_task(
                    _call(session, url, payload, headers, config.member_header, stream, sample)
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            k += 1
        if tasks:
            await asyncio.wait(tasks, timeout=config.timeout + 5)
    return samples


def _worker(config: Config, index: int, epoch: float, key: str, out: Any) -> None:
    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass
    samples = asyncio.run(_worker_loop(config, index, epoch, key))
    out.put([asdict(s) for s in samples])


def _cpu_times() -> tuple[float, float] | None:
    try:
        fields = Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]
    except OSError:
        return None
    values = [float(v) for v in fields]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def run(config: Config, key: str) -> Result:
    """Runs one step from ``config.workers`` processes and returns every sample."""
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    # Starts on the next whole second plus a moment for the processes to come up.
    epoch = math.ceil(time.time()) + 2.0
    monotonic_epoch = time.monotonic() + (epoch - time.time())
    procs = [
        ctx.Process(target=_worker, args=(config, i, monotonic_epoch, key, out))
        for i in range(config.workers)
    ]
    for proc in procs:
        proc.start()
    result = Result(config={k: v for k, v in asdict(config).items()}, started_at=epoch)
    previous = _cpu_times()
    gathered = 0
    while gathered < len(procs):
        try:
            batch = out.get(timeout=1.0)
        except Exception:  # queue.Empty
            batch = None
        if batch is not None:
            result.samples.extend(Sample(**s) for s in batch)
            gathered += 1
        now = _cpu_times()
        if previous and now and now[0] > previous[0]:
            busy = 1 - (now[1] - previous[1]) / (now[0] - previous[0])
            result.client_cpu.append(round(busy * 100, 1))
        previous = now
        if batch is None and not any(p.is_alive() for p in procs) and out.empty():
            break
    for proc in procs:
        proc.join(timeout=10)
    result.samples.sort(key=lambda s: s.start)
    return result


def _pct(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)], 1)


def _kind(sample: Sample) -> str:
    if sample.error:
        return sample.error
    if sample.status == 200:
        return "ok"
    if sample.status == 429:
        return "429"
    if sample.status is not None and sample.status >= 500:
        return "5xx"
    return "4xx"


def summarize(result: Result) -> dict[str, Any]:
    config = result.config
    warmup, duration = config["warmup"], config["duration"]
    window = [s for s in result.samples if warmup <= s.start < warmup + duration]
    kinds = Counter(_kind(s) for s in window)
    ok = [s for s in window if _kind(s) == "ok"]
    latencies = [s.latency_ms for s in ok]
    ttfts = [s.ttft_ms for s in ok if s.ttft_ms is not None]
    members = Counter(s.member or "?" for s in ok)
    timeline: dict[int, Counter[str]] = {}
    for s in result.samples:
        timeline.setdefault(int(s.start), Counter())[_kind(s)] += 1
    errors = len(window) - len(ok)
    cpu = result.client_cpu[2:-2] or result.client_cpu
    return {
        "shape": config["shape"],
        "offered_rpm": config["rpm"],
        "started": len(window),
        "ok": len(ok),
        "achieved_rpm": round(len(ok) * 60 / duration, 1) if duration else 0,
        "error_rate": round(errors / len(window), 4) if window else None,
        "errors": {k: v for k, v in kinds.items() if k != "ok"},
        "latency_ms": {
            p: _pct(latencies, q)
            for p, q in (("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99), ("max", 100))
        },
        "ttft_ms": {p: _pct(ttfts, q) for p, q in (("p50", 50), ("p95", 95), ("p99", 99))},
        "members": dict(members.most_common()),
        "fallbacks": sum(s.fallbacks for s in ok),
        "prompt_tokens": round(sum(s.prompt_tokens for s in ok) / len(ok)) if ok else 0,
        "completion_tokens": round(sum(s.completion_tokens for s in ok) / len(ok)) if ok else 0,
        "client_cpu": {
            "avg": round(sum(cpu) / len(cpu), 1) if cpu else None,
            "max": max(cpu) if cpu else None,
        },
        "timeline": [{"second": sec, **counts} for sec, counts in sorted(timeline.items())],
        "started_at": result.started_at,
    }


def write_samples(result: Result, path: Path) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        columns = (
            "start,latency_ms,ttft_ms,status,error,member,fallbacks,"
            "prompt_tokens,completion_tokens,chunks"
        )
        f.write(columns + "\n")
        for s in result.samples:
            ttft = "" if s.ttft_ms is None else f"{s.ttft_ms:.1f}"
            row = [
                f"{s.start:.3f}",
                f"{s.latency_ms:.1f}",
                ttft,
                s.status or "",
                s.error or "",
                s.member or "",
                s.fallbacks,
                s.prompt_tokens,
                s.completion_tokens,
                s.chunks,
            ]
            f.write(",".join(str(value) for value in row) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", required=True, help="the full chat/completions URL")
    parser.add_argument("--model", required=True)
    parser.add_argument("--shape", choices=sorted(SHAPES), default="chat")
    parser.add_argument("--rpm", type=float, required=True)
    parser.add_argument("--duration", type=float, default=180)
    parser.add_argument("--warmup", type=float, default=60)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--key-header", default="Authorization")
    parser.add_argument("--member-header", default="x-litellm-model-id")
    parser.add_argument(
        "--query", default="", help="extra query string, e.g. api-version=2024-10-21"
    )
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--out", type=Path, help="write the summary here")
    parser.add_argument("--samples", type=Path, help="write every request here (.csv.gz)")
    args = parser.parse_args(argv)
    config = Config(
        url=args.url,
        model=args.model,
        shape=args.shape,
        rpm=args.rpm,
        duration=args.duration,
        warmup=args.warmup,
        workers=args.workers,
        key_header=args.key_header,
        member_header=args.member_header,
        timeout=args.timeout,
        query=args.query,
    )
    result = run(config, os.environ.get("PERF_KEY", ""))
    summary = summarize(result)
    text = json.dumps({k: v for k, v in summary.items() if k != "timeline"}, indent=2)
    print(text)
    if args.out:
        args.out.write_text(json.dumps(summary, indent=2))
    if args.samples:
        write_samples(result, args.samples)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
