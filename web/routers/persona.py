"""
ペルソナ関連のルーター
"""

import logging
import asyncio
import re
from typing import Optional
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from cachetools import TTLCache
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from src.managers.file_manager import FileManager, FileUploadError, FileSecurityError
from src.managers.persona_manager import PersonaManager, PersonaManagerError
from src.models.persona import Persona
from src.services.service_factory import service_factory
from src.services.s3_service import S3Service
from src.config import config

# 一時ペルソナ用TTLキャッシュ（30分で自動削除、最大1000件）
_temp_personas_cache: TTLCache = TTLCache(maxsize=1000, ttl=1800)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

# スレッドプールエグゼキューター（同期的なAI処理を非同期で実行するため）
executor = ThreadPoolExecutor(max_workers=8)

# シングルトンマネージャーインスタンス（モジュールレベルで共有）
_persona_manager = None
_file_manager = None


def get_persona_manager() -> PersonaManager:
    """PersonaManagerのシングルトンインスタンスを取得"""
    global _persona_manager
    if _persona_manager is None:
        _persona_manager = PersonaManager()
    return _persona_manager


def get_file_manager() -> FileManager:
    """FileManagerのシングルトンインスタンスを取得"""
    global _file_manager
    if _file_manager is None:
        # S3サービスの初期化（S3_BUCKET_NAMEが設定されている場合）
        s3_service = None
        if config.S3_BUCKET_NAME:
            s3_service = S3Service(config.S3_BUCKET_NAME, config.AWS_REGION)
        _file_manager = FileManager(s3_service=s3_service)
    return _file_manager


@router.get("/generation", response_class=HTMLResponse)
async def persona_generation_page(request: Request):
    """ペルソナ生成ページ"""
    return templates.TemplateResponse(
        "persona/generation.html", {"request": request, "title": "AIペルソナ生成"}
    )


@router.get("/management", response_class=HTMLResponse)
async def persona_management_page(request: Request):
    """ペルソナ管理ページ"""
    try:
        persona_manager = get_persona_manager()
        personas = persona_manager.get_all_personas()
    except Exception as e:
        logger.error(f"ペルソナ一覧取得エラー: {e}")
        personas = []

    return templates.TemplateResponse(
        "persona/management.html",
        {"request": request, "title": "ペルソナ管理", "personas": personas},
    )


@router.post("/upload", response_class=HTMLResponse)
async def upload_file(request: Request, file: UploadFile = File(...)):
    """ファイルアップロード処理（htmx対応）"""
    try:
        file_content = await file.read()
        file_manager = get_file_manager()

        saved_path, file_text, metadata = file_manager.upload_interview_file(
            file_content, file.filename, allow_duplicates=False
        )

        # アップロード成功時のパーシャルHTMLを返す
        return templates.TemplateResponse(
            "persona/partials/upload_success.html",
            {
                "request": request,
                "file_name": file.filename,
                "file_size": len(file_content),
                "file_text": file_text,
                "file_id": metadata.file_id,
                "char_count": len(file_text),
                "word_count": len(file_text.split()),
                "line_count": len(file_text.splitlines()),
            },
        )
    except FileSecurityError as e:
        logger.warning(f"ファイルセキュリティエラー: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": f"セキュリティエラー: {str(e)}"},
            status_code=400,
        )
    except FileUploadError as e:
        logger.warning(f"ファイルアップロードエラー: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": f"アップロードエラー: {str(e)}"},
            status_code=400,
        )
    except Exception as e:
        logger.error(f"予期しないエラー: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {
                "request": request,
                "error": "ファイルのアップロード中にエラーが発生しました",
            },
            status_code=500,
        )


def _generate_persona_sync(file_text: str) -> Persona:
    """同期的なペルソナ生成処理（スレッドプールで実行）"""
    persona_manager = get_persona_manager()
    return persona_manager.generate_persona_from_interview(file_text)


