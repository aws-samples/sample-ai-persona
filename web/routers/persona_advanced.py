"""Router of advanced generation (beta).

- Page: ``GET /persona/generation/advanced``. Users who cannot use it are sent back to normal generation with a 303,
  and a toast is shown there.
- Upload: start, re-fetching URLs and completion are small JSON (read by the page script).
  File contents are not received. Signed URLs appear only in this JSON response and
  are never in logs or the page HTML.
- Generation: start is a POST that goes through the CSRF check. ``run`` is handed to this module's own pool, and
  if it cannot be handed over it is canceled with ``abort_start``.
- Progress: SSE at ``GET .../events``. The store is re-read every second, ``: keepalive`` is sent every 15 seconds,
  and each event carries ``id: <sequence>``. Sending resumes after ``Last-Event-ID``.
  A finished generation sends only the last result or error plus done, then closes.

Authentication is done by ``AuthMiddleware`` and CSRF by ``CSRFMiddleware`` across the whole app (same for new entry points).
Someone else's generation or file gets the same response as a nonexistent one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from src.config import config
from src.managers.advanced_generation.manager import AdvancedPersonaGenerationManager
from src.managers.advanced_generation.run_types import (
    AdvancedGenerationResult,
    ElapsedClock,
    GenerationProgress,
)
from src.managers.source_upload_manager import SourceUploadManager
from src.models.advanced_generation import (
    PERSONA_COUNT_MAX,
    PURPOSE_MAX_CHARS,
    AdvancedGeneration,
    EventType,
    GenerationEvent,
    GenerationStatus,
)
from src.models.authz import Principal
from src.models.errors import CodedError, ErrorCode, ErrorKind
from src.models.source_file import SourceFile
from web.error_messages import (
    error_kind_of,
    error_payload,
    http_status_for,
    is_correctable,
    json_error_response,
    mark_renderable,
    toast_response,
    user_message_for,
    user_message_for_code,
)

from src.models.demographics import gender_label  # noqa: E402
from src.services import country_service  # noqa: E402

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
# Display helpers for the candidate cards (same as the ordinary generation page)
templates.env.globals["gender_label"] = gender_label
templates.env.globals["country_name"] = country_service.country_name

#: Seconds between reads of the store while a generation runs.
SSE_POLL_SECONDS = 1.0
#: Seconds without a frame before a ``: keepalive`` comment (CloudFront 120 s).
SSE_KEEPALIVE_SECONDS = 15.0
#: The notice the ordinary page turns into a toast (``persona.py``).
UNAVAILABLE_NOTICE = "advanced_unavailable"
#: The parallel uploads of one file in the browser.
UPLOAD_CONCURRENCY = 4
#: Inspections (head read + parse) running at once in this process; the rest wait.
INSPECT_CONCURRENCY = 4
_inspect_slots = asyncio.Semaphore(INSPECT_CONCURRENCY)

#: The generations' own pool, apart from the other routers' pools.
generation_executor = ThreadPoolExecutor(
    max_workers=config.ADVANCED_GENERATION_MAX_CONCURRENT,
    thread_name_prefix="advanced-generation",
)

_GENERATION_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
#: Statuses answered with 409 instead of the TRANSIENT 503.
_CONFLICT_CODES = frozenset(
    {
        ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING.value,
        ErrorCode.GENERATION_NOT_AWAITING_PLAN.value,
        ErrorCode.GENERATION_ALREADY_FINISHED.value,
        ErrorCode.GENERATION_SOURCE_NOT_READY.value,
    }
)
_TERMINAL_EVENTS = frozenset({EventType.RESULT, EventType.ERROR, EventType.CANCELLED})

_source_upload_manager: SourceUploadManager | None = None
_advanced_generation_manager: AdvancedPersonaGenerationManager | None = None


def get_source_upload_manager() -> SourceUploadManager:
    """The shared SourceUploadManager (built on first use)."""
    global _source_upload_manager
    if _source_upload_manager is None:
        _source_upload_manager = SourceUploadManager()
    return _source_upload_manager


def get_advanced_generation_manager() -> AdvancedPersonaGenerationManager:
    """The shared AdvancedPersonaGenerationManager (one slot count per task)."""
    global _advanced_generation_manager
    if _advanced_generation_manager is None:
        _advanced_generation_manager = AdvancedPersonaGenerationManager()
    return _advanced_generation_manager


def _principal(request: Request) -> Principal:
    principal: Principal = request.state.principal
    return principal


def _status_of(code_value: str) -> int:
    """HTTP status of an error code value (409 for a running generation)."""
    if code_value in _CONFLICT_CODES:
        return 409
    code = ErrorCode.parse(code_value) or ErrorCode.UNKNOWN
    return http_status_for(code)


def screen_limits() -> dict[str, object]:
    """Upload limits shown on the screen and filled into the wording (``config``)."""
    return {
        "max_bytes": int(config.ADVANCED_UPLOAD_MAX_BYTES),
        "max_size_mb": int(config.ADVANCED_UPLOAD_MAX_BYTES) // 1_000_000,
        "max_files": int(config.ADVANCED_UPLOAD_MAX_FILES),
        # documents (PDF, Word, text) up to 100MB
        "max_document_bytes": int(config.ADVANCED_DOCUMENT_MAX_BYTES),
        "max_document_size_mb": int(config.ADVANCED_DOCUMENT_MAX_BYTES) // 1_000_000,
        "max_document_man_chars": int(config.ADVANCED_DOCUMENT_MAX_TOTAL_CHARS)
        // 10_000,
        "concurrency": UPLOAD_CONCURRENCY,
        "persona_count_max": PERSONA_COUNT_MAX,
        "purpose_max_chars": PURPOSE_MAX_CHARS,
        # Interval for re-fetching a row under inspection, and the seconds before giving up
        "inspect_poll_seconds": int(config.ADVANCED_UPLOAD_INSPECT_POLL_SECONDS),
        "inspect_stop_seconds": int(config.ADVANCED_UPLOAD_INSPECT_RETRY_SECONDS),
        "retention_days": int(config.ADVANCED_SOURCE_RETENTION_DAYS),
        # How long the plan card can be waited on ("choose within 15 minutes")
        "plan_wait_minutes": int(config.ADVANCED_GENERATION_PLAN_WAIT_SECONDS) // 60,
        # Number of tables that can be chosen for one generation
        "max_tables": int(config.ADVANCED_MAX_TABLES),
    }


def _no_store(body: dict[str, Any]) -> JSONResponse:
    response = JSONResponse(body)
    response.headers["Cache-Control"] = "no-store"
    return response


# --- page --------------------------------------------------------------------


@router.get("/generation/advanced", response_class=HTMLResponse)
async def advanced_generation_page(
    request: Request, generation: Optional[str] = None
) -> Any:
    """Advanced generation page. If it cannot be used, return to normal generation."""
    principal = _principal(request)
    manager = get_advanced_generation_manager()
    if not await run_in_threadpool(manager.is_available, principal):
        return RedirectResponse(
            f"/persona/generation?notice={UNAVAILABLE_NOTICE}", status_code=303
        )
    generation_id: Optional[str] = (
        generation if generation and _GENERATION_ID.match(generation) else None
    )
    if generation_id is None:
        generation_id = await run_in_threadpool(
            manager.running_generation_id, principal
        )
    elapsed: Optional[ElapsedClock] = None
    if generation_id is not None:
        try:
            elapsed = await run_in_threadpool(
                manager.elapsed_clock, principal, generation_id
            )
        except CodedError:
            # An unreadable generation shows no elapsed time (the reason is conveyed by the SSE error)
            logger.info("advanced generation elapsed time not readable")
    # The user's own file list (stale-record correction may call S3, so it runs in a thread)
    uploads = get_source_upload_manager()
    rows: list[dict[str, Any]] = []
    list_failed = False
    try:
        files = await run_in_threadpool(uploads.list_uploads, principal)
        rows = [_row_context(uploads, f) for f in files]
    except Exception:
        logger.warning("advanced upload list not readable", exc_info=True)
        list_failed = True
    # the box shows (checked) only when the Manager offers it
    detailed_offered = await run_in_threadpool(
        manager.detailed_analysis_offered, principal
    )
    return templates.TemplateResponse(
        request,
        "persona/advanced_generation.html",
        {
            "request": request,
            "title": "AIペルソナ生成（高度な生成・ベータ）",
            "limits": screen_limits(),
            "generation_id": generation_id,
            "elapsed": elapsed,
            "show_advanced_tab": True,
            "active_tab": "advanced",
            "upload_rows": rows,
            "upload_list_failed": list_failed,
            "detailed_analysis_offered": detailed_offered,
        },
    )


# --- uploads --------------------------------------------------------


class PartNumbers(BaseModel):
    """Body of the part URL refresh."""

    part_numbers: list[int] = Field(min_length=1, max_length=10000)


class CompletedPart(BaseModel):
    """One sent part and the ETag S3 returned for it."""

    part_number: int
    etag: str = Field(max_length=200)


class Completion(BaseModel):
    """Body of the completion (``parts``)."""

    parts: list[CompletedPart] = Field(max_length=10000)


def _completed(source_file: SourceFile) -> dict[str, Any]:
    """The ``UploadCompleted`` body."""
    body: dict[str, Any] = {
        "source_file_id": source_file.source_file_id,
        "status": source_file.status.value,
    }
    if source_file.metadata is not None:
        body["source_metadata"] = source_file.metadata.to_dict()
    if source_file.error_code:
        body["error_code"] = source_file.error_code
    return body


@router.post("/advanced/uploads")
async def start_upload(
    request: Request,
    filename: str = Form(...),
    size_bytes: int = Form(...),
    content_type: str = Form(""),
) -> Any:
    """Start an upload and return signed URLs for all parts."""
    manager = get_source_upload_manager()
    try:
        session = await run_in_threadpool(
            manager.start_upload,
            _principal(request),
            filename=filename,
            size_bytes=size_bytes,
            content_type=content_type,
        )
    except CodedError as e:
        logger.info("advanced upload not started")
        return json_error_response(
            e, status_code=_status_of(error_payload(e)["error_code"])
        )
    except Exception as e:
        logger.error("advanced upload start failed", exc_info=True)
        return json_error_response(e)
    return _no_store(session.to_dict())


@router.post("/advanced/uploads/{source_file_id}/part-urls")
async def refresh_part_urls(
    request: Request, source_file_id: str, body: PartNumbers
) -> Any:
    """Re-fetch URLs of the parts not yet sent."""
    manager = get_source_upload_manager()
    try:
        session = await run_in_threadpool(
            manager.refresh_part_urls,
            _principal(request),
            source_file_id,
            list(body.part_numbers),
        )
    except CodedError as e:
        logger.info("advanced upload urls not refreshed")
        return json_error_response(
            e, status_code=_status_of(error_payload(e)["error_code"])
        )
    except Exception as e:
        logger.error("advanced upload url refresh failed", exc_info=True)
        return json_error_response(e)
    return _no_store(session.to_dict())


@router.post("/advanced/uploads/{source_file_id}/complete")
async def complete_upload(
    request: Request, source_file_id: str, body: Completion
) -> Any:
    """Complete the upload and inspect the head of the file."""
    manager = get_source_upload_manager()
    parts = [(p.part_number, p.etag) for p in body.parts]
    try:
        async with _inspect_slots:
            source_file = await run_in_threadpool(
                manager.complete_upload, _principal(request), source_file_id, parts
            )
    except CodedError as e:
        logger.info("advanced upload not completed")
        return json_error_response(
            e, status_code=_status_of(error_payload(e)["error_code"])
        )
    except Exception as e:
        logger.error("advanced upload completion failed", exc_info=True)
        return json_error_response(e)
    return _no_store(_completed(source_file))


def _row_context(
    manager: SourceUploadManager, source_file: SourceFile
) -> dict[str, Any]:
    """What one row of section A needs: the file, its list item and the wording."""
    error = (
        user_message_for_code(source_file.error_code, screen_limits())
        if source_file.error_code
        else None
    )
    return {
        "source_file": source_file,
        "item": manager.list_item(source_file),
        "row_error": error,
        "stuck": manager.inspection_stuck(source_file),
    }


def render_upload_row(
    request: Request,
    source_file: SourceFile,
    *,
    headers: Optional[dict[str, str]] = None,
) -> HTMLResponse:
    """The row of one file in section A (S3)."""
    context = _row_context(get_source_upload_manager(), source_file)
    return templates.TemplateResponse(
        request,
        "persona/partials/advanced_upload_row.html",
        {"request": request, **context},
        headers=headers,
    )


@router.get("/advanced/uploads/{source_file_id}", response_class=HTMLResponse)
async def get_upload_row(request: Request, source_file_id: str) -> Any:
    """Fragment of the file state (the row under inspection re-reads it). Someone else's or nonexistent gives 404."""
    manager = get_source_upload_manager()
    try:
        source_file = await run_in_threadpool(
            manager.get_upload, _principal(request), source_file_id
        )
    except CodedError as e:
        logger.info("advanced upload row not readable")
        return toast_response(e, status_code=_status_of(error_payload(e)["error_code"]))
    return render_upload_row(request, source_file)


