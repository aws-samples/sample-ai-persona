"""
Login URLs (contract C11): ``GET /login``, ``GET /auth/callback``, ``POST /signout``.

HTTP translation only; every decision is made by
:class:`src.managers.auth_manager.AuthManager`, whose synchronous DynamoDB /
Cognito calls run in a thread pool.

The LoginTransaction (state, nonce, PKCE verifier, next) lives in the signed
``login_tx`` cookie of Starlette's ``SessionMiddleware`` for at most 10
minutes; the login session id goes into the separate ``session_id`` cookie
(HttpOnly, SameSite=Lax, Secure outside ``AUTH_MODE=local``).
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from src.managers.auth_manager import AuthManager, LoginFailedError
from src.models.errors import LookupUnavailableError
from web.auth_middleware import NO_STORE, SESSION_COOKIE_NAME, unavailable_response

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
executor = ThreadPoolExecutor(max_workers=4)

#: Key of the LoginTransaction inside the signed ``login_tx`` cookie.
LOGIN_TRANSACTION_KEY = "login_transaction"
_NOTICE_REASONS = {"expired", "signed_out"}

_auth_manager: AuthManager | None = None


def get_auth_manager() -> AuthManager:
    """Return the process-wide AuthManager (lazy)."""
    global _auth_manager
    if _auth_manager is None:
        _auth_manager = AuthManager()
    return _auth_manager


async def _run(func: Callable[..., Any], *args: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, lambda: func(*args))


def _with_session_cookie(
    response: Response, session_id: str, manager: AuthManager
) -> Response:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        path="/",
        httponly=True,
        samesite="lax",
        secure=manager.secure_cookies,
    )
    response.headers["Cache-Control"] = NO_STORE
    return response


def _page(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    response = templates.TemplateResponse(request, name, context)
    response.headers["Cache-Control"] = NO_STORE
    return response


@router.get("/login", response_class=HTMLResponse)
async def login(
    request: Request, next: str | None = None, reason: str | None = None
) -> Response:
    """Start a login, or show the expired / signed-out notice first (BR1.22)."""
    manager = get_auth_manager()
    target = manager.sanitize_next(next)
    if reason in _NOTICE_REASONS:
        return _page(
            request,
            "auth/login_notice.html",
            {
                "title": "ログイン",
                "reason": reason,
                "login_url": "/login?" + urlencode({"next": target}),
            },
        )
    try:
        if manager.mode == "local":
            result = await _run(
                manager.login_local, target, request.cookies.get(SESSION_COOKIE_NAME)
            )
            return _with_session_cookie(
                RedirectResponse(result.next, status_code=303),
                result.session_id,
                manager,
            )
        start = await _run(manager.begin_login, target)
    except LookupUnavailableError:
        logger.warning("login could not reach the session store", exc_info=True)
        return unavailable_response(request)
    request.session[LOGIN_TRANSACTION_KEY] = start.transaction
    return RedirectResponse(start.authorization_url, status_code=302)


@router.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """Accept the Cognito redirect and create the session (WF1 5〜8)."""
    manager = get_auth_manager()
    transaction = request.session.pop(LOGIN_TRANSACTION_KEY, None)
    try:
        result = await _run(
            lambda: manager.complete_login(
                transaction,
                state=state,
                code=code,
                error=error,
                previous_session_id=request.cookies.get(SESSION_COOKIE_NAME),
            )
        )
    except LoginFailedError:
        logger.info("login callback was not accepted")
        return _page(request, "auth/login_failed.html", {"title": "ログイン"})
    except LookupUnavailableError:
        logger.warning("login could not reach the session store", exc_info=True)
        return unavailable_response(request)
    return _with_session_cookie(
        RedirectResponse(result.next, status_code=303), result.session_id, manager
    )


@router.post("/signout")
async def signout(request: Request) -> Response:
    """End the login here and at Cognito, then go to the login page (WF4)."""
    manager = get_auth_manager()
    location = await _run(manager.logout, request.cookies.get(SESSION_COOKIE_NAME))
    response = RedirectResponse(location, status_code=303)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=manager.secure_cookies,
    )
    response.headers["Cache-Control"] = NO_STORE
    return response
