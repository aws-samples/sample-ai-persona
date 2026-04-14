"""
AIペルソナシステム - メインアプリケーション
FastAPI + Jinja2 + htmxベースのWebアプリケーション
"""

from typing import Any
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from web.routers import persona, discussion, interview, api, settings, survey
from web.middleware import CSRFMiddleware

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    # level=logging.WARNING,
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
    version="0.4.1",
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

# ルーターの登録
app.include_router(persona.router, prefix="/persona", tags=["persona"])
app.include_router(discussion.router, prefix="/discussion", tags=["discussion"])
app.include_router(interview.router, prefix="/interview", tags=["interview"])
app.include_router(api.router, prefix="/api", tags=["api"])
app.include_router(settings.router, prefix="/settings", tags=["settings"])
app.include_router(survey.router, prefix="/survey", tags=["survey"])


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Any:
    """トップページ"""
    return templates.TemplateResponse(
        "index.html", {"request": request, "title": "AIペルソナシステム"}
    )


@app.get("/health")
async def health_check() -> Any:
    """ヘルスチェック"""
    return {"status": "healthy", "version": "0.4.1"}

