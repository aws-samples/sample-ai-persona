"""
議論関連のルーター
"""

import logging
import asyncio
import json
from datetime import datetime
from typing import Any, Optional, List
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.managers.persona_manager import PersonaManager
from src.managers.discussion_manager import DiscussionManager
from src.managers.agent_discussion_manager import AgentDiscussionManager
from src.managers.report_manager import ReportManager
from src.managers.file_manager import FileManager, FileUploadError
from src.models.insight_category import InsightCategory
from ._pagination import decode_cursor, encode_cursor

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

templates.env.globals["country_name"] = country_service.country_name
templates.env.globals["country_choices"] = country_service.country_choices
templates.env.globals["gender_label"] = gender_label
templates.env.globals["GENDER_LABELS"] = GENDER_LABELS

# スレッドプールエグゼキューター（同期的なAI処理を非同期で実行するため）
executor = ThreadPoolExecutor(max_workers=8)


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


def _get_participant_personas(participant_ids: List[str]) -> dict:
    """参加者IDからペルソナ情報を取得してマップを返す"""
    persona_manager = get_persona_manager()
    persona_map = {}
    for pid in participant_ids:
        try:
            persona = persona_manager.get_persona(pid)
            if persona:
                persona_map[pid] = persona
        except Exception:
            pass
    return persona_map


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
    return templates.TemplateResponse(
        request,
        "discussion/setup.html",
        {"request": request, "title": "議論設定"},
    )


@router.post("/upload-document")
async def upload_discussion_document(file: UploadFile = File(...)) -> Any:
    """議論用ドキュメントをアップロード"""
    try:
        file_manager = get_file_manager()

        # ファイル内容を読み込み
        file_content = await file.read()

        # ファイルをアップロード
        file_metadata = file_manager.upload_discussion_document(
            file_content=file_content,
            filename=file.filename,  # type: ignore[arg-type]
        )

        # JSONレスポンスを返す
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
        logger.error(f"ドキュメントアップロードエラー: {e}")
        return JSONResponse({"error": e.user_message}, status_code=400)
    except Exception as e:
        logger.error(f"ドキュメントアップロードエラー: {e}")
        return JSONResponse(
            {"error": "ドキュメントのアップロードに失敗しました"}, status_code=400
        )


@router.get("/result-partial/{discussion_id}", response_class=HTMLResponse)
async def get_discussion_result_partial(request: Request, discussion_id: str) -> Any:
    """議論結果パーシャルを取得（リアルタイム表示完了後に使用）"""
    try:
        discussion_manager = get_discussion_manager()
        discussion = discussion_manager.get_discussion(discussion_id)

        if not discussion:
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {"request": request, "error": "議論が見つかりません"},
                status_code=404,
            )

        return templates.TemplateResponse(
            request,
            "discussion/partials/discussion_result.html",
            {"request": request, "discussion": discussion},
        )
    except Exception as e:
        logger.error(f"議論結果パーシャル取得エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": "議論結果の取得に失敗しました"},
            status_code=500,
        )


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
) -> Any:
    """議論ストリーミングエンドポイント（SSE）- 簡易モード・エージェントモード両対応"""
    try:
        ids = persona_ids.split(",")
        if len(ids) < 2:
            return StreamingResponse(
                iter(
                    [
                        f"data: {json.dumps({'type': 'error', 'message': '最低2体のペルソナが必要です'}, ensure_ascii=False)}\n\n"
                    ]
                ),
                media_type="text/event-stream",
            )

        persona_manager = get_persona_manager()
        personas_raw = [persona_manager.get_persona(pid.strip()) for pid in ids]
        personas = [p for p in personas_raw if p is not None]

        if len(personas) < 2:
            return StreamingResponse(
                iter(
                    [
                        f"data: {json.dumps({'type': 'error', 'message': '有効なペルソナが2体以上必要です'}, ensure_ascii=False)}\n\n"
                    ]
                ),
                media_type="text/event-stream",
            )

        # カテゴリー情報を解析
        categories = None
        if categories_json:
            try:
                categories_data = json.loads(categories_json)
                categories = [
                    InsightCategory(name=c["name"], description=c["description"])
                    for c in categories_data
                ]
            except Exception as e:
                logger.warning(f"カテゴリー解析エラー: {e}")

        # ドキュメントIDを解析
        doc_ids = None
        if document_ids:
            doc_ids = [did.strip() for did in document_ids.split(",") if did.strip()]

        # 参加者情報を最初に送信
        participants_data = {
            "type": "participants",
            "personas": [{"id": p.id, "name": p.name} for p in personas],  # type: ignore[union-attr]
            "mode": mode,
        }

        async def event_generator() -> Any:
            yield f"data: {json.dumps(participants_data, ensure_ascii=False)}\n\n"

            queue: asyncio.Queue[Any] = asyncio.Queue()

            def run_sync_generator() -> Any:
                try:
                    if mode == "agent":
                        manager = get_agent_discussion_manager()
                        gen = manager.run_agent_discussion_streaming(
                            personas=personas,
                            topic=topic,
                            rounds=rounds,
                            additional_instructions=additional_instructions,
                            enable_memory=enable_memory,
                            memory_mode=memory_mode,
                            enable_dataset=enable_dataset,
                            enable_kb=enable_kb,
                            categories=categories,
                            document_ids=doc_ids,
                        )
                    else:
                        dm = get_discussion_manager()
                        gen = dm.run_classic_discussion_streaming(
                            personas=personas,
                            topic=topic,
                            categories=categories,
                            document_ids=doc_ids,
                        )
                    for event in gen:
                        asyncio.run_coroutine_threadsafe(queue.put(event), loop)
                except Exception as e:
                    logger.error(f"ストリーミング処理エラー: {e}")
                    error_event = f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
                    asyncio.run_coroutine_threadsafe(queue.put(error_event), loop)
                finally:
                    asyncio.run_coroutine_threadsafe(queue.put(None), loop)

            loop = asyncio.get_event_loop()
            executor.submit(run_sync_generator)

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    if event is None:
                        break
                    yield event
                except asyncio.TimeoutError:
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
    except Exception as e:
        logger.error(f"ストリーミング議論エラー: {e}")
        return StreamingResponse(
            iter(
                [
                    f"data: {json.dumps({'type': 'error', 'message': '議論の実行中にエラーが発生しました'}, ensure_ascii=False)}\n\n"
                ]
            ),
            media_type="text/event-stream",
        )


