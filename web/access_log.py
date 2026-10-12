"""
Application access log (``http.access``), replacing uvicorn's access log.

uvicorn's access log prints the full URL including the query string, which
for ``/auth/callback?code=...&state=...`` would put the authorization code and
state into CloudWatch Logs. uvicorn runs with ``--no-access-log`` and this
middleware writes one JSON line per request instead: method, path *without*
the query, status code and duration (``security-design.md``, NFR3.3).
"""

import time
from typing import TextIO

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.managers.auth_manager import record_event


class AccessLogMiddleware:
    """Outermost middleware: log every HTTP request once it has finished."""

    def __init__(self, app: ASGIApp, *, log_stream: TextIO | None = None) -> None:
        self.app = app
        self._log_stream = log_stream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_and_capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_and_capture)
        finally:
            record_event(
                "http.access",
                stream=self._log_stream,
                method=scope.get("method"),
                path=scope.get("path"),
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