@router.delete("/advanced/uploads/{source_file_id}")
async def cancel_upload(request: Request, source_file_id: str) -> Any:
    """Cancel a file being uploaded (when the resend attempts run out)."""
    manager = get_source_upload_manager()
    try:
        await run_in_threadpool(
            manager.cancel_upload, _principal(request), source_file_id
        )
    except CodedError as e:
        logger.info("advanced upload not cancelled")
        return toast_response(e, status_code=_status_of(error_payload(e)["error_code"]))
    except Exception as e:
        logger.error("advanced upload cancel failed", exc_info=True)
        return toast_response(
            e, default="アップロードを取り消せませんでした", status_code=500
        )
    return Response(status_code=200)


def _delete_dialog_id(source_file_id: str) -> str:
    return f"advanced-delete-{source_file_id}"


@router.delete("/advanced/sources/{source_file_id}")
async def delete_source(request: Request, source_file_id: str) -> Any:
    """Delete an uploaded file. On success remove the row and close the dialog.

    In use, someone else's or nonexistent gives a toast. If S3 could not delete it, the row is
    replaced with a "could not delete" row and a toast is shown (pressing again continues from where it stopped).
    """
    principal = _principal(request)
    manager = get_source_upload_manager()
    close = {"dialogClose": {"id": _delete_dialog_id(source_file_id)}}
    try:
        await run_in_threadpool(manager.delete_source, principal, source_file_id)
    except CodedError as e:
        logger.info("advanced source not deleted")
        code = error_payload(e)["error_code"]
        toast = toast_response(e, status_code=_status_of(code))
        if code in (
            ErrorCode.UPLOAD_NOT_FOUND.value,
            ErrorCode.SOURCE_FILE_IN_USE.value,
        ):
            return toast
        return await _deleting_row(request, principal, source_file_id, toast, close)
    except Exception as e:
        logger.error("advanced source delete failed", exc_info=True)
        return toast_response(
            e, default="ファイルを削除できませんでした", status_code=500
        )
    return Response(
        status_code=200,
        headers={
            "HX-Trigger": json.dumps({**close, "advancedSourceDeleted": {}}),
        },
    )