@router.get("/results", response_class=HTMLResponse)
async def discussion_results_page(
    request: Request,
    mode: Optional[str] = None,
    search: Optional[str] = None,
    sort: Optional[str] = "newest",
    cursor: Optional[str] = None,
    append: bool = False,
) -> Any:
    """議論結果一覧ページ（インタビューセッションを含む）"""
    try:
        discussion_manager = get_discussion_manager()
        search_query = (search or "").strip()

        if search_query:
            # 検索時は全件 scan フォールバック + Python フィルタ
            discussions, _ = discussion_manager.get_discussion_history(search_all=True)
            search_lower = search_query.lower()
            discussions = [d for d in discussions if search_lower in d.topic.lower()]
            # mode フィルタも Python 側で適用
            if mode and mode in ["agent", "classic", "interview"]:
                discussions = [d for d in discussions if d.mode == mode]
            # ソート
            discussions = sorted(
                discussions,
                key=lambda d: d.created_at or datetime.min,
                reverse=(sort != "oldest"),
            )[:100]
            next_cursor_encoded: Optional[str] = None
        else:
            # GSI Query（mode 指定時は ModeIndex）
            discussions, next_cursor = discussion_manager.get_discussion_history(
                limit=21,
                cursor=decode_cursor(cursor),
                mode=mode if mode in ("agent", "classic", "interview") else None,
                sort_ascending=(sort == "oldest"),
            )
            next_cursor_encoded = encode_cursor(next_cursor)

        # 参加ペルソナ情報を取得
        all_participant_ids = set()
        for d in discussions:
            if d.participants:
                all_participant_ids.update(d.participants)
        participant_personas = (
            _get_participant_personas(list(all_participant_ids))
            if all_participant_ids
            else {}
        )
    except Exception as e:
        logger.error(f"議論一覧取得エラー: {e}")
        discussions = []
        participant_personas = {}
        next_cursor_encoded = None

    ctx = {
        "request": request,
        "discussions": discussions,
        "participant_personas": participant_personas,
        "current_mode": mode,
        "current_search": search,
        "current_sort": sort,
        "next_cursor": next_cursor_encoded,
        "is_append": append,
    }

    # htmxリクエストの場合はパーシャルを返す
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "discussion/partials/discussion_list.html",
            ctx,
        )

    return templates.TemplateResponse(
        request,
        "discussion/results.html",
        {**ctx, "title": "議論結果"},
    )


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
    try:
        if mode == "interview":
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {
                    "request": request,
                    "error": "インタビューモードは別のエンドポイントで処理されます",
                },
                status_code=400,
            )

        if len(persona_ids) < 2:
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {"request": request, "error": "議論には最低2体のペルソナが必要です"},
                status_code=400,
            )

        persona_manager = get_persona_manager()
        personas_raw = [persona_manager.get_persona(pid) for pid in persona_ids]
        personas = [p for p in personas_raw if p is not None]

        if len(personas) < 2:
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {"request": request, "error": "有効なペルソナが2体以上必要です"},
                status_code=400,
            )

        form_data = await request.form()
        categories = _parse_categories_from_form(form_data)

        loop = asyncio.get_event_loop()

        if mode == "agent":

            def _run_agent() -> Any:
                manager = get_agent_discussion_manager()
                return manager.run_agent_discussion_full(
                    personas=personas,
                    topic=topic,
                    rounds=rounds,
                    additional_instructions=additional_instructions,
                    enable_memory=enable_memory,
                    memory_mode=memory_mode,
                    categories=categories,
                    document_ids=document_ids,
                )

            discussion = await loop.run_in_executor(executor, _run_agent)
        else:

            def _run_classic() -> Any:
                manager = get_discussion_manager()
                return manager.run_classic_discussion(
                    personas=personas,
                    topic=topic,
                    categories=categories,
                    document_ids=document_ids,
                )

            discussion = await loop.run_in_executor(executor, _run_classic)

        return templates.TemplateResponse(
            request,
            "discussion/partials/discussion_result.html",
            {"request": request, "discussion": discussion},
        )
    except Exception as e:
        logger.error(f"議論開始エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {
                "request": request,
                "error": f"議論の開始中にエラーが発生しました: {str(e)}",
            },
            status_code=500,
        )