@router.post("/generate", response_class=HTMLResponse)
async def generate_persona(request: Request, file_text: str = Form(...)):
    """ペルソナ生成処理（htmx対応）"""
    try:
        # 入力データの検証
        if not file_text or not file_text.strip():
            logger.warning("空のファイルテキストでペルソナ生成が試行されました")
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "インタビューテキストが空です"},
                status_code=400,
            )

        logger.info(f"ペルソナ生成開始 - テキスト長: {len(file_text)} 文字")

        # 同期的なAI処理をスレッドプールで非同期実行
        loop = asyncio.get_event_loop()
        generated_persona = await loop.run_in_executor(
            executor, _generate_persona_sync, file_text
        )

        logger.info(f"ペルソナ生成成功: {generated_persona.name}")

        return templates.TemplateResponse(
            "persona/partials/generated_persona.html",
            {"request": request, "persona": generated_persona},
        )
    except PersonaManagerError as e:
        logger.error(f"ペルソナ生成エラー: {e}")
        # より詳細なエラー情報をログに記録
        import traceback

        logger.error(f"ペルソナ生成エラーの詳細: {traceback.format_exc()}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": f"ペルソナ生成エラー: {str(e)}"},
            status_code=500,
        )
    except Exception as e:
        logger.error(f"予期しないエラー: {e}")
        # より詳細なエラー情報をログに記録
        import traceback

        logger.error(f"予期しないエラーの詳細: {traceback.format_exc()}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": "ペルソナの生成中にエラーが発生しました"},
            status_code=500,
        )


@router.post("/save", response_class=HTMLResponse)
async def save_persona(
    request: Request,
    persona_id: str = Form(...),
    name: str = Form(...),
    age: int = Form(...),
    occupation: str = Form(...),
    background: str = Form(...),
    values: str = Form(...),
    pain_points: str = Form(...),
    goals: str = Form(...),
):
    """ペルソナ保存処理（htmx対応）"""
    try:
        persona_manager = get_persona_manager()

        # フォームデータからペルソナを作成（create_newを使用してタイムスタンプを自動設定）
        persona = Persona.create_new(
            name=name,
            age=age,
            occupation=occupation,
            background=background,
            values=[v.strip() for v in values.split("\n") if v.strip()],
            pain_points=[p.strip() for p in pain_points.split("\n") if p.strip()],
            goals=[g.strip() for g in goals.split("\n") if g.strip()],
        )

        saved_id = persona_manager.save_persona(persona)

        return templates.TemplateResponse(
            "partials/success.html",
            {
                "request": request,
                "message": f"ペルソナ「{name}」を保存しました",
                "redirect_url": "/persona/management",
            },
        )
    except Exception as e:
        logger.error(f"ペルソナ保存エラー: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": f"保存エラー: {str(e)}"},
            status_code=500,
        )


@router.get("/{persona_id}/edit", response_class=HTMLResponse)
async def get_persona_edit_form(request: Request, persona_id: str):
    """ペルソナ編集フォームパーシャル（htmx対応）"""
    try:
        persona_manager = get_persona_manager()
        persona = persona_manager.get_persona(persona_id)

        if not persona:
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "ペルソナが見つかりません"},
                status_code=404,
            )

        return templates.TemplateResponse(
            "persona/partials/edit_form.html", {"request": request, "persona": persona}
        )
    except Exception as e:
        logger.error(f"ペルソナ編集フォーム取得エラー: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": "編集フォームの取得に失敗しました"},
            status_code=500,
        )


@router.put("/{persona_id}", response_class=HTMLResponse)
async def update_persona(
    request: Request,
    persona_id: str,
    name: str = Form(...),
    age: int = Form(...),
    occupation: str = Form(...),
    background: str = Form(...),
    values: str = Form(...),
    pain_points: str = Form(...),
    goals: str = Form(...),
):
    """ペルソナ更新処理（htmx対応）- 詳細画面用"""
    try:
        persona_manager = get_persona_manager()

        # 既存ペルソナを取得
        existing_persona = persona_manager.get_persona(persona_id)
        if not existing_persona:
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "ペルソナが見つかりません"},
                status_code=404,
            )

        # ペルソナを更新
        updated_persona = persona_manager.edit_persona(
            persona_id=persona_id,
            name=name,
            age=age,
            occupation=occupation,
            background=background,
            values=[v.strip() for v in values.split("\n") if v.strip()],
            pain_points=[p.strip() for p in pain_points.split("\n") if p.strip()],
            goals=[g.strip() for g in goals.split("\n") if g.strip()],
        )

        if updated_persona:
            # htmxスワップ用：ヘッダー + ボディを返す
            return templates.TemplateResponse(
                "persona/partials/detail_swap.html",
                {
                    "request": request,
                    "persona": updated_persona,
                    "message": "ペルソナを更新しました",
                },
            )
        else:
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "ペルソナの更新に失敗しました"},
                status_code=400,
            )
    except PersonaManagerError as e:
        logger.error(f"ペルソナ更新エラー: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": f"更新エラー: {str(e)}"},
            status_code=400,
        )
    except Exception as e:
        logger.error(f"ペルソナ更新エラー: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": f"更新エラー: {str(e)}"},
            status_code=500,
        )


