"""A stand-in Azure OpenAI deployment for the LiteLLM integration stack (stdlib only).

It answers ``POST /openai/deployments/<name>/chat/completions`` and ``POST /openai/responses``
like Azure OpenAI does. A chat request with ``max_tokens`` gets that many output tokens, streamed
as server-sent events when it asks for ``stream`` (calibrate.py measures the gateway with them).
``POST /control`` with ``{"status": 429}`` (or 500, or 200) switches what it answers, and
``GET /stats`` returns how many calls it has served, so a test can make one region fail and
watch the gateway move to another.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

REGION = os.environ.get("REGION", "local")
_lock = threading.Lock()
_state: dict[str, Any] = {"status": 200, "calls": 0, "failed": 0, "paths": []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    def _send(
        self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("x-ms-region", REGION)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def do_GET(self) -> None:
        if self.path.startswith("/stats"):
            with _lock:
                self._send(200, {"region": REGION, **_state})
            return
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        body = self._body()
        if self.path.startswith("/control"):
            with _lock:
                if "status" in body:
                    _state["status"] = int(body["status"])
                if body.get("reset"):
                    _state.update(calls=0, failed=0, paths=[])
                self._send(200, {"region": REGION, **_state})
            return
        with _lock:
            status = _state["status"]
            _state["paths"] = [*_state["paths"][-19:], self.path]
            if status != 200:
                _state["failed"] += 1
            else:
                _state["calls"] += 1
        if status == 429:
            self._send(
                429,
                {"error": {"code": "429", "message": f"Rate limit reached in {REGION}"}},
                {"retry-after": "1"},
            )
            return
        if status != 200:
            self._send(status, {"error": {"code": str(status), "message": f"{REGION} failed"}})
            return
        if "/responses" not in self.path and body.get("max_tokens"):
            self._generate(body)
            return
        usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
        if "/responses" in self.path:
            self._send(
                200,
                {
                    "id": f"resp_{REGION}_{int(time.time() * 1000)}",
                    "object": "response",
                    "created_at": int(time.time()),
                    "status": "completed",
                    "model": "gpt-4o-mini",
                    "output": [
                        {
                            "type": "message",
                            "id": f"msg_{REGION}",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": f"hello from {REGION}",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                },
            )
            return
        self._send(
            200,
            {
                "id": f"chatcmpl-{REGION}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"hello from {REGION}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            },
        )

    def _generate(self, body: dict[str, Any]) -> None:
        """``max_tokens`` tokens of output, whole or streamed one token a chunk."""
        count = max(1, int(body["max_tokens"]))
        prompt = sum(len(str(m.get("content") or "")) for m in body.get("messages") or []) // 4
        usage = {
            "prompt_tokens": prompt,
            "completion_tokens": count,
            "total_tokens": prompt + count,
        }
        base = {"id": f"chatcmpl-{REGION}", "created": int(time.time()), "model": "gpt-4o-mini"}
        if not body.get("stream"):
            message = {"role": "assistant", "content": "".join(f"t{i} " for i in range(count))}
            self._send(
                200,
                {
                    **base,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": usage,
                },
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-ms-region", REGION)
        self.end_headers()

        def chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> bytes:
            choice = {"index": 0, "delta": delta, "finish_reason": finish}
            payload = {**base, "object": "chat.completion.chunk", "choices": [choice], **extra}
            return f"data: {json.dumps(payload)}\n\n".encode()

        self.wfile.write(chunk({"role": "assistant", "content": ""}))
        # Every chunk differs: LiteLLM ends a stream that repeats the same chunk.
        for i in range(count):
            self.wfile.write(chunk({"content": f"t{i} "}))
        self.wfile.write(chunk({}, "stop"))
        if (body.get("stream_options") or {}).get("include_usage"):
            payload = {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage}
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
