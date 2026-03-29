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
from src.managers.file_manager import FileManager
from src.services.service_factory import service_factory
from src.models.discussion import Discussion
from src.models.insight_category import InsightCategory

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


from web.sanitize import render_markdown  # noqa: E402

templates.env.filters["markdown"] = render_markdown

# スレッドプールエグゼキューター（同期的なAI処理を非同期で実行するため）
executor = ThreadPoolExecutor(max_workers=8)


# シングルトンマネージャーインスタンス（モジュールレベルで共有）
_persona_manager = None
_discussion_manager = None
_agent_discussion_manager = None
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
        # S3サービスを取得（設定されている場合）
        s3_service = service_factory.get_s3_service()
        _file_manager = FileManager(s3_service=s3_service)
    return _file_manager


def get_agent_discussion_manager() -> AgentDiscussionManager:
    """AgentDiscussionManagerのシングルトンインスタンスを取得"""
    global _agent_discussion_manager
    if _agent_discussion_manager is None:
        _agent_discussion_manager = AgentDiscussionManager()
    return _agent_discussion_manager


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
    """議論設定ページ"""
    try:
        persona_manager = get_persona_manager()
        personas = persona_manager.get_all_personas()
    except Exception as e:
        logger.error(f"ペルソナ一覧取得エラー: {e}")
        personas = []

    return templates.TemplateResponse(
        "discussion/setup.html",
        {"request": request, "title": "議論設定", "personas": personas},
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
            file_content=file_content, filename=file.filename  # type: ignore[arg-type]
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

    except Exception as e:
        logger.error(f"ドキュメントアップロードエラー: {e}")
        return JSONResponse({"error": str(e)}, status_code=400)


@router.get("/result-partial/{discussion_id}", response_class=HTMLResponse)
async def get_discussion_result_partial(request: Request, discussion_id: str) -> Any:
    """議論結果パーシャルを取得（リアルタイム表示完了後に使用）"""
    try:
        discussion_manager = get_discussion_manager()
        discussion = discussion_manager.get_discussion(discussion_id)

        if not discussion:
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "議論が見つかりません"},
                status_code=404,
            )

        return templates.TemplateResponse(
            "discussion/partials/discussion_result.html",
            {"request": request, "discussion": discussion},
        )
    except Exception as e:
        logger.error(f"議論結果パーシャル取得エラー: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": "議論結果の取得に失敗しました"},
            status_code=500,
        )


