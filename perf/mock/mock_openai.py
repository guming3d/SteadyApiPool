"""A stand-in Azure OpenAI deployment for load tests: it answers like a model of a set speed.

``POST .../chat/completions`` waits for the first token (``FIRST_TOKEN_SECONDS`` plus
``PREFILL_SECONDS_PER_1K`` for every 1,000 prompt tokens), then produces ``max_tokens`` tokens
at ``TOKENS_PER_SECOND``: streamed one token a chunk when the request asks for ``stream``, or
as one answer at the end. Prompt tokens are estimated at four characters a token.

What it answers (200, 429, 500, or ``hang``: never answer) comes from the control app at
``CONTROL_URL``, polled every half second, so every replica and worker of a mock fails at the
same moment. ``MODE=control`` runs that control app: ``GET /state/<region>`` reads a mock's
status, ``POST /state/<region>`` with ``{"status": 429}`` sets it, ``POST /reset`` sets them
all back to 200.

A raw ASGI app (no framework) under uvicorn with uvloop and httptools, so one worker keeps
thousands of streams going: run it with ``uvicorn mock_openai:app --workers N``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

MODE = os.environ.get("MODE", "mock")
REGION = os.environ.get("REGION", "mock")
CONTROL_URL = os.environ.get("CONTROL_URL", "").rstrip("/")
FIRST_TOKEN_SECONDS = float(os.environ.get("FIRST_TOKEN_SECONDS", "1.0"))
PREFILL_SECONDS_PER_1K = float(os.environ.get("PREFILL_SECONDS_PER_1K", "0.1"))
TOKENS_PER_SECOND = float(os.environ.get("TOKENS_PER_SECOND", "60"))
RETRY_AFTER = os.environ.get("RETRY_AFTER", "10")
MODEL = os.environ.get("MODEL", "gpt-6-luna")
HANG_SECONDS = 900.0
# The control app also relays files between the operator and the load VM (perf/ctl.py, vm.py),
# for networks that allow neither SSH nor public storage. Off unless RELAY_TOKEN is set.
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "")
RELAY_DIR = Path(os.environ.get("RELAY_DIR", "/tmp/relay"))
RELAY_MAX_BYTES = 256 * 1024 * 1024
STATUSES = {"200", "429", "500", "hang"}


def _dumps(value: Any) -> str:
    """Compact, as Azure OpenAI writes its JSON."""
    return json.dumps(value, separators=(",", ":"))


_status = "200"
_states: dict[str, str] = {}


def _headers(content_type: bytes, extra: dict[str, str] | None = None) -> list[tuple[bytes, bytes]]:
    headers = [(b"content-type", content_type), (b"x-ms-region", REGION.encode())]
    headers += [(k.encode(), v.encode()) for k, v in (extra or {}).items()]
    return headers


async def _json(send: Send, status: int, body: Any, extra: dict[str, str] | None = None) -> None:
    data = _dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                *_headers(b"application/json", extra),
                (b"content-length", str(len(data)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})


async def _read(receive: Receive) -> bytes:
    parts = []
    while True:
        message = await receive()
        parts.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(parts)


async def _poll_control() -> None:
    """Keeps ``_status`` in step with the control app; a missed poll keeps the last status."""
    global _status
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            with contextlib.suppress(httpx.HTTPError, ValueError, KeyError):
                response = await client.get(f"{CONTROL_URL}/state/{REGION}")
                status = str(response.json()["status"])
                if status in STATUSES:
                    _status = status
            await asyncio.sleep(0.5)


def _prompt_tokens(body: dict[str, Any]) -> int:
    chars = 0
    for message in body.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += sum(
                len(str(part.get("text") or "")) for part in content if isinstance(part, dict)
            )
    return max(1, chars // 4)


async def _complete(body: dict[str, Any], send: Send) -> None:
    started = time.monotonic()
    prompt = _prompt_tokens(body)
    count = max(1, int(body.get("max_tokens") or body.get("max_completion_tokens") or 16))
    first = FIRST_TOKEN_SECONDS + PREFILL_SECONDS_PER_1K * prompt / 1000
    step = 1.0 / TOKENS_PER_SECOND
    usage = {"prompt_tokens": prompt, "completion_tokens": count, "total_tokens": prompt + count}
    base = {
        "id": f"chatcmpl-{REGION}-{time.time_ns()}",
        "created": int(time.time()),
        "model": MODEL,
    }

    if not body.get("stream"):
        await asyncio.sleep(first + step * (count - 1))
        message = {"role": "assistant", "content": "".join(f"t{i} " for i in range(count))}
        choice = {"index": 0, "message": message, "finish_reason": "length"}
        await _json(
            send, 200, {**base, "object": "chat.completion", "choices": [choice], "usage": usage}
        )
        return

    def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
        choice = {"index": 0, "delta": delta, "finish_reason": finish}
        payload = {**base, "object": "chat.completion.chunk", "choices": [choice]}
        return b"data: " + _dumps(payload).encode() + b"\n\n"

    await send(
        {"type": "http.response.start", "status": 200, "headers": _headers(b"text/event-stream")}
    )
    await send(
        {
            "type": "http.response.body",
            "body": chunk({"role": "assistant", "content": ""}),
            "more_body": True,
        }
    )
    # Every chunk differs: LiteLLM ends a stream that repeats the same chunk.
    for i in range(count):
        wait = started + first + step * i - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        await send(
            {"type": "http.response.body", "body": chunk({"content": f"t{i} "}), "more_body": True}
        )
    tail = chunk({}, "length")
    if (body.get("stream_options") or {}).get("include_usage"):
        payload = {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage}
        tail += b"data: " + _dumps(payload).encode() + b"\n\n"
    await send({"type": "http.response.body", "body": tail + b"data: [DONE]\n\n"})


async def _mock(scope: Scope, receive: Receive, send: Send) -> None:
    path, method = scope["path"], scope["method"]
    if method == "GET" and path in {"/", "/healthz"}:
        await _json(send, 200, {"region": REGION, "status": _status})
        return
    if method != "POST" or not path.endswith("/chat/completions"):
        await _json(send, 404, {"error": {"code": "404", "message": "not found"}})
        return
    raw = await _read(receive)
    status = _status
    if status == "429":
        message = f"Rate limit reached in {REGION}"
        await _json(
            send, 429, {"error": {"code": "429", "message": message}}, {"retry-after": RETRY_AFTER}
        )
        return
    if status == "500":
        await _json(send, 500, {"error": {"code": "500", "message": f"{REGION} failed"}})
        return
    if status == "hang":
        await asyncio.sleep(HANG_SECONDS)
        return
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = None
    if not isinstance(body, dict):
        await _json(send, 400, {"error": {"code": "400", "message": "not a JSON object"}})
        return
    await _complete(body, send)


async def _relay(scope: Scope, raw: bytes, send: Send) -> None:
    headers = dict(scope["headers"])
    if not RELAY_TOKEN or headers.get(b"x-relay-token", b"").decode() != RELAY_TOKEN:
        await _json(send, 403, {"error": "forbidden"})
        return
    name = scope["path"].removeprefix("/files/")
    if ".." in name.split("/") or name.startswith("/"):
        await _json(send, 400, {"error": "bad name"})
        return
    path = RELAY_DIR / name
    if scope["method"] == "PUT":
        if len(raw) > RELAY_MAX_BYTES:
            await _json(send, 413, {"error": "too large"})
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        await _json(send, 200, {"name": name, "bytes": len(raw)})
    elif scope["method"] == "GET" and (name == "" or name.endswith("/")):
        prefix = (RELAY_DIR / name) if name else RELAY_DIR
        names = (
            sorted(f.relative_to(RELAY_DIR).as_posix() for f in prefix.rglob("*") if f.is_file())
            if prefix.exists()
            else []
        )
        await _json(send, 200, {"files": names})
    elif scope["method"] == "GET" and path.is_file():
        data = path.read_bytes()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/octet-stream"),
                    (b"content-length", str(len(data)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": data})
    else:
        await _json(send, 404, {"error": "not found"})


async def _control(scope: Scope, receive: Receive, send: Send) -> None:
    path, method = scope["path"], scope["method"]
    raw = await _read(receive)
    if path.startswith("/files/"):
        await _relay(scope, raw, send)
    elif path in {"/", "/healthz"}:
        await _json(send, 200, {"states": _states})
    elif path.startswith("/state/"):
        region = path.removeprefix("/state/")
        if method == "POST":
            with contextlib.suppress(ValueError, AttributeError):
                status = str(json.loads(raw).get("status"))
                if status in STATUSES:
                    _states[region] = status
        await _json(send, 200, {"region": region, "status": _states.get(region, "200")})
    elif path == "/reset" and method == "POST":
        _states.clear()
        await _json(send, 200, {"states": _states})
    else:
        await _json(send, 404, {"error": "not found"})


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                if MODE == "mock" and CONTROL_URL:
                    scope.setdefault("state", {})["poller"] = (
                        asyncio.get_running_loop().create_task(_poll_control())
                    )
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    await (_control if MODE == "control" else _mock)(scope, receive, send)