@router.get("/{persona_id}", response_class=HTMLResponse)
async def get_persona_detail(request: Request, persona_id: str):
    """ペルソナ詳細ページ"""
    try:
        persona_manager = get_persona_manager()
        persona = persona_manager.get_persona(persona_id)

        if not persona:
            raise HTTPException(status_code=404, detail="ペルソナが見つかりません")

        return templates.TemplateResponse(
            "persona/detail.html",
            {
                "request": request,
                "title": f"ペルソナ: {persona.name}",
                "persona": persona,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"ペルソナ取得エラー: {e}")
        raise HTTPException(
            status_code=500, detail="ペルソナの取得中にエラーが発生しました"
        )


@router.delete("/{persona_id}", response_class=HTMLResponse)
async def delete_persona(request: Request, persona_id: str):
    """ペルソナ削除処理（htmx対応）"""
    try:
        persona_manager = get_persona_manager()
        success = persona_manager.delete_persona(persona_id)

        if success:
            return templates.TemplateResponse(
                "partials/success.html",
                {"request": request, "message": "ペルソナを削除しました"},
            )
        else:
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "ペルソナの削除に失敗しました"},
                status_code=400,
            )
    except Exception as e:
        logger.error(f"ペルソナ削除エラー: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": f"削除エラー: {str(e)}"},
            status_code=500,
        )


@router.get("/list/partial", response_class=HTMLResponse)
async def get_persona_list_partial(request: Request, search: Optional[str] = None):
    """ペルソナ一覧パーシャル（htmx対応）"""
    try:
        persona_manager = get_persona_manager()
        personas = persona_manager.get_all_personas()

        # 検索フィルタリング
        if search:
            search_lower = search.lower()
            personas = [
                p
                for p in personas
                if search_lower in p.name.lower()
                or search_lower in p.occupation.lower()
                or search_lower in p.background.lower()
            ]

        return templates.TemplateResponse(
            "persona/partials/persona_list.html",
            {"request": request, "personas": personas},
        )
    except Exception as e:
        logger.error(f"ペルソナ一覧取得エラー: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": "ペルソナ一覧の取得に失敗しました"},
            status_code=500,
        )


# ============================================
# Memory Endpoints
# ============================================

MEMORIES_PER_PAGE = 10


class MemoryUIError(Exception):
    """Memory UI操作エラー"""

    pass


def _get_user_friendly_error_message(error: Exception) -> str:
    """
    エラーをユーザーフレンドリーなメッセージに変換

    Args:
        error: 発生した例外

    Returns:
        ユーザーフレンドリーなエラーメッセージ
    """
    from botocore.exceptions import ClientError
    from src.services.memory.memory_service import (
        MemoryServiceError,
        MemoryConnectionError,
    )

    # ClientErrorの場合
    if isinstance(error, ClientError):
        error_code = error.response.get("Error", {}).get("Code", "Unknown")

        error_messages = {
            "ThrottlingException": "サービスが一時的に混雑しています。しばらく待ってから再試行してください。",
            "ServiceUnavailable": "記憶サービスが一時的に利用できません。後でもう一度お試しください。",
            "ResourceNotFoundException": "記憶リソースが見つかりません。",
            "AccessDeniedException": "記憶サービスへのアクセスが拒否されました。",
            "ValidationException": "入力データが無効です。",
            "InternalServerError": "サーバーエラーが発生しました。後でもう一度お試しください。",
        }

        return error_messages.get(error_code, "記憶操作中にエラーが発生しました。")

    # MemoryConnectionErrorの場合
    if isinstance(error, MemoryConnectionError):
        return "記憶サービスへの接続に失敗しました。設定を確認してください。"

    # MemoryServiceErrorの場合
    if isinstance(error, MemoryServiceError):
        return "記憶サービスでエラーが発生しました。後でもう一度お試しください。"

    # ConnectionErrorの場合
    if isinstance(error, (ConnectionError, TimeoutError)):
        return "ネットワーク接続エラーが発生しました。接続を確認してください。"

    # その他のエラー
    return "予期しないエラーが発生しました。後でもう一度お試しください。"


def _parse_topic_content(content: str) -> dict | None:
    """
    <topic name="...">...</topic> 形式のコンテンツをパース

    Returns:
        パース成功時: {"name": トピック名, "content": 内容}
        パース失敗時: None
    """
    pattern = r'<topic\s+name="([^"]+)">\s*(.*?)\s*</topic>'
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return {"name": match.group(1), "content": match.group(2).strip()}
    return None