@router.get("/{discussion_id}", response_class=HTMLResponse)
async def get_discussion_detail(request: Request, discussion_id: str) -> Any:
    """議論詳細ページ（インタビューセッションを含む）"""
    try:
        discussion_manager = get_discussion_manager()
        discussion = discussion_manager.get_discussion(discussion_id)

        if not discussion:
            raise HTTPException(status_code=404, detail="議論が見つかりません")

        # 参加ペルソナの詳細情報を取得
        participant_personas = _get_participant_personas(discussion.participants)

        # デフォルトカテゴリーを取得（再生成モーダル用）
        default_categories = discussion_manager.get_default_categories()

        # カスタムカテゴリーを取得（agent_configから）
        custom_categories = None
        if discussion.agent_config and "insight_categories" in discussion.agent_config:
            custom_categories = discussion.agent_config["insight_categories"]

        # ドキュメントの署名付きURLを生成
        document_urls = {}
        if discussion.documents:
            disc_manager = get_discussion_manager()
            document_urls = disc_manager.get_document_presigned_urls(
                discussion.documents
            )

        # インタビューセッションの場合はタイトルを調整
        if discussion.mode == "interview":
            title = "インタビューセッション"
        else:
            title = f"議論: {discussion.topic}"

        from src.managers.settings_manager import SettingsManager

        return templates.TemplateResponse(
            request,
            "discussion/detail.html",
            {
                "request": request,
                "title": title,
                "discussion": discussion,
                "participant_personas": participant_personas,
                "insights": discussion.insights,
                "discussion_id": discussion_id,
                "default_categories": default_categories,
                "custom_categories": custom_categories,
                "document_urls": document_urls,
                "enable_data_driven_report": SettingsManager().is_data_agent_available(),
            },
        )
    except HTTPException:
        raise


@router.post("/{discussion_id}/regenerate-insights", response_class=HTMLResponse)
async def regenerate_insights(request: Request, discussion_id: str) -> Any:
    """インサイト再生成エンドポイント（htmx対応）"""
    try:
        form_data = await request.form()
        categories = _parse_categories_from_form(form_data)

        discussion_manager = get_discussion_manager()

        loop = asyncio.get_event_loop()
        new_insights = await loop.run_in_executor(
            executor,
            discussion_manager.regenerate_insights,
            discussion_id,
            categories,
        )

        default_categories = discussion_manager.get_default_categories()

        return templates.TemplateResponse(
            request,
            "discussion/partials/insights.html",
            {
                "request": request,
                "insights": new_insights,
                "discussion_id": discussion_id,
                "default_categories": default_categories,
            },
        )
    except Exception as e:
        logger.error(f"インサイト再生成エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {
                "request": request,
                "error": f"インサイトの再生成中にエラーが発生しました: {str(e)}",
            },
            status_code=500,
        )


