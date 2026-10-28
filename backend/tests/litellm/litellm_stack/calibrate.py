"""Measures what a call costs the gateway, for the sizer (pool_manager/litellm/sizing.py).

Runs the integration stack with docker-compose.calibrate.yml (LiteLLM configured as this app
deploys it), sends batches of calls through a virtual key, and reads the LiteLLM container's own
CPU counter (cgroup ``cpu.stat``) before and after each batch, so the client's CPU doesn't count.
The fake upstream answers at once, so only the gateway's work is measured. Then it fits

    CPU ms per call = per request + per 1,000 tokens of bodies + per streamed token

and runs one replica flat out to see what one worker reaches. Free: nothing leaves the machine.

    cd backend && .venv/Scripts/python tests/litellm/litellm_stack/calibrate.py [--requests 400]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2]))

from pool_manager.litellm.gateway_template import litellm_config  # noqa: E402
from pool_manager.litellm.resources import to_yaml  # noqa: E402

GATEWAY = "http://localhost:14000"
MASTER_KEY = "sk-test-master-key-0123456789"
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(HERE / "docker-compose.yml"),
    "-f",
    str(HERE / "docker-compose.calibrate.yml"),
]
MODEL = "calibrate"
# (streamed, input tokens, output tokens)
CASES = [
    (False, 100, 10),
    (False, 100, 500),
    (False, 100, 2000),
    (False, 5000, 10),
    (False, 20000, 10),
    (True, 100, 10),
    (True, 100, 500),
    (True, 100, 2000),
    (True, 5000, 500),
]


@dataclass
class Result:
    streamed: bool
    input_tokens: int
    output_tokens: int
    requests: int
    failed: int
    seconds: float
    cpu_ms: float

    @property
    def per_call(self) -> float:
        return self.cpu_ms / max(self.requests - self.failed, 1)


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def _cpu_usec(container: str) -> int:
    stat = _run("docker", "exec", container, "cat", "/sys/fs/cgroup/cpu.stat")
    return next(int(line.split()[1]) for line in stat.splitlines() if line.startswith("usage_usec"))


def start(telemetry: bool = True) -> str:
    """The stack with the deployed config; returns the LiteLLM container's ID."""
    config = litellm_config(redis=False)
    if not telemetry:
        config["litellm_settings"]["callbacks"] = []
    # The stack has no managed identity; members use a key.
    config["litellm_settings"].pop("enable_azure_ad_token_refresh", None)
    (HERE / "config.calibrate.yaml").write_text(to_yaml(config), encoding="utf-8")
    _run(*COMPOSE, "up", "-d", "--wait", "--force-recreate")
    return _run(*COMPOSE, "ps", "-q", "litellm")


def prepare(client: httpx.Client) -> str:
    """One model on region-a and a virtual key for it, as clients call a pool."""
    headers = {"Authorization": f"Bearer {MASTER_KEY}"}
    client.post("/model/delete", json={"id": f"{MODEL}-a"}, headers=headers)
    created = client.post(
        "/model/new",
        headers=headers,
        json={
            "model_name": MODEL,
            "litellm_params": {
                "model": "azure/gpt-4o-mini",
                "api_base": "http://region-a:8080",
                "api_key": "key-a",
                "api_version": "2024-10-21",
            },
            "model_info": {"id": f"{MODEL}-a"},
        },
    )
    created.raise_for_status()
    key = client.post("/key/generate", headers=headers, json={"models": [MODEL]})
    key.raise_for_status()
    return str(key.json()["key"])


async def batch(
    key: str, streamed: bool, input_tokens: int, output_tokens: int, requests: int, users: int
) -> tuple[int, float]:
    """Sends ``requests`` calls with ``users`` at a time; returns the failures and seconds."""
    body: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hello " * input_tokens}],
        "max_tokens": output_tokens,
    }
    if streamed:
        body |= {"stream": True, "stream_options": {"include_usage": True}}
    queue = list(range(requests))
    failed = 0
    limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
    async with httpx.AsyncClient(
        base_url=GATEWAY,
        headers={"Authorization": f"Bearer {key}"},
        timeout=120,
        limits=limits,
    ) as client:

        async def user() -> None:
            nonlocal failed
            while queue:
                queue.pop()
                try:
                    async with client.stream("POST", "/v1/chat/completions", json=body) as reply:
                        tail = b""
                        async for data in reply.aiter_bytes():
                            tail = (tail + data)[-64:]
                        # A stream that broke off still started with a 200.
                        if reply.status_code != 200 or (streamed and b"[DONE]" not in tail):
                            failed += 1
                except httpx.HTTPError:
                    failed += 1

        started = time.perf_counter()
        await asyncio.gather(*(user() for _ in range(users)))
        return failed, time.perf_counter() - started


