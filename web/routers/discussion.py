"""
議論関連のルーター

v1.0.0 U6: Router は判定しない。``request.state.principal`` を Manager に渡し、
判定の例外（``*_NOT_FOUND``・``ACCESS_DENIED``・``LOOKUP_UNAVAILABLE``）は
共通の例外ハンドラ（約束 C13）に任せる。議論の開始で参加者が読めなければ
M12 の枠（``discussion/partials/conversation_stopped.html``）を返す。
"""

import logging
import asyncio
import json
import queue as queue_mod
from typing import Any, Callable, Optional, List
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, Response, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.managers.persona_manager import PersonaManager
from src.managers.discussion_manager import DiscussionManager, DiscussionManagerError
from src.managers.agent_discussion_manager import (
    AgentDiscussionManager,
    AgentDiscussionManagerError,
)
from src.managers.report_manager import ReportManager
from src.managers.file_manager import FileManager, FileUploadError
from src.config import config
from src.models.authz import Principal
from src.models.discussion_report import REPORT_TEMPLATE_TYPES, DiscussionReport
from src.models.errors import (
    CodedError,
    ErrorCode,
    ErrorKind,
    ParticipantNotFoundError,
)
from src.models.insight_category import InsightCategory
from src.models.model_registry import display_name_for, list_selectable_models
from web.error_messages import (
    error_kind_of,
    is_correctable,
    mark_renderable,
    toast_response,
    user_message_for,
    user_message_for_code,
)
from ._asset_response import asset_response, content_disposition
from ._form_parsing import parse_persona_models

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


from web.sanitize import render_markdown  # noqa: E402

templates.env.filters["markdown"] = render_markdown

# 表示用ヘルパーをテンプレートのグローバル関数として登録（persona.py と同様）。
# country_service は ISO国コード→名前の純粋なデータ参照であり、表示ヘルパーとして
# Router から直接利用する（アーキ規約「Router→Manager経由」の表示ヘルパー例外）。
from src.services import country_service  # noqa: E402
from src.models.demographics import gender_label, GENDER_LABELS  # noqa: E402
from ._persona_avatar import persona_avatar_url  # noqa: E402

templates.env.globals["country_name"] = country_service.country_name
templates.env.globals["country_choices"] = country_service.country_choices
templates.env.globals["gender_label"] = gender_label
templates.env.globals["GENDER_LABELS"] = GENDER_LABELS
templates.env.globals["persona_avatar_url"] = persona_avatar_url
templates.env.globals["model_display_name"] = display_name_for

# スレッドプールエグゼキューター（同期的なAI処理を非同期で実行するため）
executor = ThreadPoolExecutor(max_workers=8)

#: The tab panel of the results page (``list_tabpanel``); tab switches and
#: ``listReload`` target it and get only the panel content back.
LIST_PANEL_ID = "discussion-list"

#: Where the stop panel (M12) of a discussion leads back to.
_SETUP_URL = "/discussion/setup"


# シングルトンマネージャーインスタンス（モジュールレベルで共有）
_persona_manager = None
_discussion_manager = None
_agent_discussion_manager = None
_report_manager = None
_file_manager = None


def get_persona_manager() -> PersonaManager:
    """PersonaManagerのシングルトンインスタンスを取得"""
    global _persona_manager
    if _persona_manager is None:
        _persona_manager = PersonaManager()
    return _persona_manager


def get_discussion_manager() -> DiscussionManager:
    """DiscussionManagerのシングルトンインスタンスを取得"""
    global _discussion_manager
    if _discussion_manager is None:
        _discussion_manager = DiscussionManager()
    return _discussion_manager


def get_file_manager() -> FileManager:
    """FileManagerのシングルトンインスタンスを取得"""
    global _file_manager
    if _file_manager is None:
        _file_manager = FileManager()
    return _file_manager


def get_agent_discussion_manager() -> AgentDiscussionManager:
    """AgentDiscussionManagerのシングルトンインスタンスを取得"""
    global _agent_discussion_manager
    if _agent_discussion_manager is None:
        _agent_discussion_manager = AgentDiscussionManager()
    return _agent_discussion_manager


def get_report_manager() -> ReportManager:
    """ReportManagerのシングルトンインスタンスを取得"""
    global _report_manager
    if _report_manager is None:
        _report_manager = ReportManager()
    return _report_manager


def _principal(request: Request) -> Principal:
    """The signed-in user that ``AuthMiddleware`` put on the request (C1)."""
    principal: Principal = request.state.principal
    return principal


