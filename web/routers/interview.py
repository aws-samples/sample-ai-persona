"""
インタビュー関連のルーター
"""

import logging
import asyncio
import json
import re
from typing import List, Optional, Dict, Any
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from fastapi import APIRouter, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.managers.persona_manager import PersonaManager
from src.managers.interview_manager import (
    InterviewManager,
    InterviewManagerError,
    InterviewSessionError,
    InterviewSessionNotFoundError,
    InterviewValidationError,
    InterviewAgentError,
    InterviewPersistenceError,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


from web.sanitize import render_markdown  # noqa: E402

templates.env.filters["markdown"] = render_markdown

# スレッドプールエグゼキューター（同期的なAI処理を非同期で実行するため）
executor = ThreadPoolExecutor(max_workers=8)

# シングルトンマネージャーインスタンス（モジュールレベルで共有）
_persona_manager = None
_interview_manager = None

# サポートするMIMEタイプ
SUPPORTED_IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"]
SUPPORTED_DOCUMENT_TYPES = [
    "application/pdf",
    "text/plain",
    "text/csv",
    "text/html",
    "text/markdown",
]
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def get_persona_manager() -> PersonaManager:
    """PersonaManagerのシングルトンインスタンスを取得"""
    global _persona_manager
    if _persona_manager is None:
        _persona_manager = PersonaManager()
    return _persona_manager


def get_interview_manager() -> InterviewManager:
    """InterviewManagerのシングルトンインスタンスを取得"""
    global _interview_manager
    if _interview_manager is None:
        _interview_manager = InterviewManager()
    return _interview_manager


def _prepare_file_content_block(
    file_bytes: bytes, mime_type: str, filename: str
) -> Optional[Dict[str, Any]]:
    """
    ファイルをStrands Agent SDK用のContentBlock形式に変換

    Args:
        file_bytes: ファイルのバイナリデータ
        mime_type: MIMEタイプ
        filename: ファイル名

    Returns:
        ContentBlock形式の辞書、またはサポートされていない場合はNone
    """
    if mime_type in SUPPORTED_IMAGE_TYPES:
        # 画像の場合
        image_format = mime_type.split("/")[-1]
        if image_format not in ["png", "jpeg", "gif", "webp"]:
            image_format = "png"  # フォールバック
        return {"image": {"format": image_format, "source": {"bytes": file_bytes}}}

    elif mime_type == "application/pdf":
        # PDFの場合
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", filename.rsplit(".", 1)[0])[:100]
        return {
            "document": {
                "name": safe_name,
                "format": "pdf",
                "source": {"bytes": file_bytes},
            }
        }

    elif mime_type in ["text/plain", "text/csv", "text/html", "text/markdown"]:
        # テキスト系ドキュメントの場合
        format_map = {
            "text/plain": "txt",
            "text/csv": "csv",
            "text/html": "html",
            "text/markdown": "md",
        }
        doc_format = format_map.get(mime_type, "txt")
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", filename.rsplit(".", 1)[0])[:100]
        return {
            "document": {
                "name": safe_name,
                "format": doc_format,
                "source": {"bytes": file_bytes},
            }
        }

    return None


@router.post("/create", response_class=JSONResponse)
async def create_interview_session(
    request: Request,
    persona_ids: List[str] = Form(...),
    enable_memory: bool = Form(False),
    memory_mode: str = Form("full"),
    enable_dataset: bool = Form(False),
    enable_kb: bool = Form(False),
) -> Any:
    """インタビューセッション作成エンドポイント（拡張エラーハンドリング）"""
    try:
        # Enhanced input validation
        if not persona_ids:
            return JSONResponse(
                {
                    "error": "インタビューには最低1つのペルソナが必要です",
                    "error_type": "validation_error",
                    "field": "persona_ids",
                },
                status_code=400,
            )

        if len(persona_ids) > 5:
            return JSONResponse(
                {
                    "error": "インタビューには最大5つのペルソナまで参加できます",
                    "error_type": "validation_error",
                    "field": "persona_ids",
                    "max_allowed": 5,
                    "provided": len(persona_ids),
                },
                status_code=400,
            )

        # memory_modeの検証
        valid_memory_modes = ["full", "retrieve_only", "disabled"]
        if memory_mode not in valid_memory_modes:
            return JSONResponse(
                {
                    "error": f"無効なmemory_modeです: {memory_mode}。有効な値: {', '.join(valid_memory_modes)}",
                    "error_type": "validation_error",
                    "field": "memory_mode",
                    "valid_values": valid_memory_modes,
                },
                status_code=400,
            )

        # Get personas with enhanced error handling
        persona_manager = get_persona_manager()
        personas = []
        missing_personas = []

        for persona_id in persona_ids:
            try:
                persona = persona_manager.get_persona(persona_id)
                if persona:
                    personas.append(persona)
                else:
                    missing_personas.append(persona_id)
            except Exception as e:
                logger.error(f"Error retrieving persona {persona_id}: {e}")
                missing_personas.append(persona_id)

        if missing_personas:
            return JSONResponse(
                {
                    "error": f"以下のペルソナが見つかりません: {', '.join(missing_personas)}",
                    "error_type": "persona_not_found",
                    "missing_personas": missing_personas,
                },
                status_code=404,
            )

        if not personas:
            return JSONResponse(
                {
                    "error": "有効なペルソナが見つかりません",
                    "error_type": "no_valid_personas",
                },
                status_code=400,
            )

        # Create session with enhanced error handling
        loop = asyncio.get_event_loop()
        interview_manager = get_interview_manager()

        def create_session_sync() -> Any:
            return interview_manager.start_interview_session(
                personas,
                enable_memory=enable_memory,
                memory_mode=memory_mode,
                enable_dataset=enable_dataset,
                enable_kb=enable_kb,
            )

        session = await loop.run_in_executor(executor, create_session_sync)

        logger.info(
            f"Interview session created successfully: {session.id} (enable_memory={enable_memory}, memory_mode={memory_mode}, enable_dataset={enable_dataset}, enable_kb={enable_kb})"
        )

        return JSONResponse(
            {
                "session_id": session.id,
                "participants": [{"id": p.id, "name": p.name} for p in personas],
                "message": "インタビューセッションが正常に作成されました",
                "created_at": session.created_at.isoformat(),
                "enable_memory": enable_memory,
                "memory_mode": memory_mode,
                "enable_dataset": enable_dataset,
                "enable_kb": enable_kb,
            }
        )

    except InterviewValidationError as e:
        logger.error(f"Interview validation error: {e}")
        return JSONResponse(
            {"error": str(e), "error_type": "validation_error"}, status_code=400
        )
    except InterviewAgentError as e:
        logger.error(f"Interview agent error: {e}")
        return JSONResponse(
            {
                "error": "AIエージェントの初期化に失敗しました。しばらく待ってから再試行してください。",
                "error_type": "agent_error",
                "technical_details": str(e),
            },
            status_code=503,
        )
    except InterviewSessionError as e:
        logger.error(f"Interview session error: {e}")
        return JSONResponse(
            {
                "error": "セッションの作成に失敗しました。再試行してください。",
                "error_type": "session_error",
                "technical_details": str(e),
            },
            status_code=500,
        )
    except Exception as e:
        logger.error(f"Unexpected error creating interview session: {e}")
        return JSONResponse(
            {
                "error": "予期しないエラーが発生しました。システム管理者にお問い合わせください。",
                "error_type": "internal_error",
            },
            status_code=500,
        )


@router.get("/chat/{session_id}", response_class=HTMLResponse)
async def interview_chat_page(request: Request, session_id: str) -> Any:
    """インタビューチャットページ（拡張エラーハンドリング）"""
    try:
        interview_manager = get_interview_manager()
        session = interview_manager.get_interview_session(session_id)

        # 参加ペルソナの詳細情報を取得
        persona_manager = get_persona_manager()
        participants = []
        missing_personas = []

        for persona_id in session.participants:
            try:
                persona = persona_manager.get_persona(persona_id)
                if persona:
                    participants.append(persona)
                else:
                    missing_personas.append(persona_id)
            except Exception as e:
                logger.error(f"Error retrieving participant persona {persona_id}: {e}")
                missing_personas.append(persona_id)

        # Log warnings for missing personas but continue
        if missing_personas:
            logger.warning(f"Some participant personas not found: {missing_personas}")

        return templates.TemplateResponse(
            "interview/chat.html",
            {
                "request": request,
                "title": "インタビューチャット",
                "session": session,
                "participants": participants,
                "missing_personas": missing_personas,
            },
        )

    except InterviewSessionNotFoundError as e:
        logger.error(f"Interview session not found: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {
                "request": request,
                "error": "指定されたインタビューセッションが見つかりません。セッションが終了しているか、無効なURLの可能性があります。",
            },
            status_code=404,
        )
    except Exception as e:
        logger.error(f"Error loading interview chat page: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {
                "request": request,
                "error": "ページの読み込み中にエラーが発生しました。しばらく待ってから再試行してください。",
            },
            status_code=500,
        )