async def _deleting_row(
    request: Request,
    principal: Principal,
    source_file_id: str,
    toast: Response,
    close: dict[str, Any],
) -> Response:
    """The row that says the deletion failed, with the toast of the failure."""
    triggers = {**json.loads(toast.headers.get("HX-Trigger", "{}")), **close}
    headers = {"HX-Trigger": json.dumps(triggers)}
    try:
        source_file = await run_in_threadpool(
            get_source_upload_manager().get_upload, principal, source_file_id
        )
    except CodedError:
        logger.info("advanced source row not readable after a failed delete")
        return Response(status_code=toast.status_code, headers=headers)
    return render_upload_row(request, source_file, headers=headers)


# --- generation (W4) ---------------------------------------------------------


def _form_error(request: Request, message: str) -> Any:
    """Show errors the user can fix by correcting input in an area inside the originating form (keeping the input)."""
    return mark_renderable(
        templates.TemplateResponse(
            request,
            "partials/error_inline.html",
            {"request": request, "error": message},
            status_code=400,
            headers={
                "HX-Retarget": "find .advanced-form-error",
                "HX-Reswap": "innerHTML",
            },
        )
    )


def render_progress(request: Request, generation_id: str) -> HTMLResponse:
    """Section C for one just-started generation; the page script connects its SSE."""
    return templates.TemplateResponse(
        request,
        "persona/partials/advanced_progress.html",
        {
            "request": request,
            "generation_id": generation_id,
            "elapsed": ElapsedClock(seconds=0, running=True),
        },
    )