@router.delete("/{discussion_id}", response_class=HTMLResponse)
async def delete_discussion(request: Request, discussion_id: str) -> Any:
    """議論削除処理（htmx対応）"""
    try:
        discussion_manager = get_discussion_manager()
        success = discussion_manager.delete_discussion(discussion_id)

        if success:
            referer = request.headers.get("hx-current-url", "")
            if f"/discussion/{discussion_id}" in referer:
                return HTMLResponse(
                    content="",
                    headers={"HX-Redirect": "/discussion/results"},
                )
            return templates.TemplateResponse(
                request,
                "partials/success.html",
                {"request": request, "message": "議論を削除しました"},
            )
        else:
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {"request": request, "error": "議論の削除に失敗しました"},
                status_code=400,
            )
    except Exception as e:
        logger.error(f"議論削除エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": f"削除エラー: {str(e)}"},
            status_code=500,
        )


@router.get("/insights/{discussion_id}", response_class=HTMLResponse)
async def get_discussion_insights(request: Request, discussion_id: str) -> Any:
    """議論インサイト取得（htmx対応）"""
    try:
        discussion_manager = get_discussion_manager()
        discussion = discussion_manager.get_discussion(discussion_id)

        if not discussion:
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {"request": request, "error": "議論が見つかりません"},
                status_code=404,
            )

        return templates.TemplateResponse(
            request,
            "discussion/partials/insights.html",
            {"request": request, "insights": discussion.insights},
        )
    except Exception as e:
        logger.error(f"インサイト取得エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": "インサイトの取得に失敗しました"},
            status_code=500,
        )


# =============================================================================
# レポート関連エンドポイント
# =============================================================================