@router.post("/{session_id}/message", response_class=JSONResponse)
async def send_message(
    request: Request,
    session_id: str,
    message: str = Form(...),
    files: Optional[List[UploadFile]] = File(None),
) -> Any:
    """メッセージ送信エンドポイント（マルチモーダル対応）"""
    logger.info(
        f"Received message request for session {session_id} (files: {len(files) if files else 0})"
    )

    try:
        # Enhanced input validation
        if not message:
            return JSONResponse(
                {
                    "error": "メッセージが指定されていません",
                    "error_type": "validation_error",
                    "field": "message",
                },
                status_code=400,
            )

        if not message.strip():
            return JSONResponse(
                {
                    "error": "メッセージが空です",
                    "error_type": "validation_error",
                    "field": "message",
                },
                status_code=400,
            )

        if len(message.strip()) > 2000:
            return JSONResponse(
                {
                    "error": "メッセージが長すぎます（最大2000文字）",
                    "error_type": "validation_error",
                    "field": "message",
                    "max_length": 2000,
                    "provided_length": len(message.strip()),
                },
                status_code=400,
            )

        # ファイルをContentBlock形式に変換
        document_contents = []
        if files:
            for file in files:
                if file.filename and file.size > 0:  # type: ignore[operator]
                    # ファイルサイズチェック
                    if file.size > MAX_FILE_SIZE:  # type: ignore[operator]
                        return JSONResponse(
                            {
                                "error": f"ファイル '{file.filename}' が大きすぎます（最大10MB）",
                                "error_type": "validation_error",
                                "field": "files",
                            },
                            status_code=400,
                        )

                    # MIMEタイプチェック
                    content_type = file.content_type or ""
                    if (
                        content_type
                        not in SUPPORTED_IMAGE_TYPES + SUPPORTED_DOCUMENT_TYPES
                    ):
                        return JSONResponse(
                            {
                                "error": f"ファイル '{file.filename}' のタイプ '{content_type}' はサポートされていません",
                                "error_type": "validation_error",
                                "field": "files",
                                "supported_types": SUPPORTED_IMAGE_TYPES
                                + SUPPORTED_DOCUMENT_TYPES,
                            },
                            status_code=400,
                        )

                    # ファイル内容を読み込み
                    file_bytes = await file.read()

                    # ContentBlock形式に変換
                    content_block = _prepare_file_content_block(
                        file_bytes, content_type, file.filename
                    )
                    if content_block:
                        document_contents.append(content_block)
                        logger.info(
                            f"Prepared file for multimodal: {file.filename} ({content_type})"
                        )

        # ドキュメントメタデータを準備（詳細画面での表示用）
        document_metadata = []
        if files:
            for file in files:
                if file.filename and file.size > 0:  # type: ignore[operator]
                    content_type = file.content_type or ""
                    if content_type in SUPPORTED_IMAGE_TYPES + SUPPORTED_DOCUMENT_TYPES:
                        document_metadata.append(
                            {
                                "filename": file.filename,
                                "mime_type": content_type,
                                "file_size": file.size,
                                "uploaded_at": datetime.now().isoformat(),
                            }
                        )

        interview_manager = get_interview_manager()

        # 同期的なメッセージ処理を非同期で実行
        loop = asyncio.get_event_loop()

        def send_message_sync() -> Any:
            return interview_manager.send_user_message(
                session_id,
                message.strip(),
                document_contents=document_contents if document_contents else None,
                document_metadata=document_metadata if document_metadata else None,
            )

        responses = await loop.run_in_executor(executor, send_message_sync)

        # レスポンスをJSON形式で返す
        response_data = []
        for response in responses:
            response_data.append(
                {
                    "persona_id": response.persona_id,
                    "persona_name": response.persona_name,
                    "content": response.content,
                    "content_html": render_markdown(response.content),
                    "timestamp": response.timestamp.isoformat()
                    if response.timestamp
                    else None,
                    "message_type": response.message_type,
                }
            )

        final_response = {
            "user_message": message.strip(),
            "responses": response_data,
            "message": "メッセージが正常に送信されました",
            "response_count": len(response_data),
            "timestamp": datetime.now().isoformat(),
        }

        logger.info(
            f"Message processed successfully: {len(response_data)} responses generated"
        )
        return JSONResponse(final_response)

    except InterviewSessionNotFoundError as e:
        logger.error(f"Session not found: {e}")
        return JSONResponse(
            {
                "error": "インタビューセッションが見つかりません",
                "error_type": "session_not_found",
                "session_id": session_id,
            },
            status_code=404,
        )
    except InterviewValidationError as e:
        logger.error(f"Validation error: {e}")
        return JSONResponse(
            {"error": str(e), "error_type": "validation_error"}, status_code=400
        )
    except InterviewAgentError as e:
        logger.error(f"Agent error: {e}")
        return JSONResponse(
            {
                "error": "AIエージェントとの通信に失敗しました。しばらく待ってから再試行してください。",
                "error_type": "agent_error",
                "technical_details": str(e),
            },
            status_code=503,
        )
    except Exception as e:
        logger.error(f"Unexpected error in message processing: {e}")
        import traceback

        logger.error(f"Traceback: {traceback.format_exc()}")
        return JSONResponse(
            {
                "error": "メッセージの送信中に予期しないエラーが発生しました",
                "error_type": "internal_error",
            },
            status_code=500,
        )