async def _already_running(
    request: Request, principal: Principal, message: str
) -> Response:
    """The notice under the form and the running generation's progress.

    409 so the request reads as refused; ``mark_renderable`` lets htmx put it
    in the progress area below the form. No running one found: the notice only.
    """
    manager = get_advanced_generation_manager()
    generation_id: Optional[str] = None
    elapsed: Optional[ElapsedClock] = None
    try:
        generation_id = await run_in_threadpool(
            manager.running_generation_id, principal
        )
        if generation_id is not None:
            elapsed = await run_in_threadpool(
                manager.elapsed_clock, principal, generation_id
            )
    except CodedError:
        logger.info("running advanced generation not readable")
    return mark_renderable(
        templates.TemplateResponse(
            request,
            "persona/partials/advanced_progress.html",
            {
                "request": request,
                "generation_id": generation_id,
                "elapsed": elapsed,
                "notice": message,
            },
            status_code=409,
        )
    )


@router.post("/advanced/generations", response_class=HTMLResponse)
async def start_generation(
    request: Request,
    source_file_ids: list[str] = Form(default=[]),
    purpose: str = Form(""),
    persona_count: int = Form(...),
    confirm_plan: bool = Form(False),
    use_detailed_analysis: bool = Form(False),
) -> Any:
    """Start a generation and return the progress section. ``run`` runs on a dedicated pool.

    Receives "review the analysis plan before running" as ``confirm_plan``.
    Receives "detailed analysis by AI" as ``use_detailed_analysis``. Whether it is used is
    decided by the Manager at start (it is not used even if sent when not allowed).
    When a generation is already running, return that fact and its progress with 409.
    """
    principal = _principal(request)
    manager = get_advanced_generation_manager()
    try:
        generation_id = await run_in_threadpool(
            manager.start_generation,
            principal,
            source_file_ids=list(source_file_ids),
            purpose=purpose,
            persona_count=persona_count,
            confirm_plan=confirm_plan,
            use_detailed_analysis=use_detailed_analysis,
        )
    except CodedError as e:
        logger.info("advanced generation not started")
        if error_kind_of(e) is ErrorKind.FORBIDDEN:
            raise  # 403 goes to the shared exception handler (toast)
        if is_correctable(e):
            return _form_error(request, user_message_for(e))
        code = error_payload(e)["error_code"]
        if code == ErrorCode.ADVANCED_GENERATION_ALREADY_RUNNING.value:
            return await _already_running(request, principal, user_message_for(e))
        return toast_response(e, status_code=_status_of(code))
    except Exception as e:
        logger.error("advanced generation start failed", exc_info=True)
        return toast_response(e, default="生成を始められませんでした", status_code=500)
    try:
        generation_executor.submit(manager.run, generation_id)
    except Exception:
        logger.error("advanced generation not handed to the pool", exc_info=True)
        try:
            await run_in_threadpool(
                manager.abort_start,
                generation_id,
                owner_sub=principal.sub,
                source_file_ids=list(source_file_ids),
            )
        except Exception:
            logger.error("advanced generation start not undone", exc_info=True)
        busy = CodedError(code=ErrorCode.ADVANCED_GENERATION_BUSY)
        return toast_response(busy, status_code=503)
    return render_progress(request, generation_id)


