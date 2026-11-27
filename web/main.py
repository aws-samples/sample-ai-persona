"""
AI Persona System - main application
A web application based on FastAPI + Jinja2 + htmx
"""

import contextlib
import logging
import secrets
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping

import anyio
from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import (
    http_exception_handler as fastapi_http_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from src import __version__
from src.config import config
from src.managers.auth_manager import LOGIN_TRANSACTION_TTL, AuthManager
from src.managers.persona_memory_manager import prepare_long_term_memory
from src.models.errors import CodedError, ErrorCode, ErrorKind
from web.access_log import AccessLogMiddleware
from web.auth_middleware import NO_STORE, AuthMiddleware, unauthorized_response
from web.error_messages import (
    FALLBACK_MESSAGE,
    http_status_for,
    toast_response,
    user_message_for,
)
from web.middleware import CSRFMiddleware
from web.routers import (
    persona,
    persona_advanced,
    discussion,
    interview,
    api,
    settings,
    survey,
    auth,
    sharing,
)
from web.static_assets import static_url
from web.user_badge import user_badge

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Project root
PROJECT_ROOT = Path(__file__).parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """Application lifecycle management."""
    logger.info("AIペルソナシステムを起動中...")
    # Do not start if the login settings are unusable
    AuthManager.check_startup(config)
    # Look up the AgentCore Memory strategy IDs (if that fails, continue with long-term memory disabled)
    await anyio.to_thread.run_sync(prepare_long_term_memory)
    # Survey runner (every 15 seconds). Only when a state machine is configured.
    # On stop, do not start a new round; wait for the running round, then give up the lease
    runner_loop = survey.get_survey_runner_loop()
    async with runner_loop.running() if runner_loop else contextlib.nullcontext():
        yield
        logger.info("AIペルソナシステムをシャットダウン中...")


# FastAPI application
app = FastAPI(
    title="AIペルソナシステム",
    description="AIペルソナを生成し、議論を通じてインサイトを生成",
    version=__version__,
    lifespan=lifespan,
)

# Middleware (added later = outer). From the outside:
#   AccessLog -> SessionMiddleware (cookie during login) -> AuthMiddleware -> CSRF
# AuthMiddleware sits outside CSRF so that unauthenticated requests are sent to
# 401 / the login page before CSRF is checked.
app.add_middleware(CSRFMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    # local has no cookie signing key, so use a per-process random value
    secret_key=config.LOGIN_COOKIE_SIGNING_KEY or secrets.token_urlsafe(32),
    session_cookie="login_tx",
    max_age=int(LOGIN_TRANSACTION_TTL.total_seconds()),
    same_site="lax",
    https_only=config.AUTH_MODE != "local",
)
app.add_middleware(AccessLogMiddleware)

# Mount static files
app.mount(
    "/static", StaticFiles(directory=PROJECT_ROOT / "web" / "static"), name="static"
)

# Template setup
templates = Jinja2Templates(directory=PROJECT_ROOT / "web" / "templates")


from web.sanitize import render_markdown  # noqa: E402


# Add the markdown filter
templates.env.filters["markdown"] = render_markdown

# Template globals
# user_badge is the user display at the top of the screen. static_url adds a content hash to our own
# static files (so a CDN cache does not hide a deploy).
# Registered for every Router that renders base.html
templates.env.globals["app_version"] = __version__
templates.env.globals["user_badge"] = user_badge
templates.env.globals["static_url"] = static_url
for _mod in [
    persona,
    persona_advanced,
    discussion,
    interview,
    settings,
    survey,
    auth,
    sharing,
]:
    _mod.templates.env.globals["app_version"] = __version__
    _mod.templates.env.globals["user_badge"] = user_badge
    _mod.templates.env.globals["static_url"] = static_url

# Register routers
# Advanced generation (beta). Still subject to AuthMiddleware and CSRF. Registered before persona
# because it must be matched before the /persona/{persona_id}/... routes
app.include_router(
    persona_advanced.router, prefix="/persona", tags=["persona-advanced"]
)
app.include_router(persona.router, prefix="/persona", tags=["persona"])
app.include_router(discussion.router, prefix="/discussion", tags=["discussion"])
app.include_router(interview.router, prefix="/interview", tags=["interview"])
app.include_router(api.router, prefix="/api", tags=["api"])
app.include_router(settings.router, prefix="/settings", tags=["settings"])
app.include_router(survey.router, prefix="/survey", tags=["survey"])
app.include_router(auth.router, tags=["auth"])
# Sharing entry points (/share/...). Still subject to AuthMiddleware and CSRF
app.include_router(sharing.router, tags=["sharing"])


# =============================================================================
# Conversion from exceptions to HTTP responses
#
# Four handlers make the JSON API body {detail, error_code} uniform on every path.
# Exception wording, input values and IDs are kept out of the body and only logged. htmx and screens
# keep the existing per-kind display (toast, error screen, redirect to the login screen).
# =============================================================================


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _api_error(
    status_code: int,
    code: ErrorCode,
    detail: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """The JSON API error body."""
    response = JSONResponse(
        status_code=status_code,
        content={"detail": detail, "error_code": code.value},
        headers=headers,
    )
    response.headers["Cache-Control"] = NO_STORE
    return response


def _error_page(request: Request, status_code: int, message: str) -> Response:
    """Full-page error screen (partials/error_page.html)."""
    return templates.TemplateResponse(
        request,
        "partials/error_page.html",
        {
            "request": request,
            "title": "エラー",
            "error": message,
            "status_code": status_code,
            "back_url": _back_url_for(request.url.path),
        },
        status_code=status_code,
    )


async def coded_error_handler(request: Request, exc: Exception) -> Response:
    """CodedError: the status code is decided by kind (UNKNOWN is 500).

    UNAUTHORIZED: API gets 401 JSON, htmx gets HX-Redirect, screens get a
    303 to /login. Otherwise the API gets {detail, error_code}, htmx gets a toast
    (the originating hx-target is unknown, so the body is not replaced), and screens get the error screen.
    """
    assert isinstance(exc, CodedError)
    code = exc.code
    if code.kind is ErrorKind.UNAUTHORIZED:
        return unauthorized_response(request, exc)
    status_code = http_status_for(code)
    if status_code >= 500:
        logger.error(
            "CodedError %s: %s %s",
            code.value,
            request.method,
            request.url.path,
            exc_info=exc,
        )
    else:
        logger.info(
            "CodedError %s: %s %s", code.value, request.method, request.url.path
        )
    message = user_message_for(exc)
    if _is_api(request):
        return _api_error(status_code, code, message)
    if request.headers.get("HX-Request"):
        return toast_response(exc, status_code=status_code)
    return _error_page(request, status_code, message)


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    """Request validation error (422).

    Missing required Form(...) parameters and the like are
    detected by FastAPI/Pydantic before reaching the Router, so an except in the Router cannot catch them, and they do not go through the wording catalog
    (web/error_messages.py).

    - htmx: notify with a toast. This handler is shared by all forms and does not know which
      hx-target fired. Returning partial HTML would swap it into the originating hx-target (for
      persona edit, the main container) and wipe the form and all input values (#117 cause B)
    - Otherwise (JSON API etc.): 422 {detail, error_code: REQUEST_INVALID}

    `exc.errors()` contains the user's input values as `input`, so
    nothing is copied into the response. The log also omits submitted values (`input`, `ctx`) and the exception
    string (`exc_info`); only the method, path, count and field location (`loc`) are logged
    (so a password field never ends up in the log through this path).
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    locations = [
        ".".join(str(part) for part in error.get("loc", ())) for error in errors
    ]
    logger.warning(
        "リクエスト検証エラー: %s %s errors=%d loc=%s",
        request.method,
        request.url.path,
        len(errors),
        locations,
    )
    invalid = CodedError("request validation failed", code=ErrorCode.REQUEST_INVALID)
    if request.headers.get("HX-Request"):
        return toast_response(invalid, status_code=422)
    return _api_error(422, ErrorCode.REQUEST_INVALID, user_message_for(invalid))


def _wants_html_page(request: Request) -> bool:
    """Whether this is a full-page browser navigation.

    - ``/api/*`` is a JSON API, so it is excluded (keeps compatibility with `web/routers/api.py`)
    - htmx requests are excluded because each Router returns a partial for them
    - A request whose `Accept` contains ``text/html`` counts as a full-page navigation
    """
    if _is_api(request):
        return False
    if request.headers.get("HX-Request"):
        return False
    return "text/html" in request.headers.get("accept", "")


def _router_detail(exc: StarletteHTTPException) -> str | None:
    """Fixed wording a Router assembled (not an exception message). Excludes default English phrases."""
    detail = exc.detail
    if not isinstance(detail, str) or not detail:
        return None
    try:
        if detail == HTTPStatus(exc.status_code).phrase:
            return None
    except ValueError:
        pass
    return detail


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """`HTTPException`: API gets {detail, error_code: HTTP_ERROR}, screens get an HTML page.

    - API: the status code is kept. FastAPI's own default English phrases such as 404 / 405
      are replaced with the catalog wording. Fixed wording a Router assembled is used as is
      (HTTPException is gradually being replaced with CodedError)
    - Full-page endpoints (`GET /persona/{id}` etc.): return an error screen
      (banner + return link) instead of raw JSON (#117)
    - htmx and others: keep the default behavior
    """
    assert isinstance(exc, StarletteHTTPException)
    if _is_api(request):
        detail = _router_detail(exc) or user_message_for(
            CodedError(code=ErrorCode.HTTP_ERROR)
        )
        return _api_error(exc.status_code, ErrorCode.HTTP_ERROR, detail, exc.headers)
    if not _wants_html_page(request):
        return await fastapi_http_exception_handler(request, exc)

    logger.warning(
        "HTTPException: %s %s -> %s",
        request.method,
        request.url.path,
        exc.status_code,
    )
    message = _router_detail(exc) or (
        "お探しのページが見つかりません" if exc.status_code == 404 else FALLBACK_MESSAGE
    )
    return _error_page(request, exc.status_code, message)


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Unexpected exception: 500. The exception wording goes only to the log.

    Starlette calls this handler in the outermost ServerErrorMiddleware, so
    AuthMiddleware's ``no-store`` is not applied. It is added here.
    """
    logger.error("想定外の例外: %s %s", request.method, request.url.path, exc_info=exc)
    response: Response
    if _is_api(request):
        response = _api_error(500, ErrorCode.UNKNOWN, FALLBACK_MESSAGE)
    elif request.headers.get("HX-Request"):
        response = toast_response(None, status_code=500)
    else:
        response = _error_page(request, 500, FALLBACK_MESSAGE)
    response.headers["Cache-Control"] = NO_STORE
    return response


def register_exception_handlers(target: FastAPI) -> None:
    """Register the four handlers on ``target`` (also used by small apps in tests)."""
    target.add_exception_handler(CodedError, coded_error_handler)
    target.add_exception_handler(RequestValidationError, validation_exception_handler)
    target.add_exception_handler(StarletteHTTPException, http_exception_handler)
    target.add_exception_handler(Exception, unhandled_exception_handler)


register_exception_handlers(app)


def _back_url_for(path: str) -> str:
    """Infer the list screen to return to (so NOT_FOUND can navigate back)."""
    if path.startswith("/persona"):
        return "/persona/management"
    if path.startswith("/discussion"):
        return "/discussion/results"
    if path.startswith("/survey"):
        return "/survey"
    if path.startswith("/interview"):
        return "/persona/management"
    if path.startswith("/settings"):
        return "/settings"
    return "/"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Any:
    """Top page."""
    return templates.TemplateResponse(
        request, "index.html", {"request": request, "title": "AIペルソナシステム"}
    )


@app.get("/health")
async def health_check() -> Any:
    """Health check."""
    return {"status": "healthy", "version": __version__}