@router.get("/{session_id}/messages", response_class=JSONResponse)
async def get_messages(request: Request, session_id: str) -> Any:
    """メッセージ履歴取得エンドポイント"""
    try:
        interview_manager = get_interview_manager()
        session = interview_manager.get_interview_session(session_id)

        # メッセージをJSON形式で返す（タイムスタンプ順でソート）
        messages_data = []
        for message in sorted(session.messages, key=lambda m: m.timestamp):
            messages_data.append(
                {
                    "persona_id": message.persona_id,
                    "persona_name": message.persona_name,
                    "content": message.content,
                    "message_type": message.message_type,
                    "timestamp": message.timestamp.isoformat()
                    if message.timestamp
                    else None,
                }
            )

        return JSONResponse(
            {
                "session_id": session_id,
                "messages": messages_data,
                "message_count": len(messages_data),
            }
        )

    except InterviewManagerError as e:
        logger.error(f"Error getting messages: {e}")
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.error(f"Unexpected error getting messages: {e}")
        return JSONResponse(
            {"error": "メッセージ履歴の取得中にエラーが発生しました"}, status_code=500
        )


@router.get("/{session_id}/status", response_class=JSONResponse)
async def get_session_status(request: Request, session_id: str) -> Any:
    """インタビューセッション状態取得エンドポイント"""
    try:
        interview_manager = get_interview_manager()

        # 同期的な状態取得を非同期で実行
        loop = asyncio.get_event_loop()

        def get_status_sync() -> Any:
            return interview_manager.get_session_status(session_id)

        status = await loop.run_in_executor(executor, get_status_sync)

        return JSONResponse(
            {"session_status": status, "timestamp": datetime.now().isoformat()}
        )

    except InterviewManagerError as e:
        logger.error(f"Error getting session status: {e}")
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.error(f"Unexpected error getting session status: {e}")
        return JSONResponse(
            {"error": "セッション状態の取得中にエラーが発生しました"}, status_code=500
        )