def _stream_discussion_sync(
    topic: str,
    personas: list,
    mode: str = "traditional",
    rounds: int = 3,
    additional_instructions: str = "",
    enable_memory: bool = False,
    memory_mode: str = "full",
    enable_dataset: bool = False,
    enable_kb: bool = False,
    categories: Optional[List[InsightCategory]] = None,
    document_ids: Optional[List[str]] = None,
) -> Any:
    """同期的なストリーミング議論処理（簡易モード・エージェントモード両対応）"""

    if mode == "agent":
        # エージェントモードのストリーミング
        agent_manager = get_agent_discussion_manager()

        # ペルソナエージェントを作成（メモリ設定を渡す）
        system_prompts: dict[str, str] = {}

        # 議論IDを事前に生成（メモリのsession_idとして使用）
        # Note: Discussion is imported at module level
        temp_discussion = Discussion.create_new(
            topic=topic, participants=[p.id for p in personas], mode="agent"
        )
        session_id = temp_discussion.id

        persona_agents = agent_manager.create_persona_agents(
            personas,
            system_prompts,
            enable_memory=enable_memory,
            session_id=session_id,
            memory_mode=memory_mode,
            enable_dataset=enable_dataset,
            enable_kb=enable_kb,
        )

        # ファシリテーターエージェントを作成
        facilitator = agent_manager.create_facilitator_agent(
            rounds, additional_instructions
        )

        discussion = None
        message_count = 0

        # ストリーミング議論を実行（メモリ設定とドキュメントIDを渡す）
        for event_type, data in agent_manager.start_agent_discussion_streaming(
            personas=personas,
            topic=topic,
            persona_agents=persona_agents,
            facilitator=facilitator,
            enable_memory=enable_memory,
            document_ids=document_ids,
        ):
            if event_type == "message":
                message_count += 1
                msg_data = {
                    "type": "message",
                    "persona_id": data.persona_id,
                    "persona_name": data.persona_name,
                    "content": data.content,
                    "content_html": render_markdown(data.content),
                    "message_type": data.message_type,
                }
                yield f"data: {json.dumps(msg_data, ensure_ascii=False)}\n\n"
            elif event_type == "complete":
                discussion = data

        # インサイト生成と保存（カテゴリーを渡す）
        if discussion:
            try:
                insight_manager = get_discussion_manager()
                insights = insight_manager.generate_insights(
                    discussion, categories=categories
                )
                for insight in insights:
                    discussion = discussion.add_insight(insight)
                # カテゴリーを保存
                if categories:
                    discussion = insight_manager._save_categories_to_config(
                        discussion, categories
                    )
                agent_manager.save_agent_discussion(discussion)
            except Exception as e:
                logger.warning(f"インサイト生成に失敗: {e}")
                agent_manager.save_agent_discussion(discussion)

            # 完了イベントを送信
            complete_data = {
                "type": "complete",
                "discussion_id": discussion.id,
                "message_count": message_count,
                "insight_count": len(discussion.insights) if discussion.insights else 0,
            }
            yield f"data: {json.dumps(complete_data, ensure_ascii=False)}\n\n"
    else:
        # 簡易モードのストリーミング
        ai_service = service_factory.get_ai_service()

        # ドキュメントIDからドキュメントデータを読み込み
        documents_data = None
        documents_metadata = None
        if document_ids:
            manager = get_discussion_manager()
            documents_data, documents_metadata = manager._load_documents(document_ids)

        messages = []

        for message in ai_service.facilitate_discussion_streaming(
            personas, topic, documents=documents_data
        ):
            messages.append(message)
            msg_data = {
                "type": "message",
                "persona_id": message.persona_id,
                "persona_name": message.persona_name,
                "content": message.content,
                "content_html": render_markdown(message.content),
            }
            yield f"data: {json.dumps(msg_data, ensure_ascii=False)}\n\n"

        # 議論完了後、Discussionオブジェクトを作成して保存
        discussion = Discussion.create_new(
            topic=topic,
            participants=[p.id for p in personas],
            documents=documents_metadata,
        )
        for msg in messages:
            discussion = discussion.add_message(msg)

        # インサイト生成と保存（カテゴリーを渡す）
        try:
            manager = get_discussion_manager()
            insights = manager.generate_insights(discussion, categories=categories)
            for insight in insights:
                discussion = discussion.add_insight(insight)
            # カテゴリーを保存
            if categories:
                discussion = manager._save_categories_to_config(discussion, categories)
            manager.save_discussion(discussion)
        except Exception as e:
            logger.warning(f"インサイト生成に失敗: {e}")
            manager = get_discussion_manager()
            manager.save_discussion(discussion)

        # 完了イベントを送信
        complete_data = {
            "type": "complete",
            "discussion_id": discussion.id,
            "message_count": len(messages),
            "insight_count": len(discussion.insights) if discussion.insights else 0,
        }
        yield f"data: {json.dumps(complete_data, ensure_ascii=False)}\n\n"


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
        personas = [persona_manager.get_persona(pid.strip()) for pid in ids]
        personas = [p for p in personas if p is not None]

        if len(personas) < 2:
            return StreamingResponse(
                iter(
                    [
                        f"data: {json.dumps({'type': 'error', 'message': '有効なペルソナが2体以上必要です'}, ensure_ascii=False)}\n\n"
                    ]
                ),
                media_type="text/event-stream",
            )

        # memory_modeの検証
        valid_memory_modes = ["full", "retrieve_only", "disabled"]
        if memory_mode not in valid_memory_modes:
            memory_mode = "full"  # デフォルトにフォールバック

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

            # キューを使って同期ジェネレータからのイベントを非同期で受け取る
            queue: asyncio.Queue[Any] = asyncio.Queue()

            def run_sync_generator() -> Any:
                """同期ジェネレータを実行してキューにイベントを追加"""
                try:
                    for event in _stream_discussion_sync(
                        topic,
                        personas,
                        mode,
                        rounds,
                        additional_instructions,
                        enable_memory,
                        memory_mode,
                        enable_dataset,
                        enable_kb,
                        categories,
                        doc_ids,
                    ):
                        # メインスレッドのイベントループにキュー追加をスケジュール
                        asyncio.run_coroutine_threadsafe(queue.put(event), loop)
                except Exception as e:
                    logger.error(f"ストリーミング処理エラー: {e}")
                    error_event = f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
                    asyncio.run_coroutine_threadsafe(queue.put(error_event), loop)
                finally:
                    # 終了シグナルを送信
                    asyncio.run_coroutine_threadsafe(queue.put(None), loop)

            loop = asyncio.get_event_loop()

            # 別スレッドで同期ジェネレータを実行
            executor.submit(run_sync_generator)

            # キューからイベントを取り出して順次yield
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event

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
                    f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
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
) -> Any:
    """議論結果一覧ページ（インタビューセッションを含む）"""
    try:
        discussion_manager = get_discussion_manager()

        # 全ての議論（従来モード、エージェントモード、インタビューモード）を取得
        discussions = discussion_manager.get_discussion_history()

        # モードでフィルタ（従来モードはDBに"classic"として保存されている）
        if mode and mode in ["agent", "classic", "interview"]:
            discussions = [d for d in discussions if d.mode == mode]

        # トピックで検索
        if search and search.strip():
            search_lower = search.strip().lower()
            discussions = [d for d in discussions if search_lower in d.topic.lower()]

        # 作成日でソート
        if sort == "oldest":
            discussions = sorted(
                discussions, key=lambda d: d.created_at or datetime.min
            )
        else:  # newest (default)
            discussions = sorted(
                discussions, key=lambda d: d.created_at or datetime.min, reverse=True
            )

        # 全議論の参加ペルソナ情報を取得
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

    # htmxリクエストの場合はパーシャルを返す
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "discussion/partials/discussion_list.html",
            {
                "request": request,
                "discussions": discussions,
                "participant_personas": participant_personas,
                "current_mode": mode,
                "current_search": search,
                "current_sort": sort,
            },
        )

    return templates.TemplateResponse(
        "discussion/results.html",
        {
            "request": request,
            "title": "議論結果",
            "discussions": discussions,
            "participant_personas": participant_personas,
            "current_mode": mode,
            "current_search": search,
            "current_sort": sort,
        },
    )


