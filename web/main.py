"""
AIペルソナシステム - メインアプリケーション
FastAPI + Jinja2 + htmxベースのWebアプリケーション
"""

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

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# プロジェクトルート
PROJECT_ROOT = Path(__file__).parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """アプリケーションのライフサイクル管理"""
    logger.info("AIペルソナシステムを起動中...")
    # ログインの設定が使えなければ起動しない（BR1.15、NFR4.7）
    AuthManager.check_startup(config)
    # AgentCore Memory の Strategy ID を引く（引けなければ長期記憶を無効にして続ける）
    await anyio.to_thread.run_sync(prepare_long_term_memory)
    yield
    logger.info("AIペルソナシステムをシャットダウン中...")


# FastAPIアプリケーション
app = FastAPI(
    title="AIペルソナシステム",
    description="AIペルソナを生成し、議論を通じてインサイトを生成",
    version=__version__,
    lifespan=lifespan,
)

# ミドルウェア（後に足したものほど外側）。外側から順に:
#   AccessLog → SessionMiddleware（ログイン途中の Cookie）→ AuthMiddleware → CSRF
# AuthMiddleware を CSRF より外に置くのは、未ログインの要求を CSRF より先に
# 401 / ログイン画面へ移すため。
app.add_middleware(CSRFMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    # local では Cookie の署名鍵を持たないため、プロセスごとの乱数にする
    secret_key=config.LOGIN_COOKIE_SIGNING_KEY or secrets.token_urlsafe(32),
    session_cookie="login_tx",
    max_age=int(LOGIN_TRANSACTION_TTL.total_seconds()),
    same_site="lax",
    https_only=config.AUTH_MODE != "local",
)
app.add_middleware(AccessLogMiddleware)

# 静的ファイルのマウント
app.mount(
    "/static", StaticFiles(directory=PROJECT_ROOT / "web" / "static"), name="static"
)

# テンプレート設定
templates = Jinja2Templates(directory=PROJECT_ROOT / "web" / "templates")


from web.sanitize import render_markdown  # noqa: E402


# マークダウンフィルターを追加
templates.env.filters["markdown"] = render_markdown

# テンプレートグローバル変数
# user_badge は画面上部の利用者表示（BR1.16）。static_url は自前の静的ファイルに
# 中身のハッシュを付ける（CDN のキャッシュでデプロイが隠れないように）。
# base.html を描く全 Router に登録する
templates.env.globals["app_version"] = __version__
templates.env.globals["user_badge"] = user_badge
templates.env.globals["static_url"] = static_url
for _mod in [persona, discussion, interview, settings, survey, auth, sharing]:
    _mod.templates.env.globals["app_version"] = __version__
    _mod.templates.env.globals["user_badge"] = user_badge
    _mod.templates.env.globals["static_url"] = static_url

# ルーターの登録
app.include_router(persona.router, prefix="/persona", tags=["persona"])
app.include_router(discussion.router, prefix="/discussion", tags=["discussion"])
app.include_router(interview.router, prefix="/interview", tags=["interview"])
app.include_router(api.router, prefix="/api", tags=["api"])
app.include_router(settings.router, prefix="/settings", tags=["settings"])
app.include_router(survey.router, prefix="/survey", tags=["survey"])
app.include_router(auth.router, tags=["auth"])
# 共有の入口（/share/...、U4）。AuthMiddleware と CSRF の対象のまま（NFR4.11）
app.include_router(sharing.router, tags=["sharing"])