@router.get("/{persona_id}/memories", response_class=HTMLResponse)
async def get_persona_memories(
    request: Request, persona_id: str, page: int = 1, strategy_type: str = "summary"
):
    """
    ペルソナの長期記憶一覧を取得（htmx対応）

    Args:
        persona_id: ペルソナID
        page: ページ番号
        strategy_type: 戦略タイプ（"summary" または "semantic"）

    Requirements:
        - 7.1: ペルソナ詳細画面で長期記憶セクションを表示
        - 7.2: タイムスタンプと内容サマリーを表示
        - 7.3: 記憶がない場合は適切なメッセージを表示
        - 7.4: ページネーションをサポート
        - 10.3: エラー時にユーザーにエラーメッセージを表示
    """
    try:
        persona_manager = get_persona_manager()

        # PersonaManagerを通じて記憶を取得
        memories, current_page, total_pages = persona_manager.get_persona_memories(
            persona_id=persona_id,
            strategy_type=strategy_type,
            page=page,
            per_page=MEMORIES_PER_PAGE,
        )

        return templates.TemplateResponse(
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": memories,
                "current_page": current_page,
                "total_pages": total_pages,
                "strategy_type": strategy_type,
            },
        )

    except PersonaManagerError as e:
        logger.warning(
            f"PersonaManager error getting memories for persona {persona_id}: {e}"
        )
        return templates.TemplateResponse(
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": [],
                "error": str(e),
                "strategy_type": strategy_type,
            },
        )

    except (ConnectionError, TimeoutError) as e:
        # ネットワークエラー（Requirements 10.3）
        error_msg = _get_user_friendly_error_message(e)
        logger.error(f"Network error getting memories for persona {persona_id}: {e}")
        return templates.TemplateResponse(
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": [],
                "error": error_msg,
                "strategy_type": strategy_type,
            },
        )

    except Exception as e:
        # その他のエラー（Requirements 10.3）
        error_msg = _get_user_friendly_error_message(e)
        logger.error(
            f"Error getting memories for persona {persona_id}: {e}", exc_info=True
        )
        return templates.TemplateResponse(
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": [],
                "error": error_msg,
                "strategy_type": strategy_type,
            },
        )


@router.delete("/{persona_id}/memories/{memory_id}", response_class=HTMLResponse)
async def delete_persona_memory(request: Request, persona_id: str, memory_id: str):
    """
    ペルソナの特定の記憶を削除（htmx対応）

    Requirements:
        - 8.1: 各記憶エントリに削除ボタンを提供
        - 8.2: 削除確認ダイアログを表示
        - 8.3: 削除確認後、AgentCore Memoryから記憶を削除
        - 10.3: 削除失敗時にエラーメッセージを表示
    """
    try:
        # メモリサービスを取得
        memory_service = service_factory.get_memory_service()

        if not memory_service:
            # 長期記憶機能が無効の場合 - エラーをインラインで表示
            logger.warning(f"Memory service disabled, cannot delete memory {memory_id}")
            return templates.TemplateResponse(
                "persona/partials/memory_delete_error.html",
                {
                    "request": request,
                    "memory_id": memory_id,
                    "error": "長期記憶機能が無効です",
                },
                status_code=400,
            )

        # 記憶を削除
        success = memory_service.delete_memory(actor_id=persona_id, memory_id=memory_id)

        if success:
            # 削除成功時は空のレスポンスを返す（htmxがDOM要素を削除）
            logger.info(f"Memory {memory_id} deleted for persona {persona_id}")
            return HTMLResponse(content="", status_code=200)
        else:
            # 削除失敗時はエラーメッセージを含む要素を返す
            logger.warning(f"Memory {memory_id} not found for persona {persona_id}")
            return templates.TemplateResponse(
                "persona/partials/memory_delete_error.html",
                {
                    "request": request,
                    "memory_id": memory_id,
                    "error": "記憶の削除に失敗しました。記憶が見つからないか、既に削除されている可能性があります。",
                },
                status_code=400,
            )

    except (ConnectionError, TimeoutError) as e:
        # ネットワークエラー（Requirements 10.3）
        error_msg = _get_user_friendly_error_message(e)
        logger.error(f"Network error deleting memory {memory_id}: {e}")
        return templates.TemplateResponse(
            "persona/partials/memory_delete_error.html",
            {"request": request, "memory_id": memory_id, "error": error_msg},
            status_code=503,
        )

    except Exception as e:
        # その他のエラー（Requirements 10.3）
        error_msg = _get_user_friendly_error_message(e)
        logger.error(f"Error deleting memory {memory_id}: {e}", exc_info=True)
        return templates.TemplateResponse(
            "persona/partials/memory_delete_error.html",
            {"request": request, "memory_id": memory_id, "error": error_msg},
            status_code=500,
        )


