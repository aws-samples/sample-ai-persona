"""
Discussion router

The Router decides nothing. It passes ``request.state.principal`` to the Manager and leaves
the decision exceptions (``*_NOT_FOUND``, ``ACCESS_DENIED``, ``LOOKUP_UNAVAILABLE``) to
the shared exception handler. If a participant cannot be read when a discussion starts,
it returns the stop panel (``discussion/partials/conversation_stopped.html``).
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
from src.models.insight import split_observation_hypothesis
from src.models.insight_category import InsightCategory
from src.models.model_registry import (
    default_selectable_model_id,
    display_name_for,
    list_selectable_models,
)
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

# Register display helpers as template global functions (same as persona.py).
# country_service is a pure data lookup from ISO country code to name, so the Router uses it
# directly as a display helper (the display-helper exception to the architecture rule "Router -> via Manager").
from src.services import country_service  # noqa: E402
from src.models.demographics import gender_label, GENDER_LABELS  # noqa: E402
from ._persona_avatar import persona_avatar_url  # noqa: E402

templates.env.globals["country_name"] = country_service.country_name
templates.env.globals["country_choices"] = country_service.country_choices
templates.env.globals["gender_label"] = gender_label
templates.env.globals["GENDER_LABELS"] = GENDER_LABELS
templates.env.globals["split_insight"] = split_observation_hypothesis
templates.env.globals["persona_avatar_url"] = persona_avatar_url
templates.env.globals["model_display_name"] = display_name_for

# Thread pool executor (to run synchronous AI work asynchronously)
executor = ThreadPoolExecutor(max_workers=8)

#: The tab panel of the results page (``list_tabpanel``); tab switches and
#: ``listReload`` target it and get only the panel content back.
LIST_PANEL_ID = "discussion-list"

#: Where the stop panel of a discussion leads back to.
_SETUP_URL = "/discussion/setup"


# Singleton manager instances (shared at module level)
_persona_manager = None
_discussion_manager = None
_agent_discussion_manager = None
_report_manager = None
_file_manager = None


def get_persona_manager() -> PersonaManager:
    """Get the PersonaManager singleton instance."""
    global _persona_manager
    if _persona_manager is None:
        _persona_manager = PersonaManager()
    return _persona_manager


def get_discussion_manager() -> DiscussionManager:
    """Get the DiscussionManager singleton instance."""
    global _discussion_manager
    if _discussion_manager is None:
        _discussion_manager = DiscussionManager()
    return _discussion_manager


def get_file_manager() -> FileManager:
    """Get the FileManager singleton instance."""
    global _file_manager
    if _file_manager is None:
        _file_manager = FileManager()
    return _file_manager


def get_agent_discussion_manager() -> AgentDiscussionManager:
    """Get the AgentDiscussionManager singleton instance."""
    global _agent_discussion_manager
    if _agent_discussion_manager is None:
        _agent_discussion_manager = AgentDiscussionManager()
    return _agent_discussion_manager


def get_report_manager() -> ReportManager:
    """Get the ReportManager singleton instance."""
    global _report_manager
    if _report_manager is None:
        _report_manager = ReportManager()
    return _report_manager


def _principal(request: Request) -> Principal:
    """The signed-in user that ``AuthMiddleware`` put on the request."""
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
    """Stop panel: a participant can no longer be read; the setup form is kept."""
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
    """Parse category info from form data."""
    categories = []
    index = 0

    while True:
        name_key = f"categories[{index}][name]"
        desc_key = f"categories[{index}][description]"

        if name_key not in form_data:
            break

        name = form_data.get(name_key, "").strip()
        description = form_data.get(desc_key, "").strip()

        # Add only when both name and description are present
        if name and description:
            try:
                category = InsightCategory(name=name, description=description)
                categories.append(category)
            except ValueError as e:
                logger.warning(f"Invalid category at index {index}: {e}")

        index += 1

    # None if no categories are found (the default is used)
    return categories if categories else None


@router.get("/setup", response_class=HTMLResponse)
async def discussion_setup_page(request: Request) -> Any:
    """Discussion setup page (the persona list is lazy-loaded with htmx)."""
    # Match the list and the default selection to the inference scope
    selectable_models = list_selectable_models(
        config.ENABLE_ADDITIONAL_PERSONA_MODELS, config.INFERENCE_SCOPE
    )
    return templates.TemplateResponse(
        request,
        "discussion/setup.html",
        {
            "request": request,
            "title": "議論設定",
            "selectable_models": selectable_models,
            "enable_additional_models": config.ENABLE_ADDITIONAL_PERSONA_MODELS,
            "default_model_id": default_selectable_model_id(
                selectable_models, config.AGENT_MODEL_ID
            ),
        },
    )


@router.post("/upload-document")
async def upload_discussion_document(
    request: Request, file: UploadFile = File(...)
) -> Any:
    """Upload a discussion document (placed under the user's key and unusable after 24 hours)."""
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
    """Get the discussion result partial (used after real-time display finishes). Readable users only."""
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


# This runs the AI and saves, so starting is a POST that goes through the CSRF check.
# The screen reads the response stream with web/static/js/post-event-source.js. With a GET SSE,
# an <img src> in a shared report could start it with the viewer's permissions
@router.post("/stream")
async def stream_discussion(
    request: Request,
    topic: str = Form(...),
    persona_ids: str = Form(...),
    mode: str = Form("traditional"),
    rounds: int = Form(3),
    additional_instructions: str = Form(""),
    enable_memory: bool = Form(False),
    memory_mode: str = Form("full"),
    enable_dataset: bool = Form(False),
    enable_kb: bool = Form(False),
    categories_json: str = Form(""),
    document_ids: str = Form(""),
    persona_models_json: str = Form(""),
    facilitator_model: str = Form(""),
) -> Any:
    """Stream a live discussion (SSE over the POST response), classic or agent."""
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
            # Includes an unreadable participant. No reason is given; the screen shows the panel
            return single(_sse({"type": "stopped"}))
        logger.warning("Discussion stream not started", exc_info=True)
        return single(
            _sse_error(
                user_message_for(e, default="議論の実行中にエラーが発生しました")
            )
        )

    # Parse category info
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

    # Parse per-persona model selection (received as one URL-encoded JSON string)
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
    """List of discussion/interview results (per tab).

    Only the list part is returned for tab switching, search, filtering, reloading (HX-Target is the list part)
    and "show more". For members, tab=all means "my own".
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
    """Discussion start handler (htmx supported)."""
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
        # The statements live outside the body, so re-read the saved discussion as the reader and show it
        view = await _run(get_discussion_manager().get_discussion, principal, saved.id)
    except CodedError as e:
        if isinstance(e, ParticipantNotFoundError):
            logger.info("Discussion not started: a participant is unreadable")
            return _conversation_stopped(request)
        logger.error("Failed to start discussion", exc_info=True)
        if error_kind_of(e) is ErrorKind.NOT_FOUND:
            # Someone else's or expired material gives the same 404 toast as nonexistent
            return toast_response(e, status_code=404)
        if error_kind_of(e) is ErrorKind.FORBIDDEN:
            return toast_response(e, status_code=403)
        if is_correctable(e) and isinstance(
            e, (DiscussionManagerError, AgentDiscussionManagerError)
        ):
            # Errors the user can fix by correcting input (topic length, persona count, etc.)
            # are shown in #discussion-result (a dedicated area outside the form itself).
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
        # Errors that a retry can fix are shown as a toast without clearing the settings form
        return toast_response(
            e, default="議論の開始中にエラーが発生しました", status_code=500
        )
    except Exception as e:
        # An unknown exception with no code is treated like TRANSIENT. Replacing the body would
        # lose the input, and htmx does not swap non-2xx bodies, so notify with a toast.
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
    """Export a discussion as Markdown (readable users only)."""
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
    """One attachment of a discussion.

    Served only when the discussion is readable, the attachment belongs to it, and the attachment is readable. Public mode redirects 302
    to a signed URL; the closed network has the app stream it. If unreadable the shared exception handler answers 404 and S3 is not touched.
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
    """Discussion detail page (including interviews). Readable users only; actions only for those who can manage."""
    from src.managers.settings_manager import SettingsManager

    principal = _principal(request)
    manager = get_discussion_manager()
    view = await _run(manager.get_discussion, principal, discussion_id)
    # Attachments are opened through an app route (both public and closed network). The signature is not made here
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
    """Insight regeneration endpoint (htmx supported, users who can manage only)."""
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
            # Errors the user can fix by correcting input are shown in #insights-container (a dedicated area
            # outside the regeneration form). The category input stays as a separate element in the modal.
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
        # Errors that a retry can fix are shown as a toast without clearing the insight display
        return toast_response(
            e,
            default="インサイトの再生成中にエラーが発生しました",
            status_code=500,
        )
    except CodedError:
        # Unreadable (404), unmanageable (403) and missing ai:execute (403) go to the shared handler
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
    """Discussion deletion handler (htmx supported, users who can manage only)."""
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
    """Get discussion insights (htmx supported, readable users only)."""
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
# Report endpoints
# =============================================================================


def _data_agent_refusal(
    report_manager: ReportManager, principal: Principal
) -> StreamingResponse | None:
    """One ``error`` event when the data agent may not be used.

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
        # Exceptions with a known kind, such as over capacity, return the catalog wording. Diagnostics go only to the log
        logger.error("Failed to generate report", exc_info=exc)
        yield _sse_error(user_message_for(exc, default=default))
        return
    yield _sse({"type": "done"})


# This runs the AI and saves, so starting is a POST that goes through the CSRF check
@router.post("/{discussion_id}/report/generate")
async def generate_report_stream(
    request: Request,
    discussion_id: str,
    template_type: str = Form("summary"),
    custom_prompt: Optional[str] = Form(None),
) -> Any:
    """Generate a report with SSE streaming (readable users)."""
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
    """Generate a follow-up analysis with POST SSE streaming.

    The short-term memory session is decided by the server from the pair of discussion and user (the
    session ID sent from the screen is not used).
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
    """Save a previewed report (users who can manage; the ID is assigned by the server)."""
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
    """Update report content (users who can manage only)."""
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
        # Failures a retry fixes get a toast (the screen is not rewritten). Fixable failures are marked and the body is shown
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
    """Get report content (readable users only)."""
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
    """Export a report as a file (readable users only)."""
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
    """Delete a report (users who can manage only)."""
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
        # Failures a retry fixes get a toast (the row is not removed). Fixable failures are marked and the body is shown
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