@router.post("/{session_id}/save", response_class=JSONResponse)
async def save_interview_session(
    request: Request, session_id: str, session_name: str = Form(...)
) -> Any:
    """インタビューセッション保存エンドポイント（拡張エラーハンドリング）"""
    try:
        interview_manager = get_interview_manager()

        # セッション状態を確認
        def get_session_status_sync() -> Any:
            try:
                return interview_manager.get_session_status(session_id)
            except InterviewSessionNotFoundError as e:
                raise e
            except Exception as e:
                raise InterviewSessionNotFoundError(f"セッション状態の取得に失敗: {e}")

        loop = asyncio.get_event_loop()

        try:
            session_status = await loop.run_in_executor(
                executor, get_session_status_sync
            )
        except InterviewSessionNotFoundError:
            return JSONResponse(
                {
                    "error": "インタビューセッションが見つかりません",
                    "error_type": "session_not_found",
                    "session_id": session_id,
                },
                status_code=404,
            )

        # 既に保存済みかチェック
        if session_status.get("is_saved", False):
            return JSONResponse(
                {
                    "message": "このセッションは既に保存されています",
                    "already_saved": True,
                    "session_status": session_status,
                    "timestamp": datetime.now().isoformat(),
                }
            )

        # メッセージがあるかチェック
        message_count = session_status.get("message_count", 0)
        if message_count == 0:
            return JSONResponse(
                {
                    "error": "保存するメッセージがありません",
                    "error_type": "validation_error",
                    "message_count": message_count,
                },
                status_code=400,
            )

        # ユーザーメッセージとペルソナ応答の両方があるかチェック
        if not session_status.get("has_user_messages", False):
            return JSONResponse(
                {
                    "error": "ユーザーメッセージが含まれていません",
                    "error_type": "validation_error",
                },
                status_code=400,
            )

        if not session_status.get("has_persona_responses", False):
            return JSONResponse(
                {
                    "error": "ペルソナの応答が含まれていません",
                    "error_type": "validation_error",
                },
                status_code=400,
            )

        # セッション名の検証
        if not session_name or not session_name.strip():
            return JSONResponse(
                {
                    "error": "セッション名が指定されていません",
                    "error_type": "validation_error",
                    "field": "session_name",
                },
                status_code=400,
            )

        session_name = session_name.strip()
        if len(session_name) > 100:
            return JSONResponse(
                {
                    "error": "セッション名が長すぎます（最大100文字）",
                    "error_type": "validation_error",
                    "field": "session_name",
                    "max_length": 100,
                    "provided_length": len(session_name),
                },
                status_code=400,
            )

        # 同期的な保存処理を非同期で実行
        def save_session_sync() -> Any:
            return interview_manager.save_interview_session(session_id, session_name)

        discussion_id = await loop.run_in_executor(executor, save_session_sync)

        # 保存後のセッション状態を取得
        updated_status = await loop.run_in_executor(executor, get_session_status_sync)

        logger.info(
            f"Interview session saved successfully: {session_id} -> {discussion_id}"
        )

        return JSONResponse(
            {
                "discussion_id": discussion_id,
                "message": "インタビューセッションが正常に保存されました",
                "session_status": updated_status,
                "save_timestamp": datetime.now().isoformat(),
                "message_count": message_count,
            }
        )

    except InterviewSessionNotFoundError as e:
        logger.error(f"Session not found for save: {e}")
        return JSONResponse(
            {
                "error": "インタビューセッションが見つかりません",
                "error_type": "session_not_found",
                "session_id": session_id,
            },
            status_code=404,
        )
    except InterviewValidationError as e:
        logger.error(f"Validation error during save: {e}")
        return JSONResponse(
            {"error": str(e), "error_type": "validation_error"}, status_code=400
        )
    except InterviewPersistenceError as e:
        logger.error(f"Persistence error during save: {e}")
        return JSONResponse(
            {
                "error": "データベースへの保存に失敗しました。しばらく待ってから再試行してください。",
                "error_type": "persistence_error",
                "technical_details": str(e),
            },
            status_code=503,
        )
    except Exception as e:
        logger.error(f"Unexpected error saving interview session: {e}")
        import traceback

        logger.error(f"Traceback: {traceback.format_exc()}")
        return JSONResponse(
            {
                "error": "セッション保存中に予期しないエラーが発生しました",
                "error_type": "internal_error",
            },
            status_code=500,
        )