@router.delete("/{persona_id}/memories", response_class=HTMLResponse)
async def delete_all_persona_memories(
    request: Request, persona_id: str, strategy_type: str = "summary"
):
    """
    ペルソナの全記憶を削除（htmx対応）

    Args:
        persona_id: ペルソナID
        strategy_type: 戦略タイプ（"summary" または "semantic"）

    Requirements:
        - 8.5: 「全ての記憶を削除」オプションを提供（確認付き）
        - 8.2: 削除確認ダイアログを表示
        - 8.3: 削除確認後、AgentCore Memoryから記憶を削除
        - 8.4: 全記憶削除後、空の状態を表示
        - 10.3: 削除失敗時にエラーメッセージを表示
    """
    try:
        # メモリサービスを取得
        memory_service = service_factory.get_memory_service()

        if not memory_service:
            logger.warning(
                f"Memory service disabled, cannot delete all memories for persona {persona_id}"
            )
            return templates.TemplateResponse(
                "persona/partials/memory_list.html",
                {
                    "request": request,
                    "persona_id": persona_id,
                    "memories": [],
                    "error": "長期記憶機能が無効です",
                    "strategy_type": strategy_type,
                },
            )

        # 指定された戦略タイプの記憶のみを取得して削除
        all_memories = memory_service.list_memories(actor_id=persona_id)
        memories_to_delete = [
            m
            for m in all_memories
            if m.metadata and m.metadata.get("strategy_type") == strategy_type
        ]

        deleted_count = 0
        for memory in memories_to_delete:
            try:
                if memory_service.delete_memory(
                    actor_id=persona_id, memory_id=memory.id
                ):
                    deleted_count += 1
            except Exception as e:
                logger.warning(f"Failed to delete memory {memory.id}: {e}")

        logger.info(
            f"Deleted {deleted_count} {strategy_type} memories for persona {persona_id}"
        )

        # 空の記憶リストを返す（成功メッセージ付き）
        item_name = "知識" if strategy_type == "semantic" else "記憶"
        return templates.TemplateResponse(
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": [],
                "message": f"{deleted_count}件の{item_name}を削除しました"
                if deleted_count > 0
                else f"削除する{item_name}がありませんでした",
                "current_page": 1,
                "total_pages": 1,
                "strategy_type": strategy_type,
            },
        )

    except (ConnectionError, TimeoutError) as e:
        # ネットワークエラー（Requirements 10.3）
        error_msg = _get_user_friendly_error_message(e)
        logger.error(
            f"Network error deleting all memories for persona {persona_id}: {e}"
        )

        # エラー時は現在の記憶リストを再取得して表示
        memories = _safe_get_memories(persona_id, strategy_type)

        return templates.TemplateResponse(
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": memories,
                "error": error_msg,
                "current_page": 1,
                "total_pages": 1,
                "strategy_type": strategy_type,
            },
        )

    except Exception as e:
        # その他のエラー（Requirements 10.3）
        error_msg = _get_user_friendly_error_message(e)
        logger.error(
            f"Error deleting all memories for persona {persona_id}: {e}", exc_info=True
        )

        # エラー時は現在の記憶リストを再取得して表示
        memories = _safe_get_memories(persona_id)

        return templates.TemplateResponse(
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": memories,
                "error": error_msg,
                "current_page": 1,
                "total_pages": 1,
            },
        )


def _safe_get_memories(persona_id: str, strategy_type: str = "summary") -> list:
    """
    エラー時に安全に記憶リストを取得するヘルパー関数

    Args:
        persona_id: ペルソナID
        strategy_type: 戦略タイプ（"summary" または "semantic"）

    Returns:
        記憶リスト（取得失敗時は空リスト）
    """
    try:
        memory_service = service_factory.get_memory_service()
        if memory_service:
            all_memories = memory_service.list_memories(actor_id=persona_id)
            # 戦略タイプでフィルタリング
            memories = [
                m
                for m in all_memories
                if m.metadata and m.metadata.get("strategy_type") == strategy_type
            ]
            memories.sort(key=lambda m: m.created_at, reverse=True)
            return memories
    except Exception as e:
        logger.warning(f"Failed to retrieve memories for error recovery: {e}")
    return []