def _start_discussion_sync(
    topic: str,
    personas: list,
    mode: str,
    rounds: int,
    additional_instructions: str = "",
    enable_memory: bool = False,
    memory_mode: str = "full",
    categories: Optional[List[InsightCategory]] = None,
    document_ids: Optional[List[str]] = None,
) -> Any:
    """同期的な議論開始処理（スレッドプールで実行）"""
    if mode == "agent":
        # エージェントモードの場合
        agent_manager = get_agent_discussion_manager()

        # 議論IDを事前に生成（メモリのsession_idとして使用）
        # Note: Discussion is imported at module level
        temp_discussion = Discussion.create_new(
            topic=topic, participants=[p.id for p in personas], mode="agent"
        )
        session_id = temp_discussion.id

        # ペルソナエージェントを作成（メモリ設定を渡す）
        system_prompts: dict[str, str] = {}  # デフォルトのシステムプロンプトを使用
        persona_agents = agent_manager.create_persona_agents(
            personas,
            system_prompts,
            enable_memory=enable_memory,
            session_id=session_id,
            memory_mode=memory_mode,
        )

        # ファシリテーターエージェントを作成
        facilitator = agent_manager.create_facilitator_agent(
            rounds, additional_instructions
        )

        # 議論を開始（メモリ設定とドキュメントIDを渡す）
        discussion = agent_manager.start_agent_discussion(
            personas=personas,
            topic=topic,
            persona_agents=persona_agents,
            facilitator=facilitator,
            enable_memory=enable_memory,
            document_ids=document_ids,
        )

        # インサイトを生成（DiscussionManagerを使用、カテゴリーを渡す）
        try:
            insight_manager = get_discussion_manager()
            insights = insight_manager.generate_insights(
                discussion, categories=categories
            )

            # インサイトを議論オブジェクトに追加
            for insight in insights:
                discussion = discussion.add_insight(insight)

            # カテゴリーを議論のagent_configに保存
            if categories:
                discussion = insight_manager._save_categories_to_config(
                    discussion, categories
                )

            logger.info(
                f"エージェントモード議論のインサイトを{len(insights)}件生成しました"
            )
        except Exception as e:
            logger.warning(f"エージェントモードのインサイト生成に失敗しました: {e}")

        # 議論をデータベースに保存
        agent_manager.save_agent_discussion(discussion)

        return discussion
    else:
        # 従来モードの場合
        manager = get_discussion_manager()
        discussion = manager.start_discussion(
            topic=topic, personas=personas, document_ids=document_ids
        )

        logger.info(
            f"Discussion created with {len(discussion.documents) if discussion.documents else 0} documents"
        )

        # 議論をデータベースに保存
        try:
            # インサイトを生成して保存（カテゴリーを渡す）
            insights = manager.generate_insights(discussion, categories=categories)

            # インサイトを議論オブジェクトに追加
            for insight in insights:
                discussion = discussion.add_insight(insight)

            # カテゴリーを議論のagent_configに保存
            if categories:
                discussion = manager._save_categories_to_config(discussion, categories)

            # 議論を保存
            logger.info(f"Saving discussion with documents: {discussion.documents}")
            manager.save_discussion(discussion)
        except Exception as e:
            logger.warning(
                f"インサイト生成または保存に失敗しましたが、議論は保存します: {e}"
            )
            # インサイトなしで議論を保存
            manager.save_discussion(discussion)

        return discussion


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
        logger.info(f"start_discussion called with document_ids: {document_ids}")

        # インタビューモードの場合はエラーを返す（インタビューは別のエンドポイントで処理）
        if mode == "interview":
            return templates.TemplateResponse(
                "partials/error.html",
                {
                    "request": request,
                    "error": "インタビューモードは別のエンドポイントで処理されます",
                },
                status_code=400,
            )

        if len(persona_ids) < 2:
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "議論には最低2体のペルソナが必要です"},
                status_code=400,
            )

        persona_manager = get_persona_manager()
        personas = [persona_manager.get_persona(pid) for pid in persona_ids]
        personas = [p for p in personas if p is not None]

        if len(personas) < 2:
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "有効なペルソナが2体以上必要です"},
                status_code=400,
            )

        # memory_modeの検証
        valid_memory_modes = ["full", "retrieve_only", "disabled"]
        if memory_mode not in valid_memory_modes:
            memory_mode = "full"  # デフォルトにフォールバック

        # カテゴリー情報を取得
        form_data = await request.form()
        categories = _parse_categories_from_form(form_data)

        # 同期的なAI処理をスレッドプールで非同期実行
        loop = asyncio.get_event_loop()
        discussion = await loop.run_in_executor(
            executor,
            _start_discussion_sync,
            topic,
            personas,
            mode,
            rounds,
            additional_instructions,
            enable_memory,
            memory_mode,
            categories,
            document_ids,
        )

        return templates.TemplateResponse(
            "discussion/partials/discussion_result.html",
            {"request": request, "discussion": discussion},
        )
    except Exception as e:
        logger.error(f"議論開始エラー: {e}")
        return templates.TemplateResponse(
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
        from src.config import Config

        config = Config()
        default_categories = [
            cat.to_dict() for cat in config.get_default_insight_categories()
        ]

        # カスタムカテゴリーを取得（agent_configから）
        custom_categories = None
        if discussion.agent_config and "insight_categories" in discussion.agent_config:
            custom_categories = discussion.agent_config["insight_categories"]

        # ドキュメントの署名付きURLを生成
        document_urls = {}
        if discussion.documents and config.S3_BUCKET_NAME:
            from src.services.s3_service import S3Service

            try:
                s3_service = S3Service(
                    bucket_name=config.S3_BUCKET_NAME, region_name=config.AWS_REGION
                )
                for doc in discussion.documents:
                    file_path = doc.get("file_path")
                    if file_path and file_path.startswith("s3://"):
                        try:
                            presigned_url = s3_service.generate_presigned_url(file_path)
                            document_urls[doc.get("id", file_path)] = presigned_url
                        except Exception as e:
                            logger.warning(
                                f"Failed to generate presigned URL for {file_path}: {e}"
                            )
            except Exception as e:
                logger.warning(f"Failed to initialize S3 service: {e}")

        # インタビューセッションの場合はタイトルを調整
        if discussion.mode == "interview":
            title = "インタビューセッション"
        else:
            title = f"議論: {discussion.topic}"

        return templates.TemplateResponse(
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
            },
        )
    except HTTPException:
        raise


