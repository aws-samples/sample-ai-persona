"""
Login check at the entrance of every request (``AuthMiddleware``).

A plain ASGI middleware (not ``BaseHTTPMiddleware``) so that SSE streams pass
through untouched. Rules (``functional-design/rules.md``):

- BR1.1: deny by default; only :data:`PUBLIC_PATHS` and :data:`PUBLIC_PREFIXES`
  pass without a session. ``tests/api/test_auth_middleware.py`` enumerates
  every registered route against this list (NFR4.1).
- BR1.2: not signed in -> ``/api/*`` 401 JSON, htmx 401 + ``HX-Redirect``,
  pages 303 to ``/login?next=...`` (``&reason=expired`` when expired).
- BR1.9: :data:`PASSIVE_REQUESTS` (SSE streams, polling) do not count as
  activity. Each entry is also marked with a comment next to its route.
- BR1.18 / NFR9.8: Sessions table unreachable -> 503, never "signed in".
- BR1.20: every signed-in response gets ``Cache-Control: no-store``.
- BR2.8 (U2, replaces U1's "no role still passes"): a signed-in user without
  a role may only use :data:`ROLE_EXEMPT_PATHS`; pages go to the no-role page
  (303), htmx gets ``HX-Redirect``, ``/api/*`` gets 403 ``ROLE_REQUIRED``, and
  ``authz.denied`` (``role_required``) is logged.

The session check is synchronous (DynamoDB / Cognito), so it runs in a worker
thread (``anyio.to_thread``) and never blocks the event loop; ``/health`` and
static files are answered before any lookup (``reliability-design.md``).
The resolved :class:`~src.models.authz.Principal` is put on
``request.state.principal`` (contract C1).
"""

import random
import re
import time
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, TextIO
from urllib.parse import urlencode, urlsplit

import anyio
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.config import config
from src.managers.auth_manager import record_event
from src.models.errors import (
    AuthRequiredError,
    CodedError,
    ErrorCode,
    LookupUnavailableError,
    RoleRequiredError,
)
from web.error_messages import toast_response, user_message_for

if TYPE_CHECKING:
    from src.managers.auth_manager import AuthManager

SESSION_COOKIE_NAME = "session_id"

#: URLs reachable without a login (contract C11 ``security: []``).
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/api/health",
        "/login",
        "/auth/callback",
        # U8: the first password change (M3) happens before a session exists
        "/auth/new-password",
        "/signout",
    }
)
#: Static assets (CSS / JS only; the login pages need them). Never user data.
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)

#: The no-role page (M4, BR2.8).
NO_ROLE_PATH = "/no-role"
#: URLs a signed-in user *without a role* may use, besides the public ones
#: above (BR2.8): the no-role page itself and signing out.
ROLE_EXEMPT_PATHS: frozenset[str] = frozenset({NO_ROLE_PATH, "/signout"})

#: Requests the user did not trigger (BR1.9). ``(method, path pattern)``.
PASSIVE_REQUESTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # web/routers/discussion.py stream_discussion (SSE)
    ("GET", re.compile(r"/discussion/stream")),
    # web/routers/interview.py stream_interview_messages (SSE)
    ("GET", re.compile(r"/interview/[^/]+/stream")),
    # web/routers/interview.py get_session_status (status polling)
    ("GET", re.compile(r"/interview/[^/]+/status")),
    # web/routers/survey.py Nemotron download status (hx-trigger="every 5s")
    ("GET", re.compile(r"/survey/persona-data/nemotron-status")),
    # web/routers/api.py get_job (job status polling)
    ("GET", re.compile(r"/api/jobs/[^/]+")),
)

NO_STORE = "no-store"


def is_public_path(path: str) -> bool:
    """Whether ``path`` is reachable without a login."""
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def is_role_exempt_path(path: str) -> bool:
    """Whether a signed-in user without a role may use ``path`` (BR2.8)."""
    return is_public_path(path) or path in ROLE_EXEMPT_PATHS


def is_passive_request(method: str, path: str) -> bool:
    """Whether the request is an SSE stream or polling (BR1.9)."""
    return any(
        method == expected and pattern.fullmatch(path)
        for expected, pattern in PASSIVE_REQUESTS
    )


def _login_location(next_url: str, *, expired: bool) -> str:
    params = {"next": next_url}
    if expired:
        params["reason"] = "expired"
    return "/login?" + urlencode(params)


def _current_page(request: Request) -> str:
    """The page the user is on: ``HX-Current-URL`` for htmx, else this URL."""
    if request.headers.get("HX-Request"):
        current = request.headers.get("HX-Current-URL")
        if current:
            parts = urlsplit(current)
            return parts.path + (f"?{parts.query}" if parts.query else "") or "/"
        return "/"
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=config.AUTH_MODE != "local",
    )