@router.get("/{discussion_id}/report/generate")
async def generate_report_stream(
    request: Request,
    discussion_id: str,
    template_type: str = "summary",
    custom_prompt: Optional[str] = None,
) -> Any:
    """レポートをSSEストリーミングで生成する"""
    if template_type not in ("summary", "review", "custom", "data_driven"):

        def error_gen() -> Any:
            yield f"data: {json.dumps({'type': 'error', 'message': '無効なテンプレート種別です'}, ensure_ascii=False)}\n\n"

        return StreamingResponse(error_gen(), media_type="text/event-stream")

    def stream_generator() -> Any:
        try:
            report_manager = get_report_manager()

            if template_type == "data_driven":
                import queue as queue_mod

                eq: queue_mod.Queue = queue_mod.Queue()

                def _run() -> None:
                    for _ in report_manager.generate_report_streaming(
                        discussion_id=discussion_id,
                        template_type=template_type,
                        custom_prompt=custom_prompt or None,
                        event_queue=eq,
                    ):
                        pass

                future = executor.submit(_run)
                full_content = ""

                while True:
                    try:
                        evt = eq.get(timeout=0.3)
                    except queue_mod.Empty:
                        if future.done():
                            break
                        yield f"data: {json.dumps({'type': 'keepalive'}, ensure_ascii=False)}\n\n"
                        continue

                    evt_type = evt.get("type", "")
                    content = evt.get("content", "")

                    if evt_type == "_done":
                        break
                    elif evt_type == "session_id":
                        yield f"data: {json.dumps({'type': 'session_id', 'session_id': evt.get('session_id', '')}, ensure_ascii=False)}\n\n"
                    elif evt_type == "tool_call":
                        yield f"event: thinking\ndata: {json.dumps({'type': 'tool_call', 'content': content, 'detail': evt.get('detail', '')}, ensure_ascii=False)}\n\n"
                    elif evt_type == "tool_result" and content:
                        yield f"event: thinking\ndata: {json.dumps({'type': 'tool_result', 'content': content}, ensure_ascii=False)}\n\n"
                    elif evt_type == "csv_url":
                        url = evt.get("url", "")
                        if url:
                            yield f"data: {json.dumps({'type': 'csv_url', 'url': url}, ensure_ascii=False)}\n\n"
                    elif evt_type == "thinking" and content:
                        full_content += content
                        yield f"data: {json.dumps({'type': 'chunk', 'content': content}, ensure_ascii=False)}\n\n"
                    elif evt_type == "result":
                        full_content = content
                        yield f"data: {json.dumps({'type': 'chunk', 'content': content}, ensure_ascii=False)}\n\n"
                    elif evt_type == "error":
                        yield f"data: {json.dumps({'type': 'error', 'message': content}, ensure_ascii=False)}\n\n"
                        return

                if future.done() and future.exception():
                    logger.error(f"レポート生成エラー: {future.exception()}")
                    yield f"data: {json.dumps({'type': 'error', 'message': 'レポートの生成に失敗しました'}, ensure_ascii=False)}\n\n"
                    return

                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                return

            for chunk in report_manager.generate_report_streaming(
                discussion_id=discussion_id,
                template_type=template_type,
                custom_prompt=custom_prompt or None,
            ):
                data = json.dumps(
                    {"type": "chunk", "content": chunk}, ensure_ascii=False
                )
                yield f"data: {data}\n\n"

            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"レポート生成エラー: {e}")
            data = json.dumps(
                {"type": "error", "message": "レポートの生成に失敗しました"},
                ensure_ascii=False,
            )
            yield f"data: {data}\n\n"

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
    session_id: Optional[str] = Form(None),
) -> Any:
    """フォローアップ分析をPOST SSEストリーミングで生成する"""

    def stream_generator() -> Any:
        try:
            report_manager = get_report_manager()
            import queue as queue_mod

            eq: queue_mod.Queue = queue_mod.Queue()

            def _run() -> None:
                for _ in report_manager.generate_followup_report_streaming(
                    discussion_id=discussion_id,
                    followup_prompt=followup_prompt,
                    previous_report=previous_report,
                    event_queue=eq,
                    session_id=session_id or None,
                ):
                    pass

            future = executor.submit(_run)

            while True:
                try:
                    evt = eq.get(timeout=0.3)
                except queue_mod.Empty:
                    if future.done():
                        break
                    yield f"data: {json.dumps({'type': 'keepalive'}, ensure_ascii=False)}\n\n"
                    continue

                evt_type = evt.get("type", "")
                content = evt.get("content", "")

                if evt_type == "_done":
                    break
                elif evt_type == "session_id":
                    yield f"data: {json.dumps({'type': 'session_id', 'session_id': evt.get('session_id', '')}, ensure_ascii=False)}\n\n"
                elif evt_type == "tool_call":
                    yield f"event: thinking\ndata: {json.dumps({'type': 'tool_call', 'content': content, 'detail': evt.get('detail', '')}, ensure_ascii=False)}\n\n"
                elif evt_type == "tool_result" and content:
                    yield f"event: thinking\ndata: {json.dumps({'type': 'tool_result', 'content': content}, ensure_ascii=False)}\n\n"
                elif evt_type == "csv_url":
                    url = evt.get("url", "")
                    if url:
                        yield f"data: {json.dumps({'type': 'csv_url', 'url': url}, ensure_ascii=False)}\n\n"
                elif evt_type == "thinking" and content:
                    yield f"data: {json.dumps({'type': 'chunk', 'content': content}, ensure_ascii=False)}\n\n"
                elif evt_type == "result":
                    yield f"data: {json.dumps({'type': 'chunk', 'content': content}, ensure_ascii=False)}\n\n"
                elif evt_type == "error":
                    yield f"data: {json.dumps({'type': 'error', 'message': content}, ensure_ascii=False)}\n\n"
                    return

            if future.done() and future.exception():
                logger.error(f"フォローアップレポート生成エラー: {future.exception()}")
                yield f"data: {json.dumps({'type': 'error', 'message': 'フォローアップ分析の生成に失敗しました'}, ensure_ascii=False)}\n\n"
                return

            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"フォローアップレポート生成エラー: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': 'フォローアップ分析の生成に失敗しました'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{discussion_id}/report/save")