async def _run(func: Callable[..., Any], *args: Any) -> Any:
    """Run a synchronous Manager call (DynamoDB / AI) in the worker pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, func, *args)


def _sse(data: dict[str, Any], event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_error(message: str) -> str:
    return _sse({"type": "error", "message": message})


def _attachment(filename: str) -> str:
    """``Content-Disposition`` of a download (the name may be Japanese)."""
    return content_disposition("attachment", filename)


def _conversation_stopped(request: Request) -> Response:
    """M12: a participant can no longer be read; the setup form is kept."""
    return mark_renderable(
        templates.TemplateResponse(
            request,
            "discussion/partials/conversation_stopped.html",
            {
                "request": request,
                "kind": "discussion",
                "back_url": _SETUP_URL,
                "back_label": "議論の設定へ",
            },
            status_code=404,
        )
    )


def _parse_categories_from_form(form_data) -> Optional[List[InsightCategory]]:  # type: ignore[no-untyped-def]
    """フォームデータからカテゴリー情報を解析"""
    categories = []
    index = 0

    while True:
        name_key = f"categories[{index}][name]"
        desc_key = f"categories[{index}][description]"

        if name_key not in form_data:
            break

        name = form_data.get(name_key, "").strip()
        description = form_data.get(desc_key, "").strip()

        # 名前と説明が両方ある場合のみ追加
        if name and description:
            try:
                category = InsightCategory(name=name, description=description)
                categories.append(category)
            except ValueError as e:
                logger.warning(f"Invalid category at index {index}: {e}")

        index += 1

    # カテゴリーが見つからない場合はNone（デフォルトを使用）
    return categories if categories else None


@router.get("/setup", response_class=HTMLResponse)
async def discussion_setup_page(request: Request) -> Any:
    """議論設定ページ（ペルソナ一覧は htmx で遅延ロード）"""
    selectable_models = list_selectable_models(config.ENABLE_ADDITIONAL_PERSONA_MODELS)
    return templates.TemplateResponse(
        request,
        "discussion/setup.html",
        {
            "request": request,
            "title": "議論設定",
            "selectable_models": selectable_models,
            "enable_additional_models": config.ENABLE_ADDITIONAL_PERSONA_MODELS,
            "default_model_id": config.AGENT_MODEL_ID,
        },
    )


@router.post("/upload-document")
async def upload_discussion_document(
    request: Request, file: UploadFile = File(...)
) -> Any:
    """議論用ドキュメントをアップロード（利用者のキーに置き、24 時間で使えなくなる、BR5.1）"""
    try:
        file_manager = get_file_manager()
        file_content = await file.read()
        file_metadata = await _run(
            file_manager.upload_discussion_document,
            _principal(request),
            file_content,
            file.filename or "",
        )
        return JSONResponse(
            {
                "file_id": file_metadata.file_id,
                "filename": file_metadata.original_filename,
                "file_size": file_metadata.file_size,
                "mime_type": file_metadata.mime_type,
                "uploaded_at": file_metadata.uploaded_at.isoformat(),
            }
        )

    except FileUploadError as e:
        logger.warning("Document upload error", exc_info=True)
        return JSONResponse(
            {
                "error": user_message_for(
                    e, default="ドキュメントのアップロードに失敗しました"
                )
            },
            status_code=400,
        )
    except Exception as e:
        logger.error("Document upload error", exc_info=True)
        return JSONResponse(
            {
                "error": user_message_for(
                    e, default="ドキュメントのアップロードに失敗しました"
                )
            },
            status_code=400,
        )


@router.get("/result-partial/{discussion_id}", response_class=HTMLResponse)
async def get_discussion_result_partial(request: Request, discussion_id: str) -> Any:
    """議論結果パーシャルを取得（リアルタイム表示完了後に使用）。読める人だけ（BR2.4）"""
    try:
        view = await _run(
            get_discussion_manager().get_discussion, _principal(request), discussion_id
        )
    except CodedError as e:
        logger.info("Discussion result partial not shown", exc_info=True)
        status = 404 if error_kind_of(e) is ErrorKind.NOT_FOUND else 503
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_banner.html",
                {
                    "request": request,
                    "error": user_message_for(
                        e, default="議論結果の取得に失敗しました"
                    ),
                    "back_url": "/discussion/results",
                    "back_label": "議論一覧に戻る",
                },
                status_code=status,
            )
        )
    return templates.TemplateResponse(
        request,
        "discussion/partials/discussion_result.html",
        {"request": request, "discussion": view},
    )


# 受け身の要求（BR1.9）: SSE。web/auth_middleware.py の PASSIVE_REQUESTS に載せている
# GET で AI を動かすため web/middleware.py が Sec-Fetch-Site を確かめる（NFR4.13）
@router.get("/stream")
async def stream_discussion(
    request: Request,
    topic: str,
    persona_ids: str,
    mode: str = "traditional",
    rounds: int = 3,
    additional_instructions: str = "",
    enable_memory: bool = False,
    memory_mode: str = "full",
    enable_dataset: bool = False,
    enable_kb: bool = False,
    categories_json: str = "",
    document_ids: str = "",
    persona_models_json: str = "",
    facilitator_model: str = "",
) -> Any:
    """議論ストリーミングエンドポイント（SSE）- 簡易モード・エージェントモード両対応"""
    principal = _principal(request)
    ids = [pid.strip() for pid in persona_ids.split(",") if pid.strip()]

    def single(event: str) -> StreamingResponse:
        return StreamingResponse(iter([event]), media_type="text/event-stream")

    if len(ids) < 2:
        return single(
            _sse_error(
                user_message_for_code(
                    ErrorCode.DISCUSSION_TOO_FEW_PERSONAS, {"min_personas": 2}
                )
            )
        )
    try:
        personas = await _run(
            get_persona_manager().get_personas_for_use, principal, ids
        )
    except CodedError as e:
        if isinstance(e, ParticipantNotFoundError):
            # M12: 読めない参加者を含む。理由は出さず、画面が枠を出す
            return single(_sse({"type": "stopped"}))
        logger.warning("Discussion stream not started", exc_info=True)
        return single(
            _sse_error(
                user_message_for(e, default="議論の実行中にエラーが発生しました")
            )
        )

    # カテゴリー情報を解析
    categories = None
    if categories_json:
        try:
            categories = [
                InsightCategory(name=c["name"], description=c["description"])
                for c in json.loads(categories_json)
            ]
        except Exception as e:
            logger.warning(f"Failed to parse category: {type(e).__name__}")

    doc_ids = [d.strip() for d in document_ids.split(",") if d.strip()] or None

    # per-persona モデル選択を解析（URLエンコードしたJSON文字列1個で受ける）
    persona_models = None
    if persona_models_json:
        try:
            persona_models = json.loads(persona_models_json) or None
        except Exception as e:
            logger.warning(f"Failed to parse persona_models_json: {type(e).__name__}")

    facilitator_model_id = facilitator_model.strip() or None

    participants_data = {
        "type": "participants",
        "personas": [
            {"id": p.id, "name": p.name, "avatar_url": persona_avatar_url(p)}
            for p in personas
        ],
        "mode": mode,
    }

    async def event_generator() -> Any:
        yield _sse(participants_data)

        queue: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def run_sync_generator() -> None:
            try:
                if mode == "agent":
                    gen = get_agent_discussion_manager().run_agent_discussion_streaming(
                        principal,
                        personas,
                        topic,
                        rounds=rounds,
                        additional_instructions=additional_instructions,
                        enable_memory=enable_memory,
                        memory_mode=memory_mode,
                        enable_dataset=enable_dataset,
                        enable_kb=enable_kb,
                        categories=categories,
                        document_ids=doc_ids,
                        persona_models=persona_models,
                        facilitator_model=facilitator_model_id,
                    )
                else:
                    gen = get_discussion_manager().run_classic_discussion_streaming(
                        principal,
                        personas,
                        topic,
                        categories=categories,
                        document_ids=doc_ids,
                    )
                for event in gen:
                    asyncio.run_coroutine_threadsafe(queue.put(event), loop)
            except Exception as e:
                logger.error("Streaming processing error", exc_info=True)
                if isinstance(e, ParticipantNotFoundError):
                    error_event = _sse({"type": "stopped"})
                else:
                    error_event = _sse_error(
                        user_message_for(
                            e, default="議論の実行中にエラーが発生しました"
                        )
                    )
                asyncio.run_coroutine_threadsafe(queue.put(error_event), loop)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        executor.submit(run_sync_generator)

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
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


@router.get("/results", response_class=HTMLResponse)
async def discussion_results_page(
    request: Request,
    tab: Optional[str] = None,
    mode: Optional[str] = None,
    search: Optional[str] = None,
    cursor: Optional[str] = None,
    append: bool = False,
) -> Any:
    """議論・インタビュー結果の一覧（タブ別、M6、BR2.1）。

    タブの切り替え・検索・絞り込み・読み直し（HX-Target が一覧部分）と
    「もっと見る」には一覧部分だけを返す。member の tab=all は「自分のもの」。
    """
    panel_only = request.headers.get("HX-Target") == LIST_PANEL_ID or append
    search_query = (search or "").strip()
    current_mode = mode if mode in ("agent", "classic", "interview") else None
    try:
        listing = await _run(
            get_discussion_manager().list_discussions,
            _principal(request),
            tab,
            cursor,
            current_mode,
            search_query or None,
        )
        error = None
    except CodedError as e:
        logger.warning("Discussion list fetch error", exc_info=True)
        listing = None
        error = user_message_for(e, default="議論一覧の取得に失敗しました")

    context = {
        "request": request,
        "title": "議論結果",
        "listing": listing,
        "tab": tab,
        "current_mode": current_mode,
        "current_search": search_query,
        "is_append": append,
        "error": error,
    }
    if listing is None and panel_only:
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_banner.html",
                {"request": request, "error": error},
                status_code=503,
            )
        )
    if panel_only:
        return templates.TemplateResponse(
            request, "discussion/partials/discussion_list.html", context
        )
    return templates.TemplateResponse(request, "discussion/results.html", context)


@router.post("/start", response_class=HTMLResponse)
async def start_discussion(
    request: Request,
    topic: str = Form(...),
    persona_ids: List[str] = Form(...),
    mode: str = Form("traditional"),
    rounds: int = Form(3),
    additional_instructions: str = Form(""),
    enable_memory: bool = Form(False),
    memory_mode: str = Form("full"),
    document_ids: Optional[List[str]] = Form(None),
) -> Any:
    """議論開始処理（htmx対応）"""
    if mode == "interview" or len(persona_ids) < 2:
        code = (
            ErrorCode.DISCUSSION_INTERVIEW_MODE_UNSUPPORTED
            if mode == "interview"
            else ErrorCode.DISCUSSION_TOO_FEW_PERSONAS
        )
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_inline.html",
                {
                    "request": request,
                    "error": user_message_for_code(code, {"min_personas": 2}),
                },
                status_code=400,
            )
        )
    principal = _principal(request)
    try:
        personas = await _run(
            get_persona_manager().get_personas_for_use, principal, persona_ids
        )

        form_data = await request.form()
        categories = _parse_categories_from_form(form_data)
        persona_models = parse_persona_models(form_data)
        facilitator_model_raw = form_data.get("facilitator_model", "")
        facilitator_model = (
            facilitator_model_raw.strip()
            if isinstance(facilitator_model_raw, str)
            else ""
        ) or None

        if mode == "agent":

            def _run_agent() -> Any:
                return get_agent_discussion_manager().run_agent_discussion_full(
                    principal,
                    personas,
                    topic,
                    rounds=rounds,
                    additional_instructions=additional_instructions,
                    enable_memory=enable_memory,
                    memory_mode=memory_mode,
                    categories=categories,
                    document_ids=document_ids,
                    persona_models=persona_models,
                    facilitator_model=facilitator_model,
                )

            saved = await _run(_run_agent)
        else:

            def _run_classic() -> Any:
                return get_discussion_manager().run_classic_discussion(
                    principal,
                    personas,
                    topic,
                    categories=categories,
                    document_ids=document_ids,
                )

            saved = await _run(_run_classic)
        # 発言は本体の外にあるため、保存した議論を読む人として読み直して表示する
        view = await _run(get_discussion_manager().get_discussion, principal, saved.id)
    except CodedError as e:
        if isinstance(e, ParticipantNotFoundError):
            logger.info("Discussion not started: a participant is unreadable")
            return _conversation_stopped(request)
        logger.error("Failed to start discussion", exc_info=True)
        if error_kind_of(e) is ErrorKind.NOT_FOUND:
            # 他人の・期限切れの資料は、ないものと同じ 404 のトースト（BR5.3、BR5.4）
            return toast_response(e, status_code=404)
        if error_kind_of(e) is ErrorKind.FORBIDDEN:
            return toast_response(e, status_code=403)
        if is_correctable(e) and isinstance(
            e, (DiscussionManagerError, AgentDiscussionManagerError)
        ):
            # 入力を直せば解決するエラー（トピック文字数・ペルソナ数等）は
            # #discussion-result（フォーム自身の外側の専用領域）に表示する。
            return mark_renderable(
                templates.TemplateResponse(
                    request,
                    "partials/error_inline.html",
                    {
                        "request": request,
                        "error": user_message_for(
                            e, default="議論の開始中にエラーが発生しました"
                        ),
                    },
                    status_code=400,
                )
            )
        # 再試行で解決しうるエラーは設定フォームを消さずトーストで通知する
        return toast_response(
            e, default="議論の開始中にエラーが発生しました", status_code=500
        )
    except Exception as e:
        # コードを持たない未知の例外は TRANSIENT 相当。本体を置換すると入力が
        # 失われ、かつ非2xx本文は htmx がスワップしないため、トーストで通知する。
        logger.error("Failed to start discussion (unclassified)", exc_info=True)
        return toast_response(
            e, default="議論の開始中にエラーが発生しました", status_code=500
        )

    return templates.TemplateResponse(
        request,
        "discussion/partials/discussion_result.html",
        {"request": request, "discussion": view},
    )


@router.get("/{discussion_id}/export")
async def export_discussion(request: Request, discussion_id: str) -> Any:
    """議論を Markdown でエクスポートする（読める人だけ、BR9.1）"""
    filename, text = await _run(
        get_discussion_manager().export_discussion, _principal(request), discussion_id
    )
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": _attachment(filename)},
    )


@router.get("/{discussion_id}/documents/{document_id}")
async def get_discussion_document(
    request: Request, discussion_id: str, document_id: str
) -> Any:
    """議論の添付 1 つ（閉域対応 U4 BR3.5）。

    議論を読めて、その議論の添付で、添付を読めるときだけ配る。公開は署名付き URL へ
    302、閉域はアプリが流す。読めなければ共通の例外ハンドラが 404 で答え、S3 に触れない。
    """
    result = await _run(
        get_discussion_manager().deliver_document,
        _principal(request),
        discussion_id,
        document_id,
    )
    return asset_response(result)


@router.get("/{discussion_id}", response_class=HTMLResponse)
async def get_discussion_detail(request: Request, discussion_id: str) -> Any:
    """議論詳細ページ（インタビューを含む）。読める人だけ（BR2.4）、操作は管理できる人だけ"""
    from src.managers.settings_manager import SettingsManager

    principal = _principal(request)
    manager = get_discussion_manager()
    view = await _run(manager.get_discussion, principal, discussion_id)
    # 添付はアプリのルートで開く（公開・閉域とも。閉域対応 U4 BR4.2）。署名はここで作らない
    servable = await _run(manager.servable_document_ids, principal, view)
    document_urls = {
        doc_id: f"/discussion/{quote(discussion_id, safe='')}/documents/"
        f"{quote(doc_id, safe='')}"
        for doc_id in servable
    }
    custom_categories = (view.agent_config or {}).get("insight_categories")
    title = (
        "インタビューセッション" if view.mode == "interview" else f"議論: {view.topic}"
    )
    return templates.TemplateResponse(
        request,
        "discussion/detail.html",
        {
            "request": request,
            "title": title,
            "discussion": view,
            "manageable": view.manageable,
            "insights": view.insights,
            "discussion_id": discussion_id,
            "default_categories": manager.get_default_categories(),
            "custom_categories": custom_categories,
            "document_urls": document_urls,
            "enable_data_driven_report": SettingsManager.is_data_agent_available(),
        },
    )


@router.post("/{discussion_id}/regenerate-insights", response_class=HTMLResponse)
async def regenerate_insights(request: Request, discussion_id: str) -> Any:
    """インサイト再生成エンドポイント（htmx対応、管理できる人だけ、BR2.5）"""
    discussion_manager = get_discussion_manager()
    try:
        form_data = await request.form()
        categories = _parse_categories_from_form(form_data)
        new_insights = await _run(
            discussion_manager.regenerate_insights,
            _principal(request),
            discussion_id,
            categories,
        )
    except DiscussionManagerError as e:
        logger.error("Failed to regenerate insight", exc_info=True)
        if is_correctable(e):
            # 入力を直せば解決するエラーは #insights-container（再生成フォームの
            # 外側の専用領域）に表示する。カテゴリー入力はモーダル内の別要素で残る。
            return mark_renderable(
                templates.TemplateResponse(
                    request,
                    "partials/error_inline.html",
                    {
                        "request": request,
                        "error": user_message_for(
                            e, default="インサイトの再生成中にエラーが発生しました"
                        ),
                    },
                    status_code=400,
                )
            )
        # 再試行で解決しうるエラーはインサイト表示を消さずトーストで通知する
        return toast_response(
            e,
            default="インサイトの再生成中にエラーが発生しました",
            status_code=500,
        )
    except CodedError:
        # 読めない（404）・管理できない（403）・ai:execute がない（403）は共通のハンドラ
        raise
    except Exception as e:
        logger.error("Failed to regenerate insight (unclassified)", exc_info=True)
        return toast_response(
            e,
            default="インサイトの再生成中にエラーが発生しました",
            status_code=500,
        )

    return templates.TemplateResponse(
        request,
        "discussion/partials/insights.html",
        {
            "request": request,
            "insights": new_insights,
            "discussion_id": discussion_id,
            "manageable": True,
            "default_categories": discussion_manager.get_default_categories(),
        },
    )


@router.delete("/{discussion_id}", response_class=HTMLResponse)
async def delete_discussion(request: Request, discussion_id: str) -> Any:
    """議論削除処理（htmx対応、管理できる人だけ、BR1.5）"""
    try:
        await _run(
            get_discussion_manager().delete_discussion,
            _principal(request),
            discussion_id,
        )
    except DiscussionManagerError as e:
        logger.error("Failed to delete discussion", exc_info=True)
        return toast_response(e, default="議論の削除に失敗しました", status_code=500)
    except CodedError:
        raise
    except Exception as e:
        logger.error("Failed to delete discussion (unclassified)", exc_info=True)
        return toast_response(e, default="議論の削除に失敗しました", status_code=500)

    referer = request.headers.get("hx-current-url", "")
    if f"/discussion/{discussion_id}" in referer:
        return HTMLResponse(content="", headers={"HX-Redirect": "/discussion/results"})
    return templates.TemplateResponse(
        request,
        "partials/success.html",
        {"request": request, "message": "議論を削除しました"},
    )


@router.get("/insights/{discussion_id}", response_class=HTMLResponse)
async def get_discussion_insights(request: Request, discussion_id: str) -> Any:
    """議論インサイト取得（htmx対応、読める人だけ）"""
    try:
        view = await _run(
            get_discussion_manager().get_discussion, _principal(request), discussion_id
        )
    except CodedError as e:
        logger.info("Discussion insights not shown", exc_info=True)
        status = 404 if error_kind_of(e) is ErrorKind.NOT_FOUND else 503
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_banner.html",
                {
                    "request": request,
                    "error": user_message_for(
                        e, default="インサイトの取得に失敗しました"
                    ),
                    "back_url": "/discussion/results",
                    "back_label": "議論一覧に戻る",
                },
                status_code=status,
            )
        )
    return templates.TemplateResponse(
        request,
        "discussion/partials/insights.html",
        {
            "request": request,
            "insights": view.insights,
            "discussion_id": discussion_id,
            "manageable": view.manageable,
        },
    )


# =============================================================================
# レポート関連エンドポイント
# =============================================================================


def _data_agent_refusal(
    report_manager: ReportManager, principal: Principal
) -> StreamingResponse | None:
    """One ``error`` event when the data agent may not be used (U3 BR3.7).

    Checked before the stream starts, so the agent is never called; ``None``
    lets the request go on.
    """
    try:
        report_manager.ensure_data_agent_allowed(principal)
    except CodedError as e:
        logger.info("Data-driven report refused: %s", error_kind_of(e).value)
        return StreamingResponse(
            iter([_sse_error(user_message_for(e))]), media_type="text/event-stream"
        )
    return None


def _agent_event_sse(evt: dict[str, Any]) -> str | None:
    """One event of the data-driven report agent as SSE (``None`` to skip)."""
    evt_type = evt.get("type", "")
    content = evt.get("content", "")
    if evt_type == "tool_call":
        return _sse(
            {"type": "tool_call", "content": content, "detail": evt.get("detail", "")},
            event="thinking",
        )
    if evt_type == "tool_result" and content:
        return _sse({"type": "tool_result", "content": content}, event="thinking")
    if evt_type == "csv_url" and evt.get("url"):
        return _sse({"type": "csv_url", "url": evt.get("url", "")})
    if evt_type in ("thinking", "result") and content:
        return _sse({"type": "chunk", "content": content})
    if evt_type == "error":
        return _sse_error(content)
    return None


def _stream_agent_events(run: Callable[[Any], None], default: str) -> Any:
    """Run ``run(event_queue)`` in the pool and relay its events as SSE."""
    eq: queue_mod.Queue = queue_mod.Queue()
    future = executor.submit(run, eq)
    while True:
        try:
            evt = eq.get(timeout=0.3)
        except queue_mod.Empty:
            if future.done():
                break
            yield _sse({"type": "keepalive"})
            continue
        if evt.get("type") == "_done":
            break
        sse = _agent_event_sse(evt)
        if sse is not None:
            yield sse
            if evt.get("type") == "error":
                return
    exc = future.exception() if future.done() else None
    if exc is not None:
        # 容量超過など種別が判る例外はカタログの文言を返す。診断情報はログにだけ残す
        logger.error("Failed to generate report", exc_info=exc)
        yield _sse_error(user_message_for(exc, default=default))
        return
    yield _sse({"type": "done"})


# GET で AI を動かすため web/middleware.py が Sec-Fetch-Site を確かめる（NFR4.13）
@router.get("/{discussion_id}/report/generate")
async def generate_report_stream(
    request: Request,
    discussion_id: str,
    template_type: str = "summary",
    custom_prompt: Optional[str] = None,
) -> Any:
    """レポートをSSEストリーミングで生成する（読める人、BR4.1）"""
    principal = _principal(request)
    if template_type not in REPORT_TEMPLATE_TYPES:
        return StreamingResponse(
            iter([_sse_error("無効なテンプレート種別です")]),
            media_type="text/event-stream",
        )
    report_manager = get_report_manager()
    if template_type == "data_driven":
        refusal = _data_agent_refusal(report_manager, principal)
        if refusal is not None:
            return refusal

    def stream_generator() -> Any:
        default = "レポートの生成に失敗しました"
        try:
            if template_type == "data_driven":

                def run(eq: Any) -> None:
                    for _ in report_manager.generate_report_streaming(
                        principal,
                        discussion_id,
                        template_type,
                        custom_prompt or None,
                        eq,
                    ):
                        pass

                yield from _stream_agent_events(run, default)
                return

            for chunk in report_manager.generate_report_streaming(
                principal, discussion_id, template_type, custom_prompt or None
            ):
                yield _sse({"type": "chunk", "content": chunk})
            yield _sse({"type": "done"})
        except Exception as e:
            logger.error("Failed to generate report", exc_info=True)
            yield _sse_error(user_message_for(e, default=default))

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{discussion_id}/report/generate-followup")
async def generate_followup_report_stream(
    request: Request,
    discussion_id: str,
    followup_prompt: str = Form(...),
    previous_report: str = Form(...),
) -> Any:
    """フォローアップ分析をPOST SSEストリーミングで生成する。

    短期記憶のセッションは議論と利用者の組からサーバーが決める（BR4.3。画面から
    送られたセッション ID は使わない）。
    """
    principal = _principal(request)
    report_manager = get_report_manager()
    refusal = _data_agent_refusal(report_manager, principal)
    if refusal is not None:
        return refusal

    def run(eq: Any) -> None:
        for _ in report_manager.generate_followup_report_streaming(
            principal, discussion_id, followup_prompt, previous_report, eq
        ):
            pass

    return StreamingResponse(
        _stream_agent_events(run, "フォローアップ分析の生成に失敗しました"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{discussion_id}/report/save")
async def save_report(
    request: Request,
    discussion_id: str,
    template_type: str = Form(...),
    content: str = Form(...),
    custom_prompt: Optional[str] = Form(None),
) -> Any:
    """プレビュー済みレポートを保存する（管理できる人、ID はサーバーが振る、BR4.2、BR4.4）"""
    from src.managers.settings_manager import SettingsManager

    principal = _principal(request)
    report_manager = get_report_manager()
    try:
        await _run(
            report_manager.save_report,
            principal,
            discussion_id,
            template_type,
            content,
            custom_prompt or None,
        )
        view = await _run(report_manager.get_discussion, principal, discussion_id)
    except CodedError as e:
        if error_kind_of(e) in (ErrorKind.NOT_FOUND, ErrorKind.FORBIDDEN):
            raise
        logger.warning("Failed to save report", exc_info=True)
        message = user_message_for(e, default="レポートの保存に失敗しました")
        return _report_preview(
            request, discussion_id, template_type, content, custom_prompt, message
        )
    except Exception as e:
        logger.error("Failed to save report", exc_info=True)
        message = user_message_for(e, default="レポートの保存に失敗しました")
        return _report_preview(
            request, discussion_id, template_type, content, custom_prompt, message
        )

    response = templates.TemplateResponse(
        request,
        "discussion/partials/reports.html",
        {
            "request": request,
            "discussion": view,
            "discussion_id": discussion_id,
            "manageable": view.manageable,
            "enable_data_driven_report": SettingsManager.is_data_agent_available(),
        },
    )
    response.headers["HX-Retarget"] = "#reports-container"
    return response


def _report_preview(
    request: Request,
    discussion_id: str,
    template_type: str,
    content: str,
    custom_prompt: Optional[str],
    save_error: str,
) -> Response:
    """The unsaved report again, with why it was not saved (the input is kept)."""
    preview = DiscussionReport.create_new(
        discussion_id,
        template_type if template_type in REPORT_TEMPLATE_TYPES else "custom",
        content,
        custom_prompt or None,
    )
    return templates.TemplateResponse(
        request,
        "discussion/partials/report_preview.html",
        {
            "request": request,
            "report": preview,
            "discussion_id": discussion_id,
            "save_error": save_error,
        },
    )


@router.post("/{discussion_id}/report/{report_id}/update-content")
async def update_report_content(
    request: Request,
    discussion_id: str,
    report_id: str,
    content: str = Form(...),
) -> Any:
    """レポート内容を更新する（管理できる人だけ）"""
    from markupsafe import escape

    try:
        await _run(
            get_report_manager().update_report_content,
            _principal(request),
            discussion_id,
            report_id,
            content,
        )
    except CodedError as e:
        if error_kind_of(e) in (ErrorKind.NOT_FOUND, ErrorKind.FORBIDDEN):
            raise
        logger.warning("Failed to update report", exc_info=True)
        # 再試行で直る失敗はトースト（画面を書き換えない）。直せる失敗は印を付けて本文を出す
        if not is_correctable(e):
            return toast_response(e)
        message = user_message_for(e, default="更新に失敗しました")
        return mark_renderable(
            HTMLResponse(
                f'<div class="text-sm text-red-600 bg-red-50 border border-red-200 rounded p-2 mt-2">{escape(message)}</div>',
                status_code=400,
            )
        )
    return HTMLResponse(
        '<div class="text-sm text-green-700 bg-green-50 border border-green-200 rounded p-2 mt-2">レポートを更新しました</div>'
    )


@router.get("/{discussion_id}/report/{report_id}")
async def get_report(
    request: Request,
    discussion_id: str,
    report_id: str,
) -> Any:
    """レポート内容を取得する（読める人だけ）"""
    principal = _principal(request)
    report_manager = get_report_manager()
    report = await _run(report_manager.get_report, principal, discussion_id, report_id)
    manageable = await _run(report_manager.can_manage, principal, discussion_id)
    return templates.TemplateResponse(
        request,
        "discussion/partials/report_content.html",
        {
            "request": request,
            "report": report,
            "discussion_id": discussion_id,
            "manageable": manageable,
        },
    )


@router.get("/{discussion_id}/report/{report_id}/export")
async def export_report(
    request: Request,
    discussion_id: str,
    report_id: str,
    format: str = "md",
) -> Any:
    """レポートをファイルエクスポートする（読める人だけ）"""
    filename, content = await _run(
        get_report_manager().export_report,
        _principal(request),
        discussion_id,
        report_id,
        format,
    )
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": _attachment(filename)},
    )


@router.delete("/{discussion_id}/report/{report_id}")
async def delete_report(
    request: Request,
    discussion_id: str,
    report_id: str,
) -> Any:
    """レポートを削除する（管理できる人だけ）"""
    from markupsafe import escape

    try:
        await _run(
            get_report_manager().delete_report,
            _principal(request),
            discussion_id,
            report_id,
        )
    except CodedError as e:
        if error_kind_of(e) in (ErrorKind.NOT_FOUND, ErrorKind.FORBIDDEN):
            raise
        logger.error("Failed to delete report", exc_info=True)
        # 再試行で直る失敗はトースト（行を消さない）。直せる失敗は印を付けて本文を出す
        if not is_correctable(e):
            return toast_response(e)
        message = user_message_for(e, default="レポートの削除に失敗しました")
        return mark_renderable(
            HTMLResponse(
                content=f"<div class='text-red-600'>{escape(message)}</div>",
                status_code=400,
            )
        )
    return HTMLResponse(content="<div>レポートを削除しました</div>")
