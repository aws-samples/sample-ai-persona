"""
システム設定ルーター - データセット管理、MCP設定、ナレッジベース管理
"""

import logging
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from src.managers.dataset_manager import DatasetManager
from src.services.mcp_server_manager import get_mcp_manager
from src.services.service_factory import service_factory
from src.models.dataset import DatasetColumn
from src.models.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

# シングルトンマネージャー
_dataset_manager: Optional[DatasetManager] = None


def get_dataset_manager() -> DatasetManager:
    global _dataset_manager
    if _dataset_manager is None:
        _dataset_manager = DatasetManager()
    return _dataset_manager


@router.get("/", response_class=HTMLResponse)
async def settings_page(request: Request):
    """システム設定ページ"""
    dataset_manager = get_dataset_manager()
    mcp_manager = get_mcp_manager()
    db_service = service_factory.get_database_service()

    datasets = dataset_manager.get_datasets()
    datasets_dict = [d.to_dict() for d in datasets]
    mcp_enabled = mcp_manager.is_running()
    knowledge_bases = db_service.get_all_knowledge_bases()
    kb_list = [kb.to_dict() for kb in knowledge_bases]

    return templates.TemplateResponse(
        "settings/index.html",
        {
            "request": request,
            "title": "システム設定",
            "datasets": datasets_dict,
            "mcp_enabled": mcp_enabled,
            "knowledge_bases": kb_list,
        },
    )


@router.post("/mcp/toggle", response_class=HTMLResponse)
async def toggle_mcp(request: Request, enabled: bool = Form(...)):
    """MCP有効/無効切り替え"""
    mcp_manager = get_mcp_manager()
    success = mcp_manager.toggle(enabled)

    if not success:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "MCPサーバーの切り替えに失敗しました"},
            status_code=500,
        )

    return templates.TemplateResponse(
        "settings/partials/mcp_status.html",
        {"request": request, "mcp_enabled": mcp_manager.is_running()},
    )


@router.get("/mcp/status", response_class=HTMLResponse)
async def mcp_status(request: Request):
    """MCP状態取得"""
    mcp_manager = get_mcp_manager()
    return templates.TemplateResponse(
        "settings/partials/mcp_status.html",
        {"request": request, "mcp_enabled": mcp_manager.is_running()},
    )


@router.get("/datasets", response_class=HTMLResponse)
async def list_datasets(request: Request):
    """データセット一覧"""
    dataset_manager = get_dataset_manager()
    datasets = dataset_manager.get_datasets()
    datasets_dict = [d.to_dict() for d in datasets]

    return templates.TemplateResponse(
        "settings/partials/dataset_list.html",
        {"request": request, "datasets": datasets_dict},
    )


@router.get("/datasets/form", response_class=HTMLResponse)
async def dataset_form(request: Request, dataset_id: Optional[str] = None):
    """データセットフォーム（新規/編集）"""
    dataset = None
    if dataset_id:
        dataset_manager = get_dataset_manager()
        dataset = dataset_manager.get_dataset(dataset_id)

    return templates.TemplateResponse(
        "settings/partials/dataset_form.html", {"request": request, "dataset": dataset}
    )


@router.post("/datasets/analyze", response_class=HTMLResponse)
async def analyze_csv(request: Request, file: UploadFile = File(...)):
    """CSVファイルのスキーマを解析"""
    if not file.filename.endswith(".csv"):
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "CSVファイルのみアップロード可能です"},
            status_code=400,
        )

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:  # 10MB制限
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "ファイルサイズは10MB以下にしてください"},
            status_code=400,
        )

    dataset_manager = get_dataset_manager()
    columns, row_count = dataset_manager.analyze_schema(content)

    # DatasetColumnをdictに変換
    columns_dict = [
        {"name": c.name, "data_type": c.data_type, "description": c.description}
        for c in columns
    ]

    return templates.TemplateResponse(
        "settings/partials/schema_preview.html",
        {
            "request": request,
            "columns": columns_dict,
            "row_count": row_count,
            "filename": file.filename,
        },
    )


@router.post("/datasets", response_class=HTMLResponse)
async def create_dataset(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(""),
    notes: str = Form(""),
    columns_json: str = Form(...),
):
    """データセット作成"""
    try:
        content = await file.read()

        # カラム情報をパース
        columns_data = json.loads(columns_json)
        columns = [
            DatasetColumn(
                name=c["name"],
                data_type=c["data_type"],
                description=c.get("description", ""),
            )
            for c in columns_data
        ]

        dataset_manager = get_dataset_manager()
        dataset_manager.upload_csv(
            file_content=content,
            filename=file.filename,
            name=name,
            description=description,
            notes=notes,
            columns=columns,
        )

        # 一覧を返す
        datasets = dataset_manager.get_datasets()
        datasets_dict = [d.to_dict() for d in datasets]
        return templates.TemplateResponse(
            "settings/partials/dataset_list.html",
            {"request": request, "datasets": datasets_dict},
        )

    except Exception as e:
        logger.error(f"Dataset creation failed: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": f"データセット作成に失敗しました: {e}"},
            status_code=500,
        )