async def save_report(
    request: Request,
    discussion_id: str,
    report_id: str = Form(...),
    template_type: str = Form(...),
    content: str = Form(...),
    custom_prompt: Optional[str] = Form(None),
) -> Any:
    """プレビュー済みレポートをDBに保存する"""
    try:
        from src.models.discussion_report import DiscussionReport

        report = DiscussionReport(
            id=report_id,
            template_type=template_type,
            content=content,
            created_at=datetime.now(),
            custom_prompt=custom_prompt or None,
        )

        report_manager = get_report_manager()
        report_manager.save_report(discussion_id=discussion_id, report=report)

        # 保存後、reportsセクション全体を再描画
        from src.managers.settings_manager import SettingsManager

        discussion = report_manager.get_discussion(discussion_id)
        response = templates.TemplateResponse(
            request,
            "discussion/partials/reports.html",
            {
                "request": request,
                "discussion": discussion,
                "discussion_id": discussion_id,
                "enable_data_driven_report": SettingsManager().is_data_agent_available(),
            },
        )
        response.headers["HX-Retarget"] = "#reports-container"
        return response
    except Exception as e:
        logger.error(f"レポート保存エラー: {e}")
        from src.models.discussion_report import DiscussionReport as DR

        report = DR(
            id=report_id,
            template_type=template_type,
            content=content,
            created_at=datetime.now(),
            custom_prompt=custom_prompt or None,
        )
        return templates.TemplateResponse(
            request,
            "discussion/partials/report_preview.html",
            {
                "request": request,
                "report": report,
                "discussion_id": discussion_id,
                "save_error": str(e),
            },
        )


@router.post("/{discussion_id}/report/{report_id}/update-content")
async def update_report_content(
    request: Request,
    discussion_id: str,
    report_id: str,
    content: str = Form(...),
) -> Any:
    """レポート内容を更新する"""
    try:
        report_manager = get_report_manager()
        report_manager.update_report_content(
            discussion_id=discussion_id,
            report_id=report_id,
            content=content,
        )
        return HTMLResponse(
            '<div class="text-sm text-green-700 bg-green-50 border border-green-200 rounded p-2 mt-2">レポートを更新しました</div>'
        )
    except Exception as e:
        logger.error(f"レポート更新エラー: {e}")
        return HTMLResponse(
            '<div class="text-sm text-red-600 bg-red-50 border border-red-200 rounded p-2 mt-2">更新に失敗しました</div>',
            status_code=400,
        )


@router.get("/{discussion_id}/report/{report_id}")
async def get_report(
    request: Request,
    discussion_id: str,
    report_id: str,
) -> Any:
    """レポート内容を取得する"""
    try:
        report_manager = get_report_manager()
        discussion = report_manager.get_discussion(discussion_id)
        if not discussion:
            return HTMLResponse(content="議論が見つかりません", status_code=404)

        report = next((r for r in discussion.reports if r.id == report_id), None)
        if not report:
            return HTMLResponse(content="レポートが見つかりません", status_code=404)

        return templates.TemplateResponse(
            request,
            "discussion/partials/report_content.html",
            {"request": request, "report": report, "discussion_id": discussion_id},
        )
    except Exception as e:
        logger.error(f"レポート取得エラー: {e}")
        return HTMLResponse(content="レポートの取得に失敗しました", status_code=500)


@router.get("/{discussion_id}/report/{report_id}/export")
async def export_report(
    discussion_id: str,
    report_id: str,
    format: str = "md",
) -> Any:
    """レポートをファイルエクスポートする"""
    try:
        report_manager = get_report_manager()
        discussion = report_manager.get_discussion(discussion_id)
        if not discussion:
            return HTMLResponse(content="議論が見つかりません", status_code=404)

        report = next((r for r in discussion.reports if r.id == report_id), None)
        if not report:
            return HTMLResponse(content="レポートが見つかりません", status_code=404)

        content = report.content
        if format == "txt":
            # Markdown記法を除去
            import re

            content = re.sub(r"#{1,6}\s*", "", content)
            content = re.sub(r"\*{1,2}(.*?)\*{1,2}", r"\1", content)
            content = re.sub(r"_{1,2}(.*?)_{1,2}", r"\1", content)

        timestamp = report.created_at.strftime("%Y%m%d_%H%M%S")
        filename = f"report_{report.template_type}_{timestamp}.{format}"

        from fastapi.responses import Response

        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error(f"レポートエクスポートエラー: {e}")
        return HTMLResponse(content="エクスポートに失敗しました", status_code=500)


@router.delete("/{discussion_id}/report/{report_id}")
async def delete_report(
    request: Request,
    discussion_id: str,
    report_id: str,
) -> Any:
    """レポートを削除する"""
    try:
        report_manager = get_report_manager()
        report_manager.delete_report(
            discussion_id=discussion_id,
            report_id=report_id,
        )
        return HTMLResponse(content="<div>レポートを削除しました</div>")
    except Exception as e:
        logger.error(f"レポート削除エラー: {e}")
        from markupsafe import escape

        return HTMLResponse(
            content=f"<div class='text-red-600'>{escape(str(e))}</div>",
            status_code=400,
        )
