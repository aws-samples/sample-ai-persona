"""
インタビュー関連のルーター

v1.0.0 U6: セッションはアプリの外に置かれ、持ち主だけが使える（BR3.2）。Router は
利用者（``request.state.principal``）を Manager に渡すだけで判定しない。添付は先に
``FileManager.upload_interview_attachment`` で置き、ID を Manager に渡す（BR3.6）。
会話を続けられなくなったとき（他人・期限切れのセッション、読めない参加者）は
``error_type: conversation_stopped`` を返し、画面が M12 の枠を出す。
"""

import logging
import asyncio
import json
from typing import Callable, List, Optional, Any
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from fastapi import APIRouter, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.managers.file_manager import FileManager, FileUploadError
from src.managers.persona_manager import PersonaManager
from src.managers.interview_manager import (
    InterviewManager,
    InterviewAgentError,
    InterviewPersistenceError,
)
from src.models.authz import Principal
from src.models.errors import (
    AccessDeniedError,
    CodedError,
    ConcurrentUpdateError,
    ErrorCode,
    ErrorKind,
    ParticipantNotFoundError,
)
from web.error_messages import (
    error_kind_of,
    is_correctable,
    mark_renderable,
    user_message_for,
    user_message_for_code,
)
from ._form_parsing import parse_persona_models

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

# 表示ヘルパー（ビジネスロジックを含まない純粋なURL組み立て）
from ._persona_avatar import persona_avatar_url  # noqa: E402

templates.env.globals["persona_avatar_url"] = persona_avatar_url

#: 保存名の上限（入力欄の検査）。
SESSION_NAME_MAX_LENGTH = 100
#: Where the stop panel (M12) of an interview leads back to.
_BACK_URL = "/discussion/results?mode=interview"


def _error_type_for(exc: Exception) -> str:
    """例外を画面表示用の error_type 文字列に変換する。

    フロントエンド（chat.html の showDetailedError）はこの値でアイコン・
    タイトル・再試行導線を切り替える。``conversation_stopped`` は M12 の枠
    （他人・期限切れのセッション、読めない参加者。理由は区別しない）。
    """
    if isinstance(exc, ConcurrentUpdateError):
        return "concurrent_update"
    if isinstance(exc, InterviewAgentError):
        return "agent_error"
    if isinstance(exc, InterviewPersistenceError):
        return "persistence_error"
    if isinstance(exc, AccessDeniedError):
        return "forbidden"
    if error_kind_of(exc) is ErrorKind.NOT_FOUND:
        return "conversation_stopped"
    if is_correctable(exc):
        return "validation_error"
    return "internal_error"


#: HTTP status of a coded failure by kind (the chat screen's JSON).
_STATUS_BY_KIND: dict[ErrorKind, int] = {
    ErrorKind.FORBIDDEN: 403,
    ErrorKind.NOT_FOUND: 404,
    ErrorKind.VALIDATION: 400,
    ErrorKind.CAPACITY: 400,
    ErrorKind.CONFIG: 500,
    ErrorKind.TRANSIENT: 503,
}


def _error_json(message: str, error_type: str, kind: ErrorKind) -> JSONResponse:
    """The JSON error of a coded failure (resolved wording only, never the exception)."""
    return JSONResponse(
        {"error": message, "error_type": error_type},
        status_code=_STATUS_BY_KIND.get(kind, 500),
    )


def _unexpected_json(message: str) -> JSONResponse:
    """The JSON error of an unclassified failure (500)."""
    return JSONResponse(
        {"error": message, "error_type": "internal_error"}, status_code=500
    )


from web.sanitize import render_markdown  # noqa: E402

templates.env.filters["markdown"] = render_markdown

# スレッドプールエグゼキューター（同期的なAI処理を非同期で実行するため）
executor = ThreadPoolExecutor(max_workers=8)

# シングルトンマネージャーインスタンス（モジュールレベルで共有）
_persona_manager = None
_interview_manager = None
_file_manager = None


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


def get_file_manager() -> FileManager:
    """FileManagerのシングルトンインスタンスを取得"""
    global _file_manager
    if _file_manager is None:
        _file_manager = FileManager()
    return _file_manager