@router.put("/datasets/{dataset_id}", response_class=HTMLResponse)
async def update_dataset(
    request: Request,
    dataset_id: str,
    name: str = Form(...),
    description: str = Form(""),
    notes: str = Form(""),
    columns_json: str = Form(...),
):
    """データセット更新"""
    try:
        columns_data = json.loads(columns_json)
        columns = [
            DatasetColumn(
                name=c["name"],
                data_type=c["data_type"],
                description=c.get("description", ""),
            )
            for c in columns_data
        ]

        dataset_manager = get_dataset_manager()
        dataset = dataset_manager.update_dataset(
            dataset_id=dataset_id,
            name=name,
            description=description,
            notes=notes,
            columns=columns,
        )

        if not dataset:
            raise HTTPException(status_code=404, detail="Dataset not found")

        datasets = dataset_manager.get_datasets()
        datasets_dict = [d.to_dict() for d in datasets]
        return templates.TemplateResponse(
            "settings/partials/dataset_list.html",
            {"request": request, "datasets": datasets_dict},
        )

    except Exception as e:
        logger.error(f"Dataset update failed: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": f"データセット更新に失敗しました: {e}"},
            status_code=500,
        )


@router.delete("/datasets/{dataset_id}", response_class=HTMLResponse)
async def delete_dataset(request: Request, dataset_id: str):
    """データセット削除"""
    dataset_manager = get_dataset_manager()
    success = dataset_manager.delete_dataset(dataset_id)

    if not success:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "データセットの削除に失敗しました"},
            status_code=404,
        )

    datasets = dataset_manager.get_datasets()
    datasets_dict = [d.to_dict() for d in datasets]
    return templates.TemplateResponse(
        "settings/partials/dataset_list.html",
        {"request": request, "datasets": datasets_dict},
    )


# JSON API（他のページから使用）
@router.get("/api/datasets")
async def api_list_datasets():
    """データセット一覧API"""
    dataset_manager = get_dataset_manager()
    datasets = dataset_manager.get_datasets()
    return [d.to_dict() for d in datasets]


@router.get("/api/mcp/status")
async def api_mcp_status():
    """MCP状態API"""
    mcp_manager = get_mcp_manager()
    return {"enabled": mcp_manager.is_running()}


# ==================== ナレッジベース管理 ====================


@router.get("/knowledge-bases", response_class=HTMLResponse)
async def list_knowledge_bases(request: Request):
    """ナレッジベース一覧"""
    db_service = service_factory.get_database_service()
    knowledge_bases = db_service.get_all_knowledge_bases()
    kb_list = [kb.to_dict() for kb in knowledge_bases]

    return templates.TemplateResponse(
        "settings/partials/kb_list.html",
        {"request": request, "knowledge_bases": kb_list},
    )


@router.post("/knowledge-bases", response_class=HTMLResponse)
async def create_knowledge_base(
    request: Request,
    knowledge_base_id: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
):
    """ナレッジベース登録"""
    try:
        db_service = service_factory.get_database_service()
        kb = KnowledgeBase.create_new(
            knowledge_base_id=knowledge_base_id.strip(),
            name=name.strip(),
            description=description.strip(),
        )
        db_service.save_knowledge_base(kb)
        logger.info(f"Knowledge base registered: {kb.id} ({knowledge_base_id})")

        knowledge_bases = db_service.get_all_knowledge_bases()
        kb_list = [kb.to_dict() for kb in knowledge_bases]
        return templates.TemplateResponse(
            "settings/partials/kb_list.html",
            {"request": request, "knowledge_bases": kb_list},
        )
    except Exception as e:
        logger.error(f"KB registration failed: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": f"ナレッジベースの登録に失敗しました: {e}"},
            status_code=500,
        )


@router.delete("/knowledge-bases/{kb_id}", response_class=HTMLResponse)
async def delete_knowledge_base(request: Request, kb_id: str):
    """ナレッジベース削除"""
    db_service = service_factory.get_database_service()
    db_service.delete_knowledge_base(kb_id)
    logger.info(f"Knowledge base deleted: {kb_id}")

    knowledge_bases = db_service.get_all_knowledge_bases()
    kb_list = [kb.to_dict() for kb in knowledge_bases]
    return templates.TemplateResponse(
        "settings/partials/kb_list.html",
        {"request": request, "knowledge_bases": kb_list},
    )


# JSON API（他のページから使用）
@router.get("/api/knowledge-bases")
async def api_list_knowledge_bases():
    """ナレッジベース一覧API"""
    db_service = service_factory.get_database_service()
    knowledge_bases = db_service.get_all_knowledge_bases()
    return [kb.to_dict() for kb in knowledge_bases]
