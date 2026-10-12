"""
AIペルソナシステム - メインアプリケーション
FastAPI + Jinja2 + htmxベースのWebアプリケーション
"""

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import (
    http_exception_handler as fastapi_http_exception_handler,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from src import __version__
from src.config import config
from src.managers.auth_manager import LOGIN_TRANSACTION_TTL, AuthManager
from src.managers.persona_memory_manager import prepare_long_term_memory
from src.models.errors import CodedError, ErrorKind
from web.access_log import AccessLogMiddleware
from web.auth_middleware import AuthMiddleware, unauthorized_response
from web.error_messages import FALLBACK_MESSAGE, toast_response
from web.middleware import CSRFMiddleware
from web.routers import persona, discussion, interview, api, settings, survey, auth
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
# user_badge は画面上部の利用者表示（BR1.16）。base.html を描く全 Router に登録する
templates.env.globals["app_version"] = __version__
templates.env.globals["user_badge"] = user_badge
for _mod in [persona, discussion, interview, settings, survey, auth]:
    _mod.templates.env.globals["app_version"] = __version__
    _mod.templates.env.globals["user_badge"] = user_badge

# ルーターの登録
app.include_router(persona.router, prefix="/persona", tags=["persona"])
app.include_router(discussion.router, prefix="/discussion", tags=["discussion"])
app.include_router(interview.router, prefix="/interview", tags=["interview"])
app.include_router(api.router, prefix="/api", tags=["api"])
app.include_router(settings.router, prefix="/settings", tags=["settings"])
app.include_router(survey.router, prefix="/survey", tags=["survey"])
app.include_router(auth.router, tags=["auth"])


@app.exception_handler(CodedError)
async def coded_error_handler(request: Request, exc: CodedError) -> Response:
    """約束 C13 のうち U1 が通る経路: UNAUTHORIZED の種類だけを扱う。

    API は 401 の JSON、htmx は HX-Redirect、画面は /login への 303。
    ほかの種類はこれまでどおり既定の扱い（500）に任せる（残りは U2）。
    """
    if exc.code.kind is ErrorKind.UNAUTHORIZED:
        return unauthorized_response(request, exc)
    raise exc


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    """リクエスト検証エラー（422）をhtmx向けのパーシャルHTMLに変換する。

    Form(...) の必須パラメータ欠落などは FastAPI/Pydantic がRouterに入る前に
    検出するため、Router内の except では捕捉できず、文言カタログ
    （web/error_messages.py）を経由しない。htmx はレスポンス本文を描画せず
    app.js の汎用フォールバックにフォールスルーするため、そのままでは
    「エラーが発生しました」しか表示されない。

    `exc.errors()` には利用者の入力値が `input` として含まれるため、
    レスポンスには一切転写しない（診断はログのみ）。これは Router層で
    `str(e)` を書かない原則（.claude/rules/architecture.md）と同じ扱い。
    """
    logger.warning(
        "リクエスト検証エラー: %s %s", request.method, request.url.path, exc_info=True
    )
    if request.headers.get("HX-Request"):
        # このハンドラは全フォーム共通で、どの hx-target が発火元かを知らない。
        # パーシャルHTMLを返すと発火元の hx-target（ペルソナ編集なら本体
        # コンテナ）へスワップされ、フォームと入力値がすべて消える（#117 原因B）。
        # DOMを書き換えず入力を保持できるトーストで通知する。
        return toast_response(
            exc, default="入力内容を確認してください", status_code=422
        )
    # JSON APIクライアント（web/routers/api.py）向けには標準の422応答を維持する
    return await request_validation_exception_handler(request, exc)


def _wants_html_page(request: Request) -> bool:
    """ブラウザのフルページ遷移かどうか。

    - ``/api/*`` はJSON APIなので対象外（`web/routers/api.py` の互換を保つ）
    - htmx リクエストは各Routerがパーシャルを返す設計なので対象外
    - `Accept` に ``text/html`` を含むものをフルページ遷移とみなす
    """
    if request.url.path.startswith("/api/"):
        return False
    if request.headers.get("HX-Request"):
        return False
    return "text/html" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    """`HTTPException` をブラウザ向けにHTMLページとして描画する。

    フルページ表示のエンドポイント（`GET /persona/{id}` 等）が
    `raise HTTPException(404, detail=...)` すると、FastAPI の既定ハンドラが
    `{"detail": "..."}` を返し、ブラウザに**生のJSONが表示される**。
    Issue #117 が定めた NOT_FOUND の表示（バナー + 復帰リンク）が、この経路
    だけ適用されていなかった。

    `detail` はRouter内で組み立てた固定文言（例外メッセージではない）なので
    そのまま表示してよい。文言を持たない場合はステータスに応じた既定文に落とす。
    """
    if not _wants_html_page(request):
        # JSON API / htmx 経路は既定の挙動を維持する
        return await fastapi_http_exception_handler(request, exc)

    logger.warning(
        "HTTPException: %s %s -> %s",
        request.method,
        request.url.path,
        exc.status_code,
    )
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else None
    message = detail or (
        "お探しのページが見つかりません" if exc.status_code == 404 else FALLBACK_MESSAGE
    )
    return templates.TemplateResponse(
        request,
        "partials/error_page.html",
        {
            "request": request,
            "title": "エラー",
            "error": message,
            "status_code": exc.status_code,
            "back_url": _back_url_for(request.url.path),
        },
        status_code=exc.status_code,
    )


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