def _principal(request: Request) -> Principal:
    """The signed-in user that ``AuthMiddleware`` put on the request (C1)."""
    principal: Principal = request.state.principal
    return principal


async def _run(func: Callable[..., Any], *args: Any) -> Any:
    """Run a synchronous Manager call (DynamoDB / AI) in the worker pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, func, *args)


def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_error(message: str, error_type: str) -> str:
    return _sse({"type": "error", "message": message, "error_type": error_type})


async def _upload_attachments(
    principal: Principal, files: Optional[List[UploadFile]]
) -> list[str]:
    """Place the attached files first and return their IDs (BR3.6, NFR1.54).

    Raises:
        FileUploadError: The kind or size is not accepted.
    """
    ids: list[str] = []
    file_manager = get_file_manager()
    for file in files or []:
        if not (file.filename and file.size and file.size > 0):
            continue
        content = await file.read()
        metadata = await _run(
            file_manager.upload_interview_attachment,
            principal,
            content,
            file.filename,
            file.content_type or "",
        )
        ids.append(metadata.file_id)
    return ids


@router.post("/create", response_class=JSONResponse)
async def create_interview_session(
    request: Request,
    persona_ids: List[str] = Form(...),
    enable_memory: bool = Form(False),
    memory_mode: str = Form("full"),
    enable_dataset: bool = Form(False),
    enable_kb: bool = Form(False),
) -> Any:
    """インタビューセッション作成エンドポイント（BR3.1）"""
    principal = _principal(request)
    try:
        personas = await _run(
            get_persona_manager().get_personas_for_use, principal, persona_ids
        )
    except ParticipantNotFoundError as e:
        logger.info("Interview not started: a participant is unreadable")
        return JSONResponse(
            {
                "error": user_message_for(e),
                "error_type": "persona_not_found",
                "error_code": ErrorCode.PERSONA_NOT_FOUND.value,
            },
            status_code=404,
        )
    except CodedError as e:
        logger.warning("Interview participants not read", exc_info=True)
        return _error_json(
            user_message_for(
                e, default="セッションの作成に失敗しました。再試行してください。"
            ),
            _error_type_for(e),
            error_kind_of(e),
        )

    form_data = await request.form()
    persona_models = parse_persona_models(form_data)

    def create_session_sync() -> Any:
        return get_interview_manager().start_interview_session(
            principal,
            personas,
            enable_memory=enable_memory,
            memory_mode=memory_mode,
            enable_dataset=enable_dataset,
            enable_kb=enable_kb,
            persona_models=persona_models,
        )

    try:
        session = await _run(create_session_sync)
    except CodedError as e:
        logger.error("Interview session not created", exc_info=True)
        return _error_json(
            user_message_for(
                e, default="セッションの作成に失敗しました。再試行してください。"
            ),
            _error_type_for(e),
            error_kind_of(e),
        )
    except Exception as e:
        logger.error("Unexpected error creating interview session", exc_info=True)
        return _unexpected_json(
            user_message_for(
                e,
                default="予期しないエラーが発生しました。"
                "システム管理者にお問い合わせください。",
            )
        )

    logger.info(f"Interview session created successfully: {session.id}")
    return JSONResponse(
        {
            "session_id": session.id,
            "participants": [{"id": p.id, "name": p.name} for p in personas],
            "message": "インタビューセッションが正常に作成されました",
            "created_at": session.created_at.isoformat(),
            "enable_memory": session.enable_memory,
            "memory_mode": session.memory_mode,
            "enable_dataset": enable_dataset,
            "enable_kb": enable_kb,
        }
    )


@router.get("/chat/{session_id}", response_class=HTMLResponse)
async def interview_chat_page(request: Request, session_id: str) -> Any:
    """インタビューチャットページ（持ち主だけ、BR3.2）"""
    principal = _principal(request)
    try:
        view = await _run(
            get_interview_manager().get_interview_session, principal, session_id
        )
    except CodedError as e:
        logger.info("Interview chat page not shown", exc_info=True)
        status = 404 if error_kind_of(e) is ErrorKind.NOT_FOUND else 503
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_banner.html",
                {
                    "request": request,
                    "error": user_message_for(
                        e,
                        default="指定されたインタビューセッションが見つかりません。",
                    ),
                },
                status_code=status,
            )
        )

    participants: list[Any] = []
    stopped = False
    try:
        participants = await _run(
            get_persona_manager().get_personas_for_use, principal, view.persona_ids
        )
    except ParticipantNotFoundError:
        # M12: 参加者を読めない。会話は表示するが、続けられない
        stopped = True
    except CodedError:
        logger.warning("Interview participants not read", exc_info=True)
        stopped = True

    return templates.TemplateResponse(
        request,
        "interview/chat.html",
        {
            "request": request,
            "title": "インタビューチャット",
            "session": view,
            "participants": participants,
            "stopped": stopped,
            "back_url": _BACK_URL,
        },
    )


@router.post("/{session_id}/message/stream")
async def send_message_stream(
    request: Request,
    session_id: str,
    message: str = Form(...),
    files: Optional[List[UploadFile]] = File(None),
) -> Any:
    """メッセージ送信ストリーミングエンドポイント（トークン単位SSE、BR3.3）"""
    principal = _principal(request)

    def single(event: str) -> StreamingResponse:
        return StreamingResponse(iter([event]), media_type="text/event-stream")

    if not message or not message.strip():
        return single(
            _sse(
                {
                    "type": "error",
                    "message": user_message_for_code(
                        ErrorCode.INTERVIEW_MESSAGE_REQUIRED
                    ),
                    "error_type": "validation_error",
                }
            )
        )
    try:
        document_ids = await _upload_attachments(principal, files)
    except FileUploadError as e:
        logger.warning("Interview attachment rejected", exc_info=True)
        return single(
            _sse_error(
                user_message_for(e, default="ファイルのアップロードに失敗しました"),
                _error_type_for(e),
            )
        )
    except CodedError as e:
        logger.warning("Interview attachment not placed", exc_info=True)
        return single(
            _sse_error(
                user_message_for(e, default="ファイルのアップロードに失敗しました"),
                _error_type_for(e),
            )
        )

    interview_manager = get_interview_manager()
    queue: asyncio.Queue[Any] = asyncio.Queue()

    def run_streaming() -> None:
        try:
            for event_type, data in interview_manager.send_user_message_streaming(
                principal, session_id, message.strip(), document_ids
            ):
                if event_type in ("message_start", "message_delta"):
                    sse = _sse({"type": event_type, **data})
                elif event_type == "message_end":
                    sse = _sse(
                        {
                            "type": "message_end",
                            "persona_id": data.persona_id,
                            "persona_name": data.persona_name,
                            "content": data.content,
                            "message_type": data.message_type,
                        }
                    )
                elif event_type == "files_excluded":
                    sse = _sse({"type": "files_excluded", "names": list(data)})
                else:
                    continue
                asyncio.run_coroutine_threadsafe(queue.put(sse), loop)
            asyncio.run_coroutine_threadsafe(
                queue.put(_sse({"type": "complete"})), loop
            )
        except Exception as e:
            logger.error("Interview streaming error", exc_info=True)
            error_sse = _sse_error(
                user_message_for(e, default="応答の生成中にエラーが発生しました"),
                _error_type_for(e),
            )
            asyncio.run_coroutine_threadsafe(queue.put(error_sse), loop)
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    loop = asyncio.get_running_loop()

    async def event_generator() -> Any:
        executor.submit(run_streaming)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30)
                if event is None:
                    break
                yield event
            except asyncio.TimeoutError:
                yield _sse({"type": "keepalive"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{session_id}/message", response_class=JSONResponse)
async def send_message(
    request: Request,
    session_id: str,
    message: str = Form(...),
    files: Optional[List[UploadFile]] = File(None),
) -> Any:
    """メッセージ送信エンドポイント（マルチモーダル対応、BR3.3）"""
    principal = _principal(request)
    try:
        document_ids = await _upload_attachments(principal, files)
        reply = await _run(
            get_interview_manager().send_user_message,
            principal,
            session_id,
            message.strip(),
            document_ids,
        )
    except CodedError as e:
        logger.warning("Interview message not sent", exc_info=True)
        return _error_json(
            user_message_for(e, default="メッセージの送信中にエラーが発生しました"),
            _error_type_for(e),
            error_kind_of(e),
        )
    except Exception as e:
        logger.error("Unexpected error in message processing", exc_info=True)
        return _unexpected_json(
            user_message_for(
                e, default="メッセージの送信中に予期しないエラーが発生しました"
            )
        )

    response_data = [
        {
            "persona_id": response.persona_id,
            "persona_name": response.persona_name,
            "content": response.content,
            "content_html": render_markdown(response.content),
            "timestamp": response.timestamp.isoformat() if response.timestamp else None,
            "message_type": response.message_type,
        }
        for response in reply.responses
    ]
    return JSONResponse(
        {
            "user_message": message.strip(),
            "responses": response_data,
            "excluded_files": reply.excluded_files,
            "message": "メッセージが正常に送信されました",
            "response_count": len(response_data),
            "timestamp": datetime.now().isoformat(),
        }
    )


@router.get("/{session_id}/messages", response_class=JSONResponse)
async def get_messages(request: Request, session_id: str) -> Any:
    """メッセージ履歴取得エンドポイント（持ち主だけ）"""
    try:
        view = await _run(
            get_interview_manager().get_interview_session,
            _principal(request),
            session_id,
        )
    except CodedError as e:
        logger.info("Interview messages not shown", exc_info=True)
        return _error_json(
            user_message_for(e, default="メッセージ履歴が見つかりません"),
            _error_type_for(e),
            error_kind_of(e),
        )

    messages_data = [
        {
            "persona_id": message.persona_id,
            "persona_name": message.persona_name,
            "content": message.content,
            "message_type": message.message_type,
            "timestamp": message.timestamp.isoformat() if message.timestamp else None,
        }
        for message in view.messages
    ]
    return JSONResponse(
        {
            "session_id": session_id,
            "messages": messages_data,
            "message_count": len(messages_data),
        }
    )


# 受け身の要求（BR1.9）: 状態の確認。web/auth_middleware.py の PASSIVE_REQUESTS に載せている
@router.get("/{session_id}/status", response_class=JSONResponse)
async def get_session_status(request: Request, session_id: str) -> Any:
    """インタビューセッション状態取得エンドポイント（持ち主だけ）"""
    try:
        status = await _run(
            get_interview_manager().get_session_status,
            _principal(request),
            session_id,
        )
    except CodedError as e:
        logger.info("Interview status not shown", exc_info=True)
        return _error_json(
            user_message_for(e, default="セッションが見つかりません"),
            _error_type_for(e),
            error_kind_of(e),
        )
    return JSONResponse(
        {"session_status": status, "timestamp": datetime.now().isoformat()}
    )


@router.post("/{session_id}/save", response_class=JSONResponse)
async def save_interview_session(
    request: Request, session_id: str, session_name: str = Form(...)
) -> Any:
    """インタビューを議論として保存する（2 回目以降は同じ議論に書き足す、BR3.7、BR3.8）"""
    name = (session_name or "").strip()
    if not name:
        return JSONResponse(
            {
                "error": user_message_for_code(
                    ErrorCode.INTERVIEW_SESSION_NAME_REQUIRED
                ),
                "error_type": "validation_error",
                "field": "session_name",
            },
            status_code=400,
        )
    if len(name) > SESSION_NAME_MAX_LENGTH:
        return JSONResponse(
            {
                "error": user_message_for_code(
                    ErrorCode.INTERVIEW_SESSION_NAME_TOO_LONG,
                    {"max_length": SESSION_NAME_MAX_LENGTH},
                ),
                "error_type": "validation_error",
                "field": "session_name",
                "max_length": SESSION_NAME_MAX_LENGTH,
            },
            status_code=400,
        )
    principal = _principal(request)
    interview_manager = get_interview_manager()
    try:
        result = await _run(
            interview_manager.save_interview_session, principal, session_id, name
        )
        status = await _run(interview_manager.get_session_status, principal, session_id)
    except CodedError as e:
        logger.warning("Interview session not saved", exc_info=True)
        return _error_json(
            user_message_for(e, default="セッション保存中にエラーが発生しました"),
            _error_type_for(e),
            error_kind_of(e),
        )
    except Exception as e:
        logger.error("Unexpected error saving interview session", exc_info=True)
        return _unexpected_json(
            user_message_for(
                e, default="セッション保存中に予期しないエラーが発生しました"
            )
        )

    logger.info(f"Interview session saved: {session_id} -> {result.discussion_id}")
    return JSONResponse(
        {
            "discussion_id": result.discussion_id,
            "excluded_files": result.excluded_files,
            "message": "インタビューセッションが正常に保存されました",
            "session_status": status,
            "save_timestamp": datetime.now().isoformat(),
            "message_count": status.get("message_count", 0),
        }
    )


@router.delete("/{session_id}", response_class=JSONResponse)
async def end_interview_session(request: Request, session_id: str) -> Any:
    """インタビューセッション終了エンドポイント（持ち主だけ）"""
    try:
        await _run(
            get_interview_manager().end_interview_session,
            _principal(request),
            session_id,
        )
    except CodedError as e:
        logger.warning("Interview session not ended", exc_info=True)
        return _error_json(
            user_message_for(e, default="インタビューセッションの終了に失敗しました"),
            _error_type_for(e),
            error_kind_of(e),
        )
    except Exception as e:
        logger.error("Unexpected error ending interview session", exc_info=True)
        return _unexpected_json(
            user_message_for(
                e, default="インタビューセッションの終了中にエラーが発生しました"
            )
        )
    logger.info(f"Interview session ended: {session_id}")
    return JSONResponse({"message": "インタビューセッションが終了されました"})


#: Seconds between the stream's keepalives (the session is read again each time).
_STREAM_KEEPALIVE_SECONDS = 30


# リアルタイムメッセージ送受信のためのSSEエンドポイント
# 受け身の要求（BR1.9）: SSE。web/auth_middleware.py の PASSIVE_REQUESTS に載せている
@router.get("/{session_id}/stream")
async def stream_interview_messages(request: Request, session_id: str) -> Any:
    """インタビューメッセージのストリーミング（SSE、持ち主だけ）。

    接続時に持ち主と期限を照合し（BR3.2）、keepalive のたびにセッションを読み直して
    期限が来たら ``stopped`` を送って切る（security-design.md §2）。
    """
    principal = _principal(request)
    interview_manager = get_interview_manager()
    try:
        view = await _run(
            interview_manager.get_interview_session, principal, session_id
        )
    except CodedError as e:
        logger.info("Interview stream not opened", exc_info=True)
        return StreamingResponse(
            iter(
                [
                    _sse_error(
                        user_message_for(e, default="セッションが見つかりません"),
                        _error_type_for(e),
                    )
                ]
            ),
            media_type="text/event-stream",
        )

    async def event_generator() -> Any:
        yield _sse({"type": "connected", "session_id": session_id})
        for message in view.messages:
            yield _sse(
                {
                    "type": "message",
                    "persona_id": message.persona_id,
                    "persona_name": message.persona_name,
                    "content": message.content,
                    "message_type": message.message_type,
                    "timestamp": message.timestamp.isoformat()
                    if message.timestamp
                    else None,
                }
            )
        while True:
            await asyncio.sleep(_STREAM_KEEPALIVE_SECONDS)
            try:
                await _run(interview_manager.get_session_status, principal, session_id)
            except CodedError:
                yield _sse({"type": "stopped"})
                return
            yield _sse({"type": "keepalive"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/stats", response_class=JSONResponse)
async def get_interview_stats(request: Request) -> Any:
    """この台が手元に持つインタビューの数（全体の件数は数えない、BR3.5）"""
    try:
        active_count = await _run(get_interview_manager().get_active_sessions_count)
    except Exception as e:
        logger.error(f"Error getting interview stats: {type(e).__name__}")
        return JSONResponse(
            {
                "error": user_message_for(
                    e, default="統計情報の取得中にエラーが発生しました"
                )
            },
            status_code=500,
        )
    return JSONResponse(
        {"active_sessions": active_count, "timestamp": datetime.now().isoformat()}
    )