@router.post("/{discussion_id}/regenerate-insights", response_class=HTMLResponse)
async def regenerate_insights(request: Request, discussion_id: str) -> Any:
    """インサイト再生成エンドポイント（htmx対応）"""
    try:
        # フォームデータからカテゴリーを取得
        form_data = await request.form()
        categories = _parse_categories_from_form(form_data)

        # 同期処理をスレッドプールで実行
        loop = asyncio.get_event_loop()
        new_insights = await loop.run_in_executor(
            executor, _regenerate_insights_sync, discussion_id, categories
        )

        # デフォルトカテゴリーを取得（モーダル用）
        from src.config import Config

        config = Config()
        default_categories = [
            cat.to_dict() for cat in config.get_default_insight_categories()
        ]

        # 更新されたインサイトパーシャルを返す
        return templates.TemplateResponse(
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
            "partials/error.html",
            {
                "request": request,
                "error": f"インサイトの再生成中にエラーが発生しました: {str(e)}",
            },
            status_code=500,
        )


def _regenerate_insights_sync(
    discussion_id: str, categories: Optional[List[InsightCategory]]
) -> list:
    """同期的なインサイト再生成処理"""
    manager = get_discussion_manager()
    return manager.regenerate_insights(discussion_id, categories=categories)


@router.delete("/{discussion_id}", response_class=HTMLResponse)
async def delete_discussion(request: Request, discussion_id: str) -> Any:
    """議論削除処理（htmx対応）"""
    try:
        discussion_manager = get_discussion_manager()
        success = discussion_manager.delete_discussion(discussion_id)

        if success:
            return templates.TemplateResponse(
                "partials/success.html",
                {"request": request, "message": "議論を削除しました"},
            )
        else:
            return templates.TemplateResponse(
                "partials/error.html",
                {"request": request, "error": "議論の削除に失敗しました"},
                status_code=400,
            )
    except Exception as e:
        logger.error(f"議論削除エラー: {e}")
        return templates.TemplateResponse(
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
                "partials/error.html",
                {"request": request, "error": "議論が見つかりません"},
                status_code=404,
            )

        return templates.TemplateResponse(
            "discussion/partials/insights.html",
            {"request": request, "insights": discussion.insights},
        )
    except Exception as e:
        logger.error(f"インサイト取得エラー: {e}")
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "error": "インサイトの取得に失敗しました"},
            status_code=500,
        )