@router.post("/{persona_id}/memories", response_class=HTMLResponse)
async def add_persona_memory(
    request: Request,
    persona_id: str,
    topic_name: str = Form(...),
    topic_content: str = Form(...),
    strategy_type: str = Form(default="semantic"),
):
    """
    ペルソナに手動で知識を追加（htmx対応）

    Args:
        persona_id: ペルソナID
        topic_name: トピック名（例: 好きな食べ物）
        topic_content: トピック内容（例: ラーメンが好き）
        strategy_type: 戦略タイプ（"semantic"）
    """
    try:
        persona_manager = get_persona_manager()

        # PersonaManagerを通じて知識を追加
        persona_manager.add_persona_knowledge(
            persona_id=persona_id, topic_name=topic_name, topic_content=topic_content
        )

        topic_name_clean = topic_name.strip()
        logger.info(
            f"Manual knowledge added for persona {persona_id}: {topic_name_clean}"
        )

        # 更新された記憶リストを取得
        memories, current_page, total_pages = persona_manager.get_persona_memories(
            persona_id=persona_id,
            strategy_type="semantic",
            page=1,
            per_page=MEMORIES_PER_PAGE,
        )

        return templates.TemplateResponse(
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": memories,
                "current_page": current_page,
                "total_pages": total_pages,
                "strategy_type": "semantic",
                "message": f"知識「{topic_name_clean}」を追加しました。反映まで数分かかります。",
            },
        )

    except PersonaManagerError as e:
        logger.warning(
            f"PersonaManager error adding memory for persona {persona_id}: {e}"
        )
        return templates.TemplateResponse(
            "persona/partials/memory_add_error.html",
            {"request": request, "error": str(e)},
            status_code=400,
        )

    except (ConnectionError, TimeoutError) as e:
        error_msg = _get_user_friendly_error_message(e)
        logger.error(f"Network error adding memory for persona {persona_id}: {e}")
        return templates.TemplateResponse(
            "persona/partials/memory_add_error.html",
            {"request": request, "error": error_msg},
            status_code=503,
        )

    except Exception as e:
        error_msg = _get_user_friendly_error_message(e)
        logger.error(
            f"Error adding memory for persona {persona_id}: {e}", exc_info=True
        )
        return templates.TemplateResponse(
            "persona/partials/memory_add_error.html",
            {"request": request, "error": error_msg},
            status_code=500,
        )


@router.post("/{persona_id}/memories/upload-preview", response_class=HTMLResponse)
async def upload_knowledge_file_preview(
    request: Request, persona_id: str, file: UploadFile = File(...)
):
    """
    知識ファイルをアップロードしてプレビュー表示（htmx対応）

    Args:
        persona_id: ペルソナID
        file: アップロードされたファイル
    """
    try:
        # ファイル内容を読み込み
        file_content = await file.read()

        # FileManagerで変換
        file_manager = get_file_manager()
        file_metadata, markdown_content = file_manager.upload_knowledge_file(
            file_content, file.filename
        )

        # 内容の文字数チェック（10000文字制限）
        if len(markdown_content) > 10000:
            logger.warning(
                f"Knowledge file content too long for persona {persona_id}: "
                f"{len(markdown_content)} chars (limit: 10000)"
            )
            return templates.TemplateResponse(
                "persona/partials/knowledge_file_error.html",
                {
                    "request": request,
                    "error": (
                        f"変換後の内容が長すぎます（{len(markdown_content)}文字）。"
                        f"1つの知識の内容は10000文字以内である必要があります。"
                        f"より小さいファイルを使用するか、ファイルを分割してアップロードしてください。"
                    ),
                },
            )

        # ファイル名から拡張子を除去してトピック名を生成
        topic_name = Path(file.filename).stem

        logger.info(
            f"Knowledge file uploaded for preview: {file.filename} (persona: {persona_id})"
        )

        return templates.TemplateResponse(
            "persona/partials/knowledge_file_preview.html",
            {
                "request": request,
                "persona_id": persona_id,
                "topic_name": topic_name,
                "markdown_content": markdown_content,
                "file_name": file.filename,
                "content_length": len(markdown_content),
            },
        )

    except FileUploadError as e:
        logger.warning(f"Knowledge file upload error for persona {persona_id}: {e}")
        return templates.TemplateResponse(
            "persona/partials/knowledge_file_error.html",
            {"request": request, "error": str(e)},
        )

    except FileSecurityError as e:
        logger.warning(f"Knowledge file security error for persona {persona_id}: {e}")
        return templates.TemplateResponse(
            "persona/partials/knowledge_file_error.html",
            {"request": request, "error": f"セキュリティエラー: {str(e)}"},
        )

    except Exception as e:
        logger.error(
            f"Unexpected error uploading knowledge file for persona {persona_id}: {e}",
            exc_info=True,
        )
        return templates.TemplateResponse(
            "persona/partials/knowledge_file_error.html",
            {
                "request": request,
                "error": "ファイルのアップロード中にエラーが発生しました",
            },
        )


