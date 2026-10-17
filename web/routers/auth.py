"""
Login URLs (contract C11): ``GET /login``, ``GET /auth/callback``, ``POST /signout``,
the no-role page ``GET /no-role`` (M4, BR2.8), and for the closed network
(``AUTH_MODE=admin_api``, U8) ``POST /login`` (M2) and
``GET/POST /auth/new-password`` (M3).

HTTP translation only; every decision is made by
:class:`src.managers.auth_manager.AuthManager`, whose synchronous DynamoDB /
Cognito calls run in a thread pool.

The LoginTransaction (state, nonce, PKCE verifier, next) lives in the signed
``login_tx`` cookie of Starlette's ``SessionMiddleware`` for at most 10
minutes; under ``admin_api`` the same cookie carries only the raw id of the
first-password-change challenge. The login session id goes into the separate
``session_id`` cookie (HttpOnly, SameSite=Lax, Secure outside
``AUTH_MODE=local``).

M2 / M3 are full-page forms (no htmx): failures answer 200 with the form again,
and the form fields default to empty strings so a missing field never becomes
FastAPI's 422 (BR1.2, BR2.5). Passwords are never logged and never rendered
back (BR4.5).
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from src.managers.auth_manager import (
    NEW_PASSWORD_PATH,
    AdminLoginError,
    AuthManager,
    LoginFailedError,
    LoginResult,
)
from src.models.authz import Principal
from src.models.errors import ErrorKind, LookupUnavailableError
from src.models.password_policy import (
    MAX_PASSWORD_LENGTH,
    PasswordPolicy,
    PasswordRule,
)
from web.auth_middleware import (
    NO_ROLE_PATH,
    NO_STORE,
    SESSION_COOKIE_NAME,
    unavailable_response,
)
from web.error_messages import (
    error_kind_of,
    field_of,
    missed_password_rules,
    password_rule_label,
    user_message_for,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
executor = ThreadPoolExecutor(max_workers=4)

#: Key of the LoginTransaction inside the signed ``login_tx`` cookie.
LOGIN_TRANSACTION_KEY = "login_transaction"
#: Key of the first-password-change challenge id in ``login_tx`` (U8 BR2.1).
NEW_PASSWORD_CHALLENGE_KEY = "new_password_challenge"
CHALLENGE_EXPIRED_PATH = "/login?reason=challenge_expired"
_NOTICE_REASONS = {"expired", "signed_out"}
#: Notices shown on top of M2 (U8 BR1.1).
_M2_NOTICE_REASONS = {"expired", "signed_out", "challenge_expired"}
_ADMIN_API = "admin_api"

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


def _redirect(location: str) -> Response:
    response = RedirectResponse(location, status_code=303)
    response.headers["Cache-Control"] = NO_STORE
    return response


def _signed_in(result: LoginResult, manager: AuthManager) -> Response:
    return _with_session_cookie(
        RedirectResponse(result.next, status_code=303), result.session_id, manager
    )


# --- M2 / M3 rendering (U8) --------------------------------------------------------


def _login_form(
    request: Request,
    *,
    next_url: str,
    email: str = "",
    notice: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """M2. Every failure goes through here, so the body depends only on the
    wording, the e-mail address and ``next`` (NFR4.19). The password field is
    always empty."""
    return _page(
        request,
        "auth/login.html",
        {
            "title": "ログイン",
            "next": next_url,
            "email": email,
            "notice": notice if notice in _M2_NOTICE_REASONS else None,
            "error": error,
        },
    )


def _password_rules(
    policy: PasswordPolicy, missed: frozenset[PasswordRule] | None
) -> list[dict[str, Any]]:
    """The M3 rule list. ``missed`` is ``None`` before any submission."""
    return [
        {
            "key": rule.value,
            "label": password_rule_label(rule, policy),
            "met": None if missed is None else rule not in missed,
        }
        for rule in policy.rules()
    ]


def _new_password_form(
    request: Request,
    policy: PasswordPolicy,
    *,
    error: str | None = None,
    error_field: str | None = None,
    missed: frozenset[PasswordRule] | None = None,
) -> HTMLResponse:
    """M3. Neither password field is ever filled back in (BR2.5)."""
    return _page(
        request,
        "auth/new_password.html",
        {
            "title": "パスワードの設定",
            "policy": policy,
            "max_length": MAX_PASSWORD_LENGTH,
            "action": NEW_PASSWORD_PATH,
            "rules": _password_rules(policy, missed),
            "error": error,
            "error_field": error_field,
        },
    )


def _challenge_expired(request: Request) -> Response:
    request.session.pop(NEW_PASSWORD_CHALLENGE_KEY, None)
    return _redirect(CHALLENGE_EXPIRED_PATH)


# --- routes -----------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login(
    request: Request, next: str | None = None, reason: str | None = None
) -> Response:
    """Start a login, or show the expired / signed-out notice first (BR1.22).

    Under ``admin_api`` the app's own login form (M2) is the login screen and
    Cognito's screen is never used (U8 BR1.1).
    """
    manager = get_auth_manager()
    target = manager.sanitize_next(next)
    if manager.mode == _ADMIN_API:
        return _login_form(request, next_url=target, notice=reason)
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
            return _signed_in(result, manager)
        start = await _run(manager.begin_login, target)
    except LookupUnavailableError:
        logger.warning("login could not reach the session store", exc_info=True)
        return unavailable_response(request)
    request.session[LOGIN_TRANSACTION_KEY] = start.transaction
    return RedirectResponse(start.authorization_url, status_code=302)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    email: str = Form(default=""),
    password: str = Form(default=""),
    next: str = Form(default="/"),
) -> Response:
    """M2 submission (U8 WF1). Only ``admin_api`` has a login form."""
    manager = get_auth_manager()
    if manager.mode != _ADMIN_API:
        return _redirect("/login")
    target = manager.sanitize_next(next)
    try:
        outcome = await _run(
            lambda: manager.login_admin_api(
                email=email,
                password=password,
                next_url=target,
                previous_session_id=request.cookies.get(SESSION_COOKIE_NAME),
            )
        )
    except AdminLoginError as e:
        logger.info("admin_api login was not completed")
        return _login_form(
            request, next_url=target, email=email.strip(), error=user_message_for(e)
        )
    except LookupUnavailableError:
        logger.warning("login could not reach the session store", exc_info=True)
        return unavailable_response(request)
    if isinstance(outcome, LoginResult):
        return _signed_in(outcome, manager)
    request.session[NEW_PASSWORD_CHALLENGE_KEY] = outcome.challenge_id
    return _redirect(outcome.location)


@router.get(NEW_PASSWORD_PATH, response_class=HTMLResponse)
async def new_password_page(request: Request) -> Response:
    """M3 (U8 WF2 1〜2): only with a valid challenge, else back to M2."""
    manager = get_auth_manager()
    if manager.mode != _ADMIN_API:
        return _redirect("/login")
    challenge_id = request.session.get(NEW_PASSWORD_CHALLENGE_KEY)
    try:
        policy = await _run(manager.check_challenge, challenge_id)
    except AdminLoginError:
        return _challenge_expired(request)
    except LookupUnavailableError:
        logger.warning("M3 could not reach the session store", exc_info=True)
        return unavailable_response(request)
    return _new_password_form(request, policy)


@router.post(NEW_PASSWORD_PATH, response_class=HTMLResponse)
async def new_password_submit(
    request: Request,
    new_password: str = Form(default=""),
    confirmation: str = Form(default=""),
) -> Response:
    """M3 submission (U8 WF2 3〜8)."""
    manager = get_auth_manager()
    if manager.mode != _ADMIN_API:
        return _redirect("/login")
    challenge_id = request.session.get(NEW_PASSWORD_CHALLENGE_KEY)
    try:
        result = await _run(
            lambda: manager.complete_new_password(
                challenge_id=challenge_id,
                new_password=new_password,
                confirmation=confirmation,
                previous_session_id=request.cookies.get(SESSION_COOKIE_NAME),
            )
        )
    except AdminLoginError as e:
        if error_kind_of(e) is ErrorKind.UNAUTHORIZED:
            return _challenge_expired(request)
        logger.info("first password change was not completed")
        missed = missed_password_rules(e)
        return _new_password_form(
            request,
            manager.password_policy,
            error=user_message_for(e),
            error_field=field_of(e) or "new_password",
            missed=missed or None,
        )
    except LookupUnavailableError:
        logger.warning("M3 could not reach the session store", exc_info=True)
        return unavailable_response(request)
    request.session.pop(NEW_PASSWORD_CHALLENGE_KEY, None)
    return _signed_in(result, manager)


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
    return _signed_in(result, manager)


@router.get(NO_ROLE_PATH, response_class=HTMLResponse)
async def no_role(request: Request) -> Response:
    """The no-role page (M4, BR2.8): why the user cannot go further.

    Only users without a role are sent here; one who has a role (for example
    after logging in again) goes to the top page.
    """
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal) and principal.roles:
        return RedirectResponse("/", status_code=303)
    return _page(request, "auth/no_role.html", {"title": "利用権限がありません"})


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
