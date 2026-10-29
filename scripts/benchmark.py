"""Load-tests a pool from the terminal: simulated users calling the gateway like real clients.

    set LITELLM_KEY=sk-...           (PowerShell: $env:LITELLM_KEY="sk-..."; POSIX: export ...)
    backend/.venv/Scripts/python scripts/benchmark.py --url https://<gateway>/v1 --model <pool>

    set APIM_KEY=<subscription key>
    backend/.venv/Scripts/python scripts/benchmark.py --gateway apim \
        --url https://<apim>.azure-api.net/<api path> --model <model>

It uses the same engine as the pool's call guide (backend/pool_manager/benchmark.py) and works
against any OpenAI-compatible chat completions endpoint. With --gateway apim the URL is the
API's, the key goes in APIM's subscription key header (--key-header), the api-version query is
added (--api-version) and the answering member is the x-ms-region Azure OpenAI sends. Presets:
smoke (2 users, 30 s, the default), typical (10 users, 2 min) and stress (30 users, 3 min);
--users, --duration and the other options change them. It prints a status line while it runs
and a report at the end; Ctrl+C stops it (requests already sent finish) and still prints the
report, a second Ctrl+C quits at once. Every request is billed by Foundry like real traffic.

The key comes from LITELLM_KEY or APIM_KEY (or --key, or a prompt), is only sent to the
gateway and is never written to the JSON or CSV files. Exit code: 0 when the run finished and
some requests succeeded, 1 when none did, 2 for a usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import os
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pydantic import ValidationError  # noqa: E402

from pool_manager.benchmark import (  # noqa: E402
    PRESETS,
    PROMPT_TOKENS,
    BenchmarkTarget,
    Sample,
    report,
    run_benchmark,
    summarize,
)
from pool_manager.models import (  # noqa: E402
    BENCHMARK_MAX_REQUESTS,
    BENCHMARK_MAX_SECONDS,
    BENCHMARK_MAX_USERS,
    BenchmarkSpec,
)

KEY_ENVS = {"litellm": "LITELLM_KEY", "apim": "APIM_KEY"}
# APIM's defaults: its subscription key header and the api-version the call guide uses.
APIM_KEY_HEADER = "Ocp-Apim-Subscription-Key"
APIM_API_VERSION = "2025-04-01-preview"


def chat_url(url: str, gateway: str = "litellm") -> str:
    """The chat completions URL, from the gateway's root, its /v1 or the full URL (APIM: from
    the API's URL)."""
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1") or gateway == "apim":
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


def target(args: argparse.Namespace, key: str) -> BenchmarkTarget:
    """Where the requests go and how the gateway is called."""
    url = chat_url(args.url, args.gateway)
    if args.gateway == "apim":
        return BenchmarkTarget(
            url=url,
            model=args.model,
            headers={
                "Content-Type": "application/json",
                args.key_header or APIM_KEY_HEADER: key,
            },
            params={"api-version": args.api_version or APIM_API_VERSION},
            member_header="x-ms-region",
            tokens_field="max_completion_tokens",
        )
    headers = {"Content-Type": "application/json"}
    if args.key_header:
        headers[args.key_header] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    return BenchmarkTarget(
        url=url,
        model=args.model,
        headers=headers,
        params={"api-version": args.api_version} if args.api_version else {},
        member_header="x-litellm-model-id",
        fallbacks_header="x-litellm-attempted-fallbacks",
    )


def _positive(kind: type[int] | type[float], zero: bool = False) -> Any:
    def parse(text: str) -> int | float:
        value = kind(text)
        if value < 0 or (value == 0 and not zero):
            raise argparse.ArgumentTypeError("must be 0 or more" if zero else "must be more than 0")
        return value

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmark.py",
        description="Load-test a gateway pool with simulated users.",
        epilog="The key comes from "
        + " or ".join(f"{env} ({gateway})" for gateway, env in KEY_ENVS.items())
        + ", --key or a prompt.",
    )
    parser.add_argument(
        "--gateway",
        choices=list(KEY_ENVS),
        default="litellm",
        help="the kind of gateway (default litellm)",
    )
    parser.add_argument(
        "--url",
        required=True,
        help="the gateway, e.g. https://<gateway>/v1; APIM: https://<apim>.azure-api.net/<api>",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="the pool ID or one of its aliases; APIM: the pool's model",
    )
    parser.add_argument(
        "--key-header",
        help=f"send the key in this header (APIM default {APIM_KEY_HEADER}; else Bearer)",
    )
    parser.add_argument(
        "--api-version", help=f"the api-version query (APIM default {APIM_API_VERSION})"
    )
    parser.add_argument("--preset", choices=sorted(PRESETS), default="smoke")
    parser.add_argument("--users", type=_positive(int), help="simulated users at once")
    parser.add_argument("--duration", type=_positive(int), help="seconds to run")
    parser.add_argument("--max-requests", type=_positive(int), help="stop after this many")
    parser.add_argument(
        "--ramp-up",
        type=_positive(int, zero=True),
        help="seconds over which users join",
    )
    parser.add_argument("--rate", type=_positive(float), help="at most this many requests/s")
    parser.add_argument("--prompt", choices=list(PROMPT_TOKENS), default="short")
    parser.add_argument("--max-tokens", type=_positive(int), default=128)
    parser.add_argument("--no-stream", action="store_true", help="no streaming (no TTFT)")
    parser.add_argument("--key", help="the key (prefer the environment variable: no shell history)")
    parser.add_argument("--json", type=Path, help="write the summary to this file")
    parser.add_argument("--csv", type=Path, help="write one row per request to this file")
    parser.add_argument(
        "--i-know",
        action="store_true",
        help=f"lift the limits ({BENCHMARK_MAX_USERS} users, {BENCHMARK_MAX_SECONDS} s, "
        f"{BENCHMARK_MAX_REQUESTS} requests)",
    )
    return parser


