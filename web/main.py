"""
AIペルソナシステム - メインアプリケーション
FastAPI + Jinja2 + htmxベースのWebアプリケーション
"""

import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, FileResponse

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
async def lifespan(app: FastAPI):
    """アプリケーションのライフサイクル管理"""
    logger.info("AIペルソナシステムを起動中...")
    yield
    logger.info("AIペルソナシステムをシャットダウン中...")


# FastAPIアプリケーション
app = FastAPI(
    title="AIペルソナシステム",
    description="AIペルソナを生成し、議論を通じてインサイトを生成",
    version="0.1.0",
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
async def index(request: Request):
    """トップページ"""
    return templates.TemplateResponse(
        "index.html", {"request": request, "title": "AIペルソナシステム"}
    )


@app.get("/health")
async def health_check():
    """ヘルスチェック"""
    return {"status": "healthy", "version": "0.1.0"}


@app.get("/files/documents/{filename:path}")
async def serve_discussion_document(filename: str):
    """
    議論用ドキュメントファイルを配信（ローカルファイルのみ）

    Note: S3ファイルは署名付きURLで直接配信されるため、このエンドポイントは
    ローカルファイルのみを対象とします。

    Args:
        filename: ファイル名（パス区切りを含む場合あり）

    Returns:
        FileResponse: ファイルレスポンス
    """
    # セキュリティ: パストラバーサル攻撃を防ぐ
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    # MIMEタイプを推測
    suffix = Path(filename).suffix.lower()
    mime_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".html": "text/html",
        ".md": "text/markdown",
    }
    mime_type = mime_types.get(suffix, "application/octet-stream")

    # ローカルファイルを確認
    file_path = PROJECT_ROOT / "discussion_documents" / filename

    if not file_path.exists():
        logger.warning(f"Document file not found: {filename}")
        raise HTTPException(status_code=404, detail="File not found")

    # ファイルがdiscussion_documentsディレクトリ内にあることを確認
    try:
        file_path.resolve().relative_to(
            (PROJECT_ROOT / "discussion_documents").resolve()
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file path")

    return FileResponse(path=file_path, media_type=mime_type, filename=file_path.name)


@app.get("/survey-images/{filename}")
async def serve_survey_image(filename: str):
    """アンケート用画像を配信"""
    file_path = PROJECT_ROOT / "survey_images" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        file_path.resolve().relative_to((PROJECT_ROOT / "survey_images").resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file path")
    suffix = file_path.suffix.lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(
        suffix.lstrip("."), "application/octet-stream"
    )
    return FileResponse(path=file_path, media_type=mime)