def measure(
    container: str, key: str, case: tuple[bool, int, int], requests: int, users: int
) -> Result:
    streamed, input_tokens, output_tokens = case
    asyncio.run(batch(key, streamed, input_tokens, output_tokens, max(20, requests // 10), users))
    before = _cpu_usec(container)
    failed, seconds = asyncio.run(
        batch(key, streamed, input_tokens, output_tokens, requests, users)
    )
    cpu_ms = (_cpu_usec(container) - before) / 1000
    return Result(streamed, input_tokens, output_tokens, requests, failed, seconds, cpu_ms)


def fit(results: list[Result]) -> tuple[float, float, float]:
    """Least squares of CPU ms per call on [1, body tokens / 1000, streamed tokens]."""
    rows = [
        (1.0, (r.input_tokens + r.output_tokens) / 1000, r.output_tokens if r.streamed else 0.0)
        for r in results
    ]
    ys = [r.per_call for r in results]
    n = 3
    a = [[sum(row[i] * row[j] for row in rows) for j in range(n)] for i in range(n)]
    b = [sum(row[i] * y for row, y in zip(rows, ys, strict=True)) for i in range(n)]
    # Gaussian elimination on the 3x3 normal equations.
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        b[col], b[pivot] = b[pivot], b[col]
        for r in range(n):
            if r != col and a[col][col]:
                factor = a[r][col] / a[col][col]
                a[r] = [x - factor * y for x, y in zip(a[r], a[col], strict=True)]
                b[r] -= factor * b[col]
    request, body, streamed = (b[i] / a[i][i] for i in range(n))
    return request, body, streamed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", type=int, default=400, help="calls per case")
    parser.add_argument("--users", type=int, default=16, help="calls at a time")
    parser.add_argument("--keep", action="store_true", help="leave the stack running")
    parser.add_argument(
        "--without-telemetry",
        action="store_true",
        help="LiteLLM without its OpenTelemetry callback",
    )
    parser.add_argument(
        "--cases",
        nargs="*",
        default=None,
        help="only these, as streamed:input:output (yes:100:500)",
    )
    args = parser.parse_args(argv)
    cases = CASES
    if args.cases:
        cases = [
            (streamed == "yes", int(inputs), int(outputs))
            for streamed, inputs, outputs in (case.split(":") for case in args.cases)
        ]
    container = start(telemetry=not args.without_telemetry)
    try:
        with httpx.Client(base_url=GATEWAY, timeout=60) as client:
            for _ in range(60):
                try:
                    if client.get("/health/readiness").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(2)
            key = prepare(client)
        results = []
        print("streamed  input  output  calls  failed  calls/s  CPU ms/call")
        for case in cases:
            result = measure(container, key, case, args.requests, args.users)
            results.append(result)
            print(
                f"{'yes' if result.streamed else 'no':>8} {result.input_tokens:>6} "
                f"{result.output_tokens:>7} {result.requests:>6} {result.failed:>7} "
                f"{result.requests / result.seconds:>8.1f} {result.per_call:>12.2f}"
            )
        if len(results) < 3:
            return 0
        request, body, streamed = fit(results)
        # One replica flat out: short calls, many at a time, for what one worker reaches.
        flat = measure(container, key, (False, 100, 10), args.requests * 3, 64)
        cores = flat.cpu_ms / 1000 / flat.seconds
        summary = {
            "cpu_ms_per_request": round(request, 2),
            "cpu_ms_per_1k_tokens": round(body, 3),
            "cpu_ms_per_streamed_token": round(streamed, 4),
            "flat_out_calls_per_second": round(flat.requests / flat.seconds, 1),
            "flat_out_cores": round(cores, 2),
            "flat_out_failed": flat.failed,
        }
        print(json.dumps(summary, indent=2))
    finally:
        if not args.keep:
            _run(*COMPOSE, "down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