# --- The plan card and the cancel -----------------------------------


async def _control(
    request: Request,
    generation_id: str,
    action: str,
    revised_purpose: Optional[str] = None,
    *,
    plan_only: bool = False,
    data_reading: Optional[dict[str, dict[str, Any]]] = None,
) -> Optional[Response]:
    """Hand one request to the Manager; the refusal as a response, else ``None``.

    Someone else's or a missing generation is 404, a state that does not
    allow it 409 (toasts, the screen is left as it is); a too long instruction
    or an action the plan card does not send goes into the card's own error
    area (VALIDATION, the input stays). ``plan_only``: the plan route, which
    takes approve and replan only.
    """
    manager = get_advanced_generation_manager()
    try:
        await run_in_threadpool(
            manager.decide_plan if plan_only else manager.request_control,
            _principal(request),
            generation_id,
            action,
            revised_purpose=revised_purpose,
            **({"data_reading": data_reading} if plan_only else {}),
        )
    except CodedError as e:
        logger.info("advanced generation control refused")
        if is_correctable(e):
            return mark_renderable(
                templates.TemplateResponse(
                    request,
                    "partials/error_inline.html",
                    {"request": request, "error": user_message_for(e)},
                    status_code=400,
                    headers={
                        "HX-Retarget": "find .advanced-plan-error",
                        "HX-Reswap": "innerHTML",
                    },
                )
            )
        return toast_response(e, status_code=_status_of(error_payload(e)["error_code"]))
    except Exception as e:
        logger.error("advanced generation control failed", exc_info=True)
        return toast_response(
            e, default="操作を受け付けられませんでした", status_code=500
        )
    return None