@router.delete("/{session_id}", response_class=JSONResponse)
async def end_interview_session(request: Request, session_id: str) -> Any:
    """インタビューセッション終了エンドポイント"""
    try:
        interview_manager = get_interview_manager()

        # 同期的な終了処理を非同期で実行
        loop = asyncio.get_event_loop()

        def end_session_sync() -> Any:
            interview_manager.end_interview_session(session_id)

        await loop.run_in_executor(executor, end_session_sync)

        logger.info(f"Interview session ended: {session_id}")

        return JSONResponse({"message": "インタビューセッションが終了されました"})

    except InterviewManagerError as e:
        logger.error(f"Error ending interview session: {e}")
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"Unexpected error ending interview session: {e}")
        return JSONResponse(
            {"error": "インタビューセッションの終了中にエラーが発生しました"},
            status_code=500,
        )


# リアルタイムメッセージ送受信のためのSSEエンドポイント
@router.get("/{session_id}/stream")
async def stream_interview_messages(request: Request, session_id: str) -> Any:
    """インタビューメッセージのリアルタイムストリーミング（SSE）"""
    try:
        interview_manager = get_interview_manager()

        # セッションの存在確認
        session = interview_manager.get_interview_session(session_id)

        async def event_generator() -> Any:
            # 初期メッセージを送信
            yield f"data: {json.dumps({'type': 'connected', 'session_id': session_id}, ensure_ascii=False)}\n\n"

            # 既存のメッセージを送信
            for message in session.messages:
                message_data = {
                    "type": "message",
                    "persona_id": message.persona_id,
                    "persona_name": message.persona_name,
                    "content": message.content,
                    "message_type": message.message_type,
                    "timestamp": message.timestamp.isoformat()
                    if message.timestamp
                    else None,
                }
                yield f"data: {json.dumps(message_data, ensure_ascii=False)}\n\n"

            # Keep-alive（実際のリアルタイム更新は別途実装が必要）
            while True:
                await asyncio.sleep(30)  # 30秒ごとにkeep-alive
                yield f"data: {json.dumps({'type': 'keepalive'}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    except InterviewManagerError as e:
        logger.error(f"Interview session not found for streaming: {e}")
        return StreamingResponse(
            iter(
                [
                    f"data: {json.dumps({'type': 'error', 'message': 'セッションが見つかりません'}, ensure_ascii=False)}\n\n"
                ]
            ),
            media_type="text/event-stream",
        )
    except Exception as e:
        logger.error(f"Error starting interview stream: {e}")
        return StreamingResponse(
            iter(
                [
                    f"data: {json.dumps({'type': 'error', 'message': 'ストリーミングの開始中にエラーが発生しました'}, ensure_ascii=False)}\n\n"
                ]
            ),
            media_type="text/event-stream",
        )


