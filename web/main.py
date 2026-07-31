"""
AIペルソナシステム - メインアプリケーション
FastAPI + Jinja2 + htmxベースのWebアプリケーション
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src import __version__
from web.error_messages import toast_response
from web.middleware import CSRFMiddleware
from web.routers import persona, discussion, interview, api, settings, survey

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
    yield
    logger.info("AIペルソナシステムをシャットダウン中...")


# FastAPIアプリケーション
app = FastAPI(
    title="AIペルソナシステム",
    description="AIペルソナを生成し、議論を通じてインサイトを生成",
    version=__version__,
    lifespan=lifespan,
)

# CSRF保護ミドルウェア
app.add_middleware(CSRFMiddleware)

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
templates.env.globals["app_version"] = __version__
for _mod in [persona, discussion, interview, settings, survey]:
    _mod.templates.env.globals["app_version"] = __version__

# ルーターの登録
app.include_router(persona.router, prefix="/persona", tags=["persona"])
app.include_router(discussion.router, prefix="/discussion", tags=["discussion"])
app.include_router(interview.router, prefix="/interview", tags=["interview"])
app.include_router(api.router, prefix="/api", tags=["api"])
app.include_router(settings.router, prefix="/settings", tags=["settings"])
app.include_router(survey.router, prefix="/survey", tags=["survey"])


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