def _reading_choices(form: Any) -> Optional[dict[str, dict[str, Any]]]:
    """The plan card's table choices: ``reading_kind__<alias>`` and its fellows.

    The Manager checks every value against the table's own columns; here the
    form is only put into shape.
    """
    out: dict[str, dict[str, Any]] = {}
    for key in form.keys():
        if not key.startswith("reading_kind__"):
            continue
        alias = key.removeprefix("reading_kind__")
        out[alias] = {
            "kind": str(form.get(key)),
            "person_id": str(form.get(f"reading_person__{alias}") or "") or None,
            "skip": [str(v) for v in form.getlist(f"reading_skip__{alias}")],
        }
    return out or None


@router.post("/advanced/generations/{generation_id}/plan", response_class=HTMLResponse)
async def decide_plan(
    request: Request,
    generation_id: str,
    action: str = Form(...),
    revised_purpose: str = Form(""),
) -> Any:
    """Approve the plan, or correct the instructions and replan."""
    revised = revised_purpose if action == "replan" else None
    refused = await _control(
        request,
        generation_id,
        action,
        revised,
        plan_only=True,
        data_reading=_reading_choices(await request.form()),
    )
    if refused is not None:
        return refused
    return templates.TemplateResponse(
        request,
        "persona/partials/advanced_plan_card.html",
        {
            "request": request,
            "generation_id": generation_id,
            "plan": None,
            "purpose": revised or "",
            "awaiting": False,
            "accepted": action,
            "limits": screen_limits(),
        },
    )


@router.post(
    "/advanced/generations/{generation_id}/cancel", response_class=HTMLResponse
)
async def cancel_generation(request: Request, generation_id: str) -> Any:
    """Accept the cancel and return "canceling"."""
    refused = await _control(request, generation_id, "cancel")
    if refused is not None:
        return refused
    return templates.TemplateResponse(
        request,
        "persona/partials/advanced_cancel.html",
        {"request": request, "generation_id": generation_id, "cancelling": True},
    )


# --- progress -----------------------------------------------------------