# ナレッジベース紐付け関連エンドポイント


@router.get("/{persona_id}/kb-binding", response_class=HTMLResponse)
async def get_kb_binding(request: Request, persona_id: str):
    """ペルソナのナレッジベース紐付け情報を取得"""
    db_service = service_factory.get_database_service()

    knowledge_bases = db_service.get_all_knowledge_bases()
    binding = db_service.get_kb_binding_by_persona(persona_id)

    # 紐付け済みだがKBが削除されている場合、紐付けも削除
    if binding:
        kb = db_service.get_knowledge_base(binding.kb_id)
        if not kb:
            db_service.delete_kb_binding(binding.id)
            binding = None

    return templates.TemplateResponse(
        "persona/partials/kb_binding.html",
        {
            "request": request,
            "persona_id": persona_id,
            "knowledge_bases": knowledge_bases,
            "binding": binding,
        },
    )


@router.post("/{persona_id}/kb-binding", response_class=HTMLResponse)
async def create_kb_binding(
    request: Request,
    persona_id: str,
    kb_id: str = Form(...),
    metadata_filters_json: str = Form(default="{}"),
):
    """ナレッジベース紐付けを作成（既存があれば上書き）"""
    import json
    from src.models.knowledge_base import PersonaKBBinding

    db_service = service_factory.get_database_service()

    try:
        metadata_filters = json.loads(metadata_filters_json) if metadata_filters_json else {}
    except json.JSONDecodeError:
        metadata_filters = {}

    binding = PersonaKBBinding.create_new(
        persona_id=persona_id,
        kb_id=kb_id,
        metadata_filters=metadata_filters,
    )

    db_service.save_kb_binding(binding)
    logger.info(f"Created KB binding: persona={persona_id}, kb={kb_id}")

    return await get_kb_binding(request, persona_id)


@router.delete("/{persona_id}/kb-binding/{binding_id}", response_class=HTMLResponse)
async def delete_kb_binding(request: Request, persona_id: str, binding_id: str):
    """ナレッジベース紐付けを解除"""
    db_service = service_factory.get_database_service()
    db_service.delete_kb_binding(binding_id)
    logger.info(f"Deleted KB binding: {binding_id}")

    return await get_kb_binding(request, persona_id)


# データセット紐付け関連エンドポイント


@router.get("/{persona_id}/dataset-bindings", response_class=HTMLResponse)
async def get_dataset_bindings(request: Request, persona_id: str):
    """ペルソナのデータセット紐付け一覧を取得"""
    db_service = service_factory.get_database_service()

    # 全データセットを取得
    datasets = db_service.get_all_datasets()

    # このペルソナの紐付けを取得
    bindings = db_service.get_bindings_by_persona(persona_id)
    bindings_map = {b.dataset_id: b for b in bindings}

    return templates.TemplateResponse(
        "persona/partials/dataset_bindings.html",
        {
            "request": request,
            "persona_id": persona_id,
            "datasets": datasets,
            "bindings_map": bindings_map,
        },
    )


@router.post("/{persona_id}/dataset-bindings", response_class=HTMLResponse)
async def create_dataset_binding(
    request: Request,
    persona_id: str,
    dataset_id: str = Form(...),
    key_name: str = Form(default=""),
    key_value: str = Form(default=""),
):
    """データセット紐付けを作成"""
    from src.models.dataset import PersonaDatasetBinding

    db_service = service_factory.get_database_service()

    binding_keys = {}
    if key_name and key_value:
        binding_keys[key_name] = key_value

    binding = PersonaDatasetBinding.create_new(
        persona_id=persona_id, dataset_id=dataset_id, binding_keys=binding_keys
    )

    db_service.save_binding(binding)
    logger.info(f"Created dataset binding: persona={persona_id}, dataset={dataset_id}")

    # 更新された一覧を返す
    return await get_dataset_bindings(request, persona_id)


@router.delete(
    "/{persona_id}/dataset-bindings/{binding_id}", response_class=HTMLResponse
)
async def delete_dataset_binding(request: Request, persona_id: str, binding_id: str):
    """データセット紐付けを削除"""
    db_service = service_factory.get_database_service()
    db_service.delete_binding(binding_id)
    logger.info(f"Deleted dataset binding: {binding_id}")

    return await get_dataset_bindings(request, persona_id)