def _spec(args: argparse.Namespace) -> BenchmarkSpec:
    values: dict[str, Any] = {
        **PRESETS[args.preset],
        "prompt_size": args.prompt,
        "max_tokens": args.max_tokens,
        "stream": not args.no_stream,
        "model": args.model,
    }
    for name, option in (
        ("users", "users"),
        ("duration_seconds", "duration"),
        ("max_requests", "max_requests"),
        ("ramp_up_seconds", "ramp_up"),
        ("rate", "rate"),
    ):
        if getattr(args, option) is not None:
            values[name] = getattr(args, option)
    if args.i_know:
        values.setdefault("max_requests", 10**9)
        defaults = BenchmarkSpec().model_dump()
        return BenchmarkSpec.model_construct(**{**defaults, **values})
    return BenchmarkSpec(**values)


def _key(args: argparse.Namespace) -> str | None:
    key = args.key or os.environ.get(KEY_ENVS[args.gateway])
    if not key and sys.stdin.isatty():
        kind = "Subscription key" if args.gateway == "apim" else "Virtual key"
        key = getpass.getpass(f"{kind} (input hidden): ")
    return key.strip() if key else None


def _status(samples: list[Sample], elapsed: float, spec: BenchmarkSpec) -> str:
    summary = summarize(samples, elapsed, spec, live=True)
    rate = "-" if summary.success_rate is None else f"{summary.success_rate:.0%}"
    p50 = "-" if summary.latency_p50_ms is None else f"{summary.latency_p50_ms:,.0f}"
    p95 = "-" if summary.latency_p95_ms is None else f"{summary.latency_p95_ms:,.0f}"
    return (
        f"{elapsed:4.0f}/{spec.duration_seconds} s  {summary.requests} requests "
        f"({summary.requests_per_second:g}/s)  ok {rate}  p50 {p50} ms  p95 {p95} ms  "
        f"429 {summary.throttled}  failovers {summary.failovers}"
    )


def _write_csv(path: Path, samples: list[Sample]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        fields = list(asdict(Sample(start=0, latency_ms=0)))
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for sample in sorted(samples, key=lambda item: item.start):
            writer.writerow(asdict(sample))


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        spec = _spec(args)
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
        parser.error(f"{problems} (--i-know lifts the limits)")
    key = _key(args)
    if not key:
        parser.error(f"no key: set {KEY_ENVS[args.gateway]} or pass --key")
    destination = target(args, key)
    url = destination.url
    if args.i_know:
        print(
            "Limits lifted (--i-know): mind the cost and the rate limits.",
            file=sys.stderr,
        )
    every = f", at most {spec.rate:g} requests/s" if spec.rate else ""
    ramp = f", joining over {spec.ramp_up_seconds} s" if spec.ramp_up_seconds else ""
    print(
        f"Load test of {spec.model} at {url}: {spec.users} users for {spec.duration_seconds} s"
        f"{ramp}{every}, {args.prompt} prompts (~{PROMPT_TOKENS[spec.prompt_size]} tokens), "
        f"{destination.tokens_field} {spec.max_tokens}. Every request is billed like real traffic. "
        "Ctrl+C stops.",
        file=out,
    )

    samples: list[Sample] = []
    stop = {"asked": False}
    started = time.monotonic()
    live = sys.stderr.isatty()

    def interrupt(*_: Any) -> None:
        if stop["asked"]:
            raise KeyboardInterrupt
        stop["asked"] = True
        print(
            "\nStopping: waiting for the requests already sent (Ctrl+C again to quit).",
            file=sys.stderr,
        )

    async def run() -> float:
        async def status() -> None:
            last = 0.0
            while True:
                await asyncio.sleep(1)
                elapsed = time.monotonic() - started
                if live:
                    print(
                        f"\r{_status(samples, elapsed, spec)}   ",
                        end="",
                        file=sys.stderr,
                    )
                elif elapsed - last >= 10:
                    last = elapsed
                    print(_status(samples, elapsed, spec), file=sys.stderr)

        ticker = asyncio.create_task(status())
        try:
            return await run_benchmark(
                spec,
                destination,
                samples,
                transport=transport,
                should_stop=lambda: stop["asked"],
            )
        finally:
            ticker.cancel()

    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        elapsed = asyncio.run(run())
    except KeyboardInterrupt:
        elapsed = time.monotonic() - started
    finally:
        signal.signal(signal.SIGINT, previous)
    if live:
        print(file=sys.stderr)

    summary = summarize(samples, elapsed, spec)
    print(report(summary, model=spec.model or args.model, spec=spec), file=out)
    if args.json:
        result = {
            "url": url,
            "model": spec.model,
            "spec": spec.model_dump(),
            "stopped": stop["asked"],
            "summary": summary.model_dump(),
        }
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"Summary written to {args.json}", file=out)
    if args.csv:
        _write_csv(args.csv, samples)
        print(f"{len(samples)} requests written to {args.csv}", file=out)
    return 0 if summary.successes else 1


if __name__ == "__main__":
    sys.exit(main())