def _frame(event: str, data: str, event_id: Optional[int] = None) -> str:
    """One SSE frame; multi-line data becomes several ``data:`` lines.

    SSE ends a line at ``\\r\\n``, ``\\r`` or ``\\n``, so every break is made
    ``\\n`` first; a bare ``\\r`` would otherwise start a line without ``data:``.
    """
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    text = data.replace("\r\n", "\n").replace("\r", "\n")
    lines.extend(f"data: {line}" for line in (text.split("\n") if text else [""]))
    return "\n".join(lines) + "\n\n"


def _last_event_id(raw: Optional[str]) -> Optional[int]:
    if raw is None or not raw.strip().isdigit():
        return None
    return int(raw.strip())


def _summary(event: GenerationEvent) -> dict[str, Any]:
    try:
        value = json.loads(event.summary or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _failure_values(progress: GenerationProgress) -> dict[str, Any]:
    """The values the failure wording may use (with the owner's file name)."""
    return dict(progress.failure_context or progress.generation.error_context or {})


def _stored_error(code_value: Optional[str], context: Optional[dict[str, Any]]) -> str:
    """The ``error`` payload of a failure stored as a code (never text)."""
    code = ErrorCode.parse(code_value) or ErrorCode.GENERATION_OPERATION_FAILED
    return json.dumps(
        {
            "error_code": code.value,
            "kind": code.kind.value,
            "message": user_message_for_code(code.value, dict(context or {})),
        }
    )


def render_result(result: AdvancedGenerationResult) -> str:
    """Section D as HTML (the ``result`` payload)."""
    generation = result.generation
    readable_until = (
        generation.finished_at
        + timedelta(seconds=config.ADVANCED_GENERATION_RESULT_READABLE_SECONDS)
        if generation.finished_at
        else None
    )
    template = templates.get_template("persona/partials/advanced_result.html")
    return template.render(
        result=result,
        readable_until=readable_until,
        record_url=f"/persona/advanced/generations/{generation.generation_id}/record",
    )


def _has_card(progress: GenerationProgress) -> bool:
    """A plan the user confirms is shown as its card."""
    generation = progress.generation
    return generation.confirm_plan and generation.plan is not None


def render_plan_card(
    generation: AdvancedGeneration, *, accepted: Optional[str] = None
) -> str:
    """The plan card as HTML (the ``plan`` payload; escaped).

    ``accepted``: ``approve`` or ``replan`` once the user chose (the POST's
    answer); the buttons are then left out.
    """
    template = templates.get_template("persona/partials/advanced_plan_card.html")
    return template.render(
        generation_id=generation.generation_id,
        plan=generation.plan,
        reading=generation.data_reading,
        purpose=generation.purpose,
        awaiting=generation.status is GenerationStatus.AWAITING_PLAN_APPROVAL,
        accepted=accepted,
        limits=screen_limits(),
    )


async def _result_frame(
    manager: AdvancedPersonaGenerationManager,
    principal: Principal,
    generation_id: str,
    event_id: Optional[int],
) -> str:
    try:
        result = await run_in_threadpool(manager.get_result, principal, generation_id)
    except CodedError as e:
        logger.warning("advanced generation result not readable")
        return _frame("error", json.dumps(error_payload(e)), event_id)
    return _frame("result", render_result(result), event_id)


async def _event_frames(
    request: Request,
    manager: AdvancedPersonaGenerationManager,
    principal: Principal,
    generation_id: str,
    first: GenerationProgress,
    after: Optional[int],
) -> AsyncIterator[str]:
    progress = first
    sent = after
    terminal_sent = False
    last_write = time.monotonic()
    events: tuple[GenerationEvent, ...] = progress.events
    if after is None and progress.is_finished:
        # First connection to a finished generation: send only the last result or error
        events = tuple(e for e in events if e.event_type in _TERMINAL_EVENTS)[-1:]
    while True:
        for event in events:
            if event.event_type is EventType.RESULT:
                frame = await _result_frame(
                    manager, principal, generation_id, event.sequence
                )
                terminal_sent = True
            elif event.event_type is EventType.CANCELLED:
                # the cancel is not an error; the payload is empty
                frame = _frame("cancelled", "", event.sequence)
                terminal_sent = True
            elif event.event_type is EventType.PLAN and _has_card(progress):
                frame = _frame(
                    "plan", render_plan_card(progress.generation), event.sequence
                )
            elif event.event_type is EventType.ERROR:
                summary = _summary(event)
                frame = _frame(
                    "error",
                    _stored_error(summary.get("error_code"), _failure_values(progress)),
                    event.sequence,
                )
                terminal_sent = True
            else:
                frame = _frame(
                    event.event_type.value, json.dumps(_summary(event)), event.sequence
                )
            yield frame
            last_write = time.monotonic()
            sent = event.sequence
        if progress.is_finished:
            if not terminal_sent:
                generation = progress.generation
                if generation.status is GenerationStatus.DONE:
                    yield await _result_frame(manager, principal, generation_id, None)
                elif generation.status is GenerationStatus.CANCELLED:
                    yield _frame("cancelled", "")
                else:
                    yield _frame(
                        "error",
                        _stored_error(generation.error_code, _failure_values(progress)),
                    )
            yield _frame("done", "")
            return
        await asyncio.sleep(SSE_POLL_SECONDS)
        if time.monotonic() - last_write >= SSE_KEEPALIVE_SECONDS:
            yield ": keepalive\n\n"
            last_write = time.monotonic()
        if await request.is_disconnected():
            return
        try:
            progress = await run_in_threadpool(
                manager.stream_events, principal, generation_id, after_sequence=sent
            )
        except CodedError as e:
            logger.warning("advanced generation events not readable")
            yield _frame("error", json.dumps(error_payload(e)))
            yield _frame("done", "")
            return
        events = progress.events


def _event_stream(frames: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        frames,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _expired_frames(payload: dict[str, str]) -> AsyncIterator[str]:
    yield _frame("error", json.dumps(payload))
    yield _frame("done", "")


@router.get("/advanced/generations/{generation_id}/events")
async def generation_events(request: Request, generation_id: str) -> Any:
    """SSE of progress. Read-only; it does not start a generation. Owner only."""
    # The session is checked only at connect time (up to 60 minutes). It is not re-checked while connected.
    # It is a passive request, so connecting alone does not extend the last-activity time (PASSIVE_REQUESTS)
    principal = _principal(request)
    manager = get_advanced_generation_manager()
    # The browser's automatic reconnect sends it in a header; when the page reconnects itself it arrives in the query
    after = _last_event_id(
        request.headers.get("Last-Event-ID")
        or request.query_params.get("last_event_id")
    )
    try:
        first = await run_in_threadpool(
            manager.stream_events, principal, generation_id, after_sequence=after
        )
    except CodedError as e:
        logger.info("advanced generation events refused")
        payload = error_payload(e)
        if payload["error_code"] == ErrorCode.GENERATION_EXPIRED.value:
            return _event_stream(_expired_frames(payload))
        return toast_response(e, status_code=_status_of(payload["error_code"]))
    return _event_stream(
        _event_frames(request, manager, principal, generation_id, first, after)
    )


@router.get("/advanced/generations/{generation_id}/record", response_class=HTMLResponse)
async def generation_record(request: Request, generation_id: str) -> Any:
    """Contents of the analysis-record dialog. Owner only, up to 24 hours."""
    manager = get_advanced_generation_manager()
    try:
        record = await run_in_threadpool(
            manager.get_record, _principal(request), generation_id
        )
    except CodedError as e:
        logger.info("advanced generation record not readable")
        if error_kind_of(e) is ErrorKind.NOT_FOUND:
            return mark_renderable(
                templates.TemplateResponse(
                    request,
                    "partials/error_banner.html",
                    {"request": request, "error": user_message_for(e)},
                    status_code=404,
                )
            )
        return toast_response(e, status_code=_status_of(error_payload(e)["error_code"]))
    return templates.TemplateResponse(
        request,
        "persona/partials/advanced_record_dialog.html",
        {"request": request, "record": record},
    )