# =============================================================================
# 例外から HTTP 応答への変換（約束 C13、BR2.14）
#
# 4 つのハンドラで、どの経路でも JSON API の本文を {detail, error_code} にそろえる。
# 例外の文言・入力値・ID は本文に出さず、ログだけに出す（NFR3.6）。htmx と画面は
# 既存の種類ごとの表示（トースト・エラー画面・ログイン画面への移動）のまま。
# =============================================================================


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _api_error(
    status_code: int,
    code: ErrorCode,
    detail: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """The JSON API error body of contract C12 / C13."""
    response = JSONResponse(
        status_code=status_code,
        content={"detail": detail, "error_code": code.value},
        headers=headers,
    )
    response.headers["Cache-Control"] = NO_STORE
    return response


def _error_page(request: Request, status_code: int, message: str) -> Response:
    """フルページのエラー画面（partials/error_page.html）。"""
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
    """CodedError: 種類で状態コードを決める（UNKNOWN は 500）。

    UNAUTHORIZED は U1 の扱い（API は 401 の JSON、htmx は HX-Redirect、画面は
    /login への 303）。それ以外は API なら {detail, error_code}、htmx はトースト
    （発火元の hx-target を知らないため本文を差し替えない）、画面はエラー画面。
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
    """リクエスト検証エラー（422）。

    Form(...) の必須パラメータ欠落などは FastAPI/Pydantic がRouterに入る前に
    検出するため、Router内の except では捕捉できず、文言カタログ
    （web/error_messages.py）を経由しない。

    - htmx: トーストで通知する。このハンドラは全フォーム共通で、どの hx-target が
      発火元かを知らない。パーシャルHTMLを返すと発火元の hx-target（ペルソナ編集
      なら本体コンテナ）へスワップされ、フォームと入力値がすべて消える（#117 原因B）
    - それ以外（JSON API など）: 422 の {detail, error_code: REQUEST_INVALID}
      （約束 C13）

    `exc.errors()` には利用者の入力値が `input` として含まれるため、
    レスポンスには一切転写しない（診断はログのみ）。
    """
    logger.warning(
        "リクエスト検証エラー: %s %s", request.method, request.url.path, exc_info=exc
    )
    invalid = CodedError("request validation failed", code=ErrorCode.REQUEST_INVALID)
    if request.headers.get("HX-Request"):
        return toast_response(invalid, status_code=422)
    return _api_error(422, ErrorCode.REQUEST_INVALID, user_message_for(invalid))


def _wants_html_page(request: Request) -> bool:
    """ブラウザのフルページ遷移かどうか。

    - ``/api/*`` はJSON APIなので対象外（`web/routers/api.py` の互換を保つ）
    - htmx リクエストは各Routerがパーシャルを返す設計なので対象外
    - `Accept` に ``text/html`` を含むものをフルページ遷移とみなす
    """
    if _is_api(request):
        return False
    if request.headers.get("HX-Request"):
        return False
    return "text/html" in request.headers.get("accept", "")


def _router_detail(exc: StarletteHTTPException) -> str | None:
    """Router が組み立てた固定文言（例外メッセージではない）。既定の英語の句は除く。"""
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
    """`HTTPException`: API は {detail, error_code: HTTP_ERROR}、画面はHTMLページ。

    - API: 状態コードはそのまま。FastAPI 自身の 404・405 など既定の英語の句は
      カタログの文言に置き換える。Router が組み立てた固定文言はそのまま使う
      （v1.0.0 では各 Unit が HTTPException を CodedError に置き換えていく）
    - フルページ表示のエンドポイント（`GET /persona/{id}` 等）: 生のJSONではなく
      エラー画面（バナー + 復帰リンク）を返す（#117）
    - htmx・その他: 既定の挙動を維持する
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
    """想定外の例外: 500。例外の文言はログだけに出す（NFR9.21）。

    Starlette はこのハンドラを一番外側の ServerErrorMiddleware で呼ぶため、
    AuthMiddleware の ``no-store`` が付かない。ここで付ける。
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
    """約束 C13 の 4 つのハンドラを ``target`` に登録する（テストの小さなアプリでも使う）。"""
    target.add_exception_handler(CodedError, coded_error_handler)
    target.add_exception_handler(RequestValidationError, validation_exception_handler)
    target.add_exception_handler(StarletteHTTPException, http_exception_handler)
    target.add_exception_handler(Exception, unhandled_exception_handler)


register_exception_handlers(app)


def _back_url_for(path: str) -> str:
    """復帰先の一覧画面を推定する（NOT_FOUND から戻れるようにする）。"""
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
    """トップページ"""
    return templates.TemplateResponse(
        request, "index.html", {"request": request, "title": "AIペルソナシステム"}
    )


@app.get("/health")
async def health_check() -> Any:
    """ヘルスチェック"""
    return {"status": "healthy", "version": __version__}