@router.post("/cleanup", response_class=JSONResponse)
async def cleanup_inactive_sessions(request: Request, max_age_hours: int = 24) -> Any:
    """非アクティブなインタビューセッションのクリーンアップ"""
    try:
        interview_manager = get_interview_manager()

        # 同期的なクリーンアップ処理を非同期で実行
        loop = asyncio.get_event_loop()

        def cleanup_sync() -> Any:
            return interview_manager.cleanup_inactive_sessions(max_age_hours)

        cleaned_count = await loop.run_in_executor(executor, cleanup_sync)

        logger.info(f"Cleaned up {cleaned_count} inactive sessions")

        return JSONResponse(
            {
                "cleaned_sessions": cleaned_count,
                "message": f"{cleaned_count}個の非アクティブセッションをクリーンアップしました",
            }
        )

    except Exception as e:
        logger.error(f"Error during session cleanup: {e}")
        return JSONResponse(
            {"error": "セッションクリーンアップ中にエラーが発生しました"},
            status_code=500,
        )


@router.get("/stats", response_class=JSONResponse)
async def get_interview_stats(request: Request) -> Any:
    """インタビューシステムの統計情報取得"""
    try:
        interview_manager = get_interview_manager()

        # 同期的な統計取得を非同期で実行
        loop = asyncio.get_event_loop()

        def get_stats_sync() -> Any:
            active_count = interview_manager.get_active_sessions_count()
            return {
                "active_sessions": active_count,
                "timestamp": datetime.now().isoformat(),
            }

        stats = await loop.run_in_executor(executor, get_stats_sync)

        return JSONResponse(stats)

    except Exception as e:
        logger.error(f"Error getting interview stats: {e}")
        return JSONResponse(
            {"error": "統計情報の取得中にエラーが発生しました"}, status_code=500
        )
