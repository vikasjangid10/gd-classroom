"""Request context and access logging, as pure ASGI middleware.

Deliberately *not* built on ``BaseHTTPMiddleware``. That helper runs the downstream app
inside its own anyio task group, and when an unhandled exception has to travel back out
past it to a registered ``Exception`` handler, the response stream can be left waiting
forever — the client sees a request that never answers rather than a 500. Pure ASGI
middleware has no such interaction, and costs less per request besides.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import bind_contextvars, clear_contextvars, get_logger

log = get_logger(__name__)


class RequestContextMiddleware:
    """Assigns a request id, logs the access line, and guarantees an answer.

    The last part is why the catch-all lives here rather than in a registered
    ``Exception`` handler: a framework-level handler is one upgrade away from changing
    behaviour, and when it does the symptom is a request that never returns. Owning the
    last line of defence in our own ASGI layer makes that failure mode impossible.
    """

    def __init__(self, app: ASGIApp, *, expose_internals: bool = False) -> None:
        self.app = app
        self.expose_internals = expose_internals

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get("x-request-id") or uuid.uuid4().hex[:16]
        path = scope.get("path", "")
        method = scope.get("method", "")

        clear_contextvars()
        bind_contextvars(request_id=request_id, path=path, method=method)

        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id

        started = time.perf_counter()
        outcome = {"status": 500}
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                outcome["status"] = message["status"]
                MutableHeaders(scope=message)["x-request-id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            log.exception("unhandled_exception")
            if response_started:
                raise  # headers are already on the wire; nothing left to say
            await self._send_error(send_wrapper, request_id, exc)
        finally:
            # Long-lived SSE streams would report a meaningless duration.
            if not path.endswith("/events"):
                log.info(
                    "request",
                    status=outcome["status"],
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )

    async def _send_error(self, send: Send, request_id: str, exc: Exception) -> None:
        message = (
            f"{type(exc).__name__}: {exc}"
            if self.expose_internals
            else "Something went wrong on our side."
        )
        body = json.dumps(
            {
                "error": {
                    "code": "internal_error",
                    "message": message,
                    "details": {},
                    "trace_id": request_id,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
