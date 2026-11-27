"""
CSRF protection middleware

Custom-header verification:
- For state-changing requests (POST/PUT/DELETE/PATCH),
  check that an HX-Request or X-Requested-With header is present
- htmx adds the HX-Request header automatically, so htmx requests pass
- fetch() in JS must set the X-Requested-With header explicitly
- ``/api/`` is not excluded. Only ``/health`` is exempt
- Login-related form posts (``POST /signout``) cannot carry a custom header,
  so they pass when ``Origin`` matches the app itself (scheme, host and port of the ``APP_BASE_URL`` setting)
- Entry points that run the AI (discussion and report SSE) are POST so they go through this check.
  A GET SSE guarded with ``Sec-Fetch-Site: same-origin`` was rejected because it cannot tell
  requests from an ``<img src>`` placed on the same origin
"""

from typing import Any, TextIO
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.config import config
from src.managers.auth_manager import record_event
from src.models.errors import CodedError, ErrorCode
from web.error_messages import user_message_for

UNSAFE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
EXEMPT_PATHS = {"/health"}
#: Form posts verified by Origin instead of a custom header: the closed-network
#: login and first-password-change forms.
ORIGIN_CHECKED_PATHS = {"/signout", "/login", "/auth/new-password"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin_of(url: str | None) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` with default ports filled in, or ``None``."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError:
        return None
    return scheme, parts.hostname.lower(), port


class CSRFMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests without a custom header or own Origin."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        app_base_url: str | None = None,
        log_stream: TextIO | None = None,
    ) -> None:
        super().__init__(app)
        self._allowed_origin = _origin_of(app_base_url or config.APP_BASE_URL)
        self._log_stream = log_stream

    def _passes(self, request: Request) -> bool:
        if request.headers.get("HX-Request") or request.headers.get("X-Requested-With"):
            return True
        if request.url.path in ORIGIN_CHECKED_PATHS:
            origin = _origin_of(request.headers.get("Origin"))
            return origin is not None and origin == self._allowed_origin
        return False

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if (
            request.method in UNSAFE_METHODS
            and request.url.path not in EXEMPT_PATHS
            and not self._passes(request)
        ):
            record_event(
                "auth.csrf_rejected",
                stream=self._log_stream,
                method=request.method,
                path=request.url.path,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": user_message_for(CodedError(code=ErrorCode.CSRF_FAILED)),
                    "error_code": ErrorCode.CSRF_FAILED.value,
                },
            )
        response = await call_next(request)
        return response  # type: ignore[no-any-return]