def unauthorized_response(
    request: Request, exc: CodedError, *, clear_cookie: bool = False
) -> Response:
    """The not-signed-in answer of BR1.2 for this kind of request.

    Only ``exc.code`` is read; the exception message never reaches the body.
    """
    code = exc.code
    expired = code is ErrorCode.SESSION_EXPIRED
    response: Response
    if request.url.path.startswith("/api/"):
        response = JSONResponse(
            status_code=401,
            content={
                "detail": user_message_for(CodedError(code=code)),
                "error_code": code.value,
            },
        )
    elif request.headers.get("HX-Request"):
        response = Response(
            status_code=401,
            headers={
                "HX-Redirect": _login_location(_current_page(request), expired=expired)
            },
        )
    else:
        response = RedirectResponse(
            _login_location(_current_page(request), expired=expired), status_code=303
        )
    response.headers["Cache-Control"] = NO_STORE
    if clear_cookie:
        _clear_session_cookie(response)
    return response


def role_required_response(request: Request) -> Response:
    """The no-role answer of BR2.8: M4 for pages and htmx, 403 for the API."""
    response: Response
    if request.url.path.startswith("/api/"):
        code = ErrorCode.ROLE_REQUIRED
        response = JSONResponse(
            status_code=403,
            content={
                "detail": user_message_for(RoleRequiredError("no role")),
                "error_code": code.value,
            },
        )
    elif request.headers.get("HX-Request"):
        response = Response(status_code=403, headers={"HX-Redirect": NO_ROLE_PATH})
    else:
        response = RedirectResponse(NO_ROLE_PATH, status_code=303)
    response.headers["Cache-Control"] = NO_STORE
    return response


def unavailable_response(request: Request) -> Response:
    """503 when the login cannot be confirmed (BR1.18, NFR9.8)."""
    error = LookupUnavailableError("login lookup unavailable")
    message = user_message_for(error)
    response: Response
    if request.url.path.startswith("/api/"):
        response = JSONResponse(
            status_code=503,
            content={
                "detail": message,
                "error_code": ErrorCode.LOOKUP_UNAVAILABLE.value,
            },
        )
    elif request.headers.get("HX-Request"):
        response = toast_response(error, status_code=503)
    else:
        response = HTMLResponse(
            status_code=503,
            content=(
                '<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">'
                "<title>AIペルソナシステム</title></head><body>"
                f'<p data-testid="lookup-unavailable">{message}</p></body></html>'
            ),
        )
    response.headers["Cache-Control"] = NO_STORE
    return response


def _default_manager_getter() -> "AuthManager":
    from web.routers.auth import get_auth_manager

    return get_auth_manager()


class AuthMiddleware:
    """Deny-by-default login check that puts the Principal on the request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        manager_getter: Callable[[], "AuthManager"] | None = None,
        sample_rate: float | None = None,
        log_stream: TextIO | None = None,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self.app = app
        self._manager_getter = manager_getter or _default_manager_getter
        self._sample_rate = (
            config.AUTH_TIMING_SAMPLE_RATE if sample_rate is None else sample_rate
        )
        self._log_stream = log_stream
        self._random = random_source

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_public_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        manager = self._manager_getter()
        started = time.perf_counter()
        try:
            principal = await anyio.to_thread.run_sync(
                partial(
                    manager.check_request,
                    session_id,
                    passive=is_passive_request(request.method, scope["path"]),
                )
            )
        except AuthRequiredError as e:
            response = unauthorized_response(
                request, e, clear_cookie=session_id is not None
            )
            await response(scope, receive, send)
            return
        except LookupUnavailableError:
            await unavailable_response(request)(scope, receive, send)
            return
        self._log_timing(started, principal.sub)
        scope.setdefault("state", {})["principal"] = principal
        if not principal.roles and not is_role_exempt_path(scope["path"]):
            # BR2.8: no role -> only the no-role page. Decided from the session's
            # roles of this request, so a newly assigned role works after the
            # next login or token refresh (NFR5.5).
            record_event(
                "authz.denied",
                stream=self._log_stream,
                reason="role_required",
                sub=principal.sub,
            )
            await role_required_response(request)(scope, receive, send)
            return
        await self.app(scope, receive, self._no_store(send))

    def _log_timing(self, started: float, sub: str) -> None:
        if self._sample_rate > 0 and self._random() < self._sample_rate:
            record_event(
                "auth.request_checked",
                stream=self._log_stream,
                sub=sub,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )

    @staticmethod
    def _no_store(send: Send) -> Callable[[Message], Any]:
        """Force ``no-store`` unless the route chose a ``private`` policy.

        A ``private`` value is an explicit browser-only cache decision of the
        route (the persona avatar redirect keeps ``private, max-age=300``);
        shared caches such as CloudFront never store it either way.
        """

        async def send_with_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "private" not in headers.get("Cache-Control", "").lower():
                    headers["Cache-Control"] = NO_STORE
            await send(message)

        return send_with_no_store