def _generate_multiple_personas_sync(
    file_content: bytes, filename: str, persona_count: int
) -> list:
    """同期的な複数ペルソナ生成処理（スレッドプールで実行）"""
    persona_manager = get_persona_manager()
    return persona_manager.generate_personas_from_market_report(
        file_content, filename, persona_count
    )


@router.post("/generate-multiple", response_class=HTMLResponse)
async def generate_multiple_personas(
    request: Request, file: UploadFile = File(...), persona_count: int = Form(...)
):
    """市場調査レポートから複数ペルソナを生成（htmx対応）"""
    try:
        # 入力検証
        if persona_count < 1 or persona_count > 10:
            logger.warning(f"無効なペルソナ数: {persona_count}")
            return templates.TemplateResponse(
                "partials/error.html",
                {
                    "request": request,
                    "error": "ペルソナ数は1-10の範囲で指定してください",
                },
                status_code=400,
            )

        # ファイル読み込み
        file_content = await file.read()
        if not file_content:
            logger.warning("空のファイルがアップロードされました")
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "ファイルが空です"},
                status_code=400,
            )

        logger.info(
            f"複数ペルソナ生成開始 - ファイル: {file.filename}, "
            f"サイズ: {len(file_content)} bytes, ペルソナ数: {persona_count}"
        )

        # 同期的なAI処理をスレッドプールで非同期実行
        loop = asyncio.get_event_loop()
        generated_personas = await loop.run_in_executor(
            executor,
            _generate_multiple_personas_sync,
            file_content,
            file.filename,
            persona_count,
        )

        logger.info(f"{len(generated_personas)}個のペルソナ生成成功")

        # 生成されたペルソナをTTLキャッシュに保存（30分で自動削除）
        for persona in generated_personas:
            _temp_personas_cache[persona.id] = persona

        return templates.TemplateResponse(
            "persona/partials/persona_candidates.html",
            {"request": request, "personas": generated_personas},
        )

    except PersonaManagerError as e:
        logger.error(f"複数ペルソナ生成エラー: {e}")
        import traceback

        logger.error(f"エラー詳細: {traceback.format_exc()}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": f"ペルソナ生成エラー: {str(e)}"},
            status_code=500,
        )
    except Exception as e:
        logger.error(f"予期しないエラー: {e}")
        import traceback

        logger.error(f"エラー詳細: {traceback.format_exc()}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": "ペルソナの生成中にエラーが発生しました"},
            status_code=500,
        )


@router.post("/save-selected", response_class=HTMLResponse)
async def save_selected_personas(request: Request, persona_ids: str = Form(...)):
    """選択された複数ペルソナを保存（htmx対応）"""
    try:
        # カンマ区切りのIDリストをパース
        id_list = [pid.strip() for pid in persona_ids.split(",") if pid.strip()]

        if not id_list:
            logger.warning("保存するペルソナが選択されていません")
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "保存するペルソナを選択してください"},
                status_code=400,
            )

        logger.info(f"{len(id_list)}個のペルソナ保存開始")

        persona_manager = get_persona_manager()
        saved_count = 0

        # 各ペルソナを保存
        for persona_id in id_list:
            try:
                # TTLキャッシュからペルソナを取得
                persona = _temp_personas_cache.get(persona_id)
                if persona:
                    persona_manager.save_persona(persona)
                    saved_count += 1
                    logger.info(f"ペルソナ保存成功: {persona.name} (ID: {persona_id})")
                else:
                    logger.warning(f"ペルソナが見つかりません (ID: {persona_id})")
            except Exception as e:
                logger.error(f"ペルソナ保存エラー (ID: {persona_id}): {e}")
                # 個別のエラーは続行
                continue

        # 保存後、キャッシュから削除
        for persona_id in id_list:
            _temp_personas_cache.pop(persona_id, None)

        if saved_count == 0:
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "ペルソナの保存に失敗しました"},
                status_code=500,
            )

        logger.info(f"{saved_count}個のペルソナ保存完了")

        # 成功メッセージを返す
        return templates.TemplateResponse(
            "persona/partials/save_success.html",
            {"request": request, "saved_count": saved_count},
        )

    except Exception as e:
        logger.error(f"予期しないエラー: {e}")
        import traceback

        logger.error(f"エラー詳細: {traceback.format_exc()}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": "ペルソナの保存中にエラーが発生しました"},
            status_code=500,
        )
