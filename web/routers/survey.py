"""
マスアンケート機能のルーター

U7: テンプレート・結果は所有者と部署への共有で読める人が決まり（BR1.2〜BR1.4）、
ペルソナ元データの管理は admin だけ（BR4.1）。判定は Manager が行い、読めない・ないもの
（404）と読めるが変えられないもの（403）は共通の例外ハンドラが答える。
"""

from dataclasses import replace
from typing import Any, Callable, Optional
import json
import logging
import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.responses import (
    HTMLResponse,
    Response,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from src.managers.survey_analysis_manager import (
    SurveyAnalysisManager,
    SurveyAnalysisManagerError,
)
from src.managers.survey_dataset_manager import (
    SurveyDatasetManager,
    SurveyDatasetManagerError,
    SurveyDatasetValidationError,
)
from src.managers.survey_execution_manager import (
    SurveyExecutionManager,
    SurveyExecutionManagerError,
    SurveyExecutionValidationError,
)
from src.managers.survey_template_manager import (
    SurveyTemplateManager,
    SurveyTemplateManagerError,
    SurveyTemplateValidationError,
)
from src.managers.file_manager import FileManager
from src.managers.settings_manager import SettingsManager
from src.models.authz import Principal
from src.models.errors import CodedError, ErrorCode, ErrorKind
from src.models.survey_template import Question, TemplateImage

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

from web.error_messages import (  # noqa: E402
    error_kind_of,
    is_transient,
    mark_renderable,
    toast_response,
    user_message_for,
    user_message_for_code,
)
from web.sanitize import render_markdown  # noqa: E402
from web import asset_scope_views  # noqa: E402

# マークダウンフィルターを追加
templates.env.filters["markdown"] = render_markdown

executor = ThreadPoolExecutor(max_workers=8)

#: Kinds answered by the common exception handler (404 / 403), never by the screen.
_ACCESS_KINDS = (ErrorKind.NOT_FOUND, ErrorKind.FORBIDDEN, ErrorKind.UNAUTHORIZED)

#: Tab panels (``list_tabpanel``) of the template and result lists; tab
#: switches, searches and ``listReload`` target them and get only the panel.
TEMPLATE_PANEL_ID = "survey-template-list"
RESULT_PANEL_ID = "survey-list"
#: The template choice on the start page (its own tabs and search).
START_PANEL_ID = "start-template-panel"
#: The custom persona data list (reloaded after an import, a delete or a scope save).
CUSTOM_LIST_ID = "custom-dataset-list"

# --- Manager遅延初期化 ---
_template_manager: SurveyTemplateManager | None = None
_dataset_manager: SurveyDatasetManager | None = None
_execution_manager: SurveyExecutionManager | None = None
_analysis_manager: SurveyAnalysisManager | None = None
_file_manager: FileManager | None = None


def get_template_manager() -> SurveyTemplateManager:
    global _template_manager
    if _template_manager is None:
        _template_manager = SurveyTemplateManager()
    return _template_manager


def get_dataset_manager() -> SurveyDatasetManager:
    global _dataset_manager
    if _dataset_manager is None:
        _dataset_manager = SurveyDatasetManager()
    return _dataset_manager


def get_execution_manager() -> SurveyExecutionManager:
    global _execution_manager
    if _execution_manager is None:
        _execution_manager = SurveyExecutionManager()
    return _execution_manager


def get_analysis_manager() -> SurveyAnalysisManager:
    global _analysis_manager
    if _analysis_manager is None:
        _analysis_manager = SurveyAnalysisManager()
    return _analysis_manager


def get_file_manager() -> FileManager:
    """FileManager（テンプレートの画像のアップロードと配信、U5）"""
    global _file_manager
    if _file_manager is None:
        _file_manager = FileManager()
    return _file_manager


def _principal(request: Request) -> Principal:
    """The signed-in user that ``AuthMiddleware`` put on the request (C1)."""
    principal: Principal = request.state.principal
    return principal


async def _run(func: Callable[..., Any], *args: Any) -> Any:
    """Run a synchronous Manager call (DynamoDB / S3 / DuckDB / AI) in the pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, func, *args)


def _panel_only(request: Request, panel_id: str, append: bool = False) -> bool:
    """Whether to answer with the tab panel only (tab switch, search, reload)."""
    return request.headers.get("HX-Target") == panel_id or append


def _list_error(request: Request, message: str) -> Any:
    """The panel of a list that could not be read (503, retry button)."""
    return mark_renderable(
        templates.TemplateResponse(
            request,
            "partials/error_banner.html",
            {"request": request, "error": message},
            status_code=503,
        )
    )


# =========================================================================
# 画面表示エンドポイント
# =========================================================================


@router.get("", response_class=HTMLResponse)
async def survey_index(request: Request) -> Any:
    """アンケートTOP画面"""
    return templates.TemplateResponse(
        request,
        "survey/index.html",
        {"request": request, "title": "マスアンケート"},
    )


# =========================================================================
# ペルソナ元データ（閲覧は使える人、管理は admin だけ、BR4.1・BR4.2）
# =========================================================================


async def _persona_data_context(principal: Principal) -> dict[str, Any]:
    """The persona data list of one user (usable custom data; admins see all)."""
    listing = await _run(get_dataset_manager().list_persona_data, principal)
    return {"listing": listing}


@router.get("/persona-data", response_class=HTMLResponse)
async def persona_data_page(request: Request) -> Any:
    """ペルソナデータ設定画面（member は使えるデータを見るだけ）"""
    manager = get_dataset_manager()
    # 状態の取得は失敗しても例外を投げない（閉域対応 U3 BR2.2）。Router は代替を持たない
    nemotron_status = await _run(manager.check_nemotron_status)
    context = await _persona_data_context(_principal(request))
    return templates.TemplateResponse(
        request,
        "survey/persona_data.html",
        {
            "request": request,
            "title": "ペルソナデータ設定",
            "nemotron_status": nemotron_status,
            "downloading": _nemotron_download_status["downloading"],
            "manageable": context["listing"].manageable,
            "data_agent_allowed": SettingsManager.is_data_agent_allowed(),
            **context,
        },
    )


@router.get("/persona-data/custom", response_class=HTMLResponse)
async def custom_dataset_list(request: Request) -> Any:
    """カスタムデータの一覧（読み直し用、data-reload-url）"""
    context = await _persona_data_context(_principal(request))
    return templates.TemplateResponse(
        request,
        "survey/partials/custom_dataset_list.html",
        {"request": request, **context},
    )


_nemotron_download_status: dict = {"downloading": False, "error": None}


def _download_nemotron_background(principal: Principal) -> None:
    """バックグラウンドでNemotronデータセットをダウンロードする。"""
    try:
        get_dataset_manager().download_nemotron_dataset(principal)
    except Exception:
        logger.error("Failed to download Nemotron dataset", exc_info=True)
        _nemotron_download_status["error"] = "データセットのダウンロードに失敗しました"
    finally:
        _nemotron_download_status["downloading"] = False


async def _nemotron_status_response(request: Request) -> Any:
    """The "downloading" partial, with the Manager's status (no stand-in, BR2.2)."""
    status = await _run(get_dataset_manager().check_nemotron_status)
    return templates.TemplateResponse(
        request,
        "survey/partials/nemotron_status.html",
        {
            "request": request,
            "nemotron_status": status,
            "downloading": True,
            "manageable": True,
        },
    )


@router.post("/persona-data/download-nemotron", response_class=HTMLResponse)
async def download_nemotron(request: Request) -> Any:
    """Nemotronデータセットのダウンロードをバックグラウンドで開始（admin だけ）"""
    principal = _principal(request)
    # 裏で動かす前に止める（裏の失敗は応答に届かない）。閉域では取得しない（U3 BR2.3）
    get_dataset_manager().ensure_nemotron_downloadable(principal)
    if _nemotron_download_status["downloading"]:
        return await _nemotron_status_response(request)
    _nemotron_download_status["downloading"] = True
    _nemotron_download_status["error"] = None
    asyncio.create_task(asyncio.to_thread(_download_nemotron_background, principal))
    return await _nemotron_status_response(request)


# 受け身の要求（BR1.9）: 5 秒ごとの自動更新。web/auth_middleware.py の PASSIVE_REQUESTS に載せている
@router.get("/persona-data/nemotron-status", response_class=HTMLResponse)
async def nemotron_download_status(request: Request) -> Any:
    """Nemotronダウンロード状況をポーリングで返す"""
    manager = get_dataset_manager()
    status = await _run(manager.check_nemotron_status)
    listing = await _run(manager.list_persona_data, _principal(request))
    return templates.TemplateResponse(
        request,
        "survey/partials/nemotron_status.html",
        {
            "request": request,
            "nemotron_status": status,
            "downloading": _nemotron_download_status["downloading"],
            "error": _nemotron_download_status["error"],
            "success": status.available
            and not _nemotron_download_status["downloading"],
            "manageable": listing.manageable,
        },
    )


def _standard_mapping(form: Any) -> dict[str, str]:
    """``mapping_<standard column>`` fields -> {standard column: CSV column}."""
    mapping: dict[str, str] = {}
    for key, value in form.items():
        if key.startswith("mapping_") and value:
            mapping[key[len("mapping_") :]] = str(value)
    return mapping


def _extra_columns(form: Any) -> list[dict[str, str]]:
    """``extra_column_<i>`` fields (gaps in the indices are allowed)."""
    extras: list[dict[str, str]] = []
    indices = sorted(
        int(k.split("_", 2)[2])
        for k in form.keys()
        if k.startswith("extra_column_") and form.get(k)
    )
    for i in indices:
        csv_col = str(form.get(f"extra_column_{i}", ""))
        if csv_col:
            extras.append(
                {
                    "csv_column": csv_col,
                    "label": str(form.get(f"extra_label_{i}", "")) or csv_col,
                    "description": str(form.get(f"extra_desc_{i}", "")),
                }
            )
    return extras


def _upload_result(request: Request, error: str) -> Any:
    return templates.TemplateResponse(
        request,
        "survey/partials/custom_upload_result.html",
        {"request": request, "error": error},
    )


async def _column_mapping(
    request: Request,
    principal: Principal,
    *,
    filename: str,
    temp_key: str,
    parsed: dict[str, Any],
    auto_mapping: dict[str, str],
    dataset_name: str,
    is_dwh: bool = False,
    suggested_extra: Optional[list] = None,
) -> Any:
    manager = get_dataset_manager()
    choices = await _run(manager.import_scope_choices, principal)
    return templates.TemplateResponse(
        request,
        "survey/partials/column_mapping.html",
        {
            "request": request,
            "filename": filename,
            "temp_key": temp_key,
            "columns": parsed["columns"],
            "samples": parsed["samples"],
            "auto_mapping": auto_mapping,
            "standard_columns": manager.get_standard_columns(),
            "is_dwh": is_dwh,
            "dataset_name": dataset_name,
            "suggested_extra": suggested_extra or [],
            "scope_choices": choices,
        },
    )


@router.post("/persona-data/upload-custom", response_class=HTMLResponse)
async def upload_custom_step1(request: Request, file: UploadFile = File(...)) -> Any:
    """Step1: CSVアップロード → カラム解析 → マッピングUI表示（admin だけ）"""
    principal = _principal(request)
    manager = get_dataset_manager()
    manager.ensure_manageable(principal)
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return _upload_result(
            request,
            user_message_for_code(
                ErrorCode.FILE_FORMAT_NOT_ALLOWED, {"allowed_formats": "CSV"}
            ),
        )
    try:
        content = await file.read()
        if len(content) > 500 * 1024 * 1024:
            return _upload_result(
                request,
                user_message_for_code(ErrorCode.FILE_TOO_LARGE, {"max_size_mb": 500.0}),
            )
        parsed = await _run(manager.parse_csv_columns, principal, content)

        # CSVバイト列を一時的にS3に保存（マッピング確定後に使用）
        temp_key = f"persona-dataset/temp/{uuid.uuid4().hex[:8]}-{file.filename}"
        await _run(manager.upload_temp_file, principal, content, temp_key)

        return await _column_mapping(
            request,
            principal,
            filename=file.filename,
            temp_key=temp_key,
            parsed=parsed,
            auto_mapping=parsed["auto_mapping"],
            dataset_name=Path(file.filename).stem,
        )
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.error("Failed to parse CSV", exc_info=True)
        return _upload_result(
            request, user_message_for(e, default="CSVファイルの解析に失敗しました")
        )


@router.post("/persona-data/preview-prompt", response_class=HTMLResponse)
async def preview_persona_prompt(request: Request) -> Any:
    """マッピング設定に基づくシステムプロンプトのプレビューを返す。"""
    principal = _principal(request)
    manager = get_dataset_manager()
    manager.ensure_manageable(principal)
    form = await request.form()
    temp_key = str(form.get("temp_key", ""))
    column_mapping = _standard_mapping(form)
    extra_columns = _extra_columns(form)

    try:
        csv_bytes = await _run(manager.download_temp_file, principal, temp_key)
        preview_text = await _run(
            manager.preview_system_prompt,
            principal,
            csv_bytes,
            column_mapping,
            extra_columns or None,
        )
        return templates.TemplateResponse(
            request,
            "survey/partials/prompt_preview.html",
            {"request": request, "preview_text": preview_text},
        )
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.error("Failed to preview prompt", exc_info=True)
        return templates.TemplateResponse(
            request,
            "survey/partials/prompt_preview.html",
            {
                "request": request,
                "error": user_message_for(
                    e, default="プロンプトのプレビュー生成に失敗しました"
                ),
            },
        )


async def _import(request: Request, *, dwh: bool) -> Any:
    """Mapping confirmed -> Parquet -> registered with its name and scope."""
    principal = _principal(request)
    manager = get_dataset_manager()
    manager.ensure_manageable(principal)
    form = await request.form()
    temp_key = str(form.get("temp_key", ""))
    filename = str(form.get("filename", ""))
    name = str(form.get("dataset_name", "")).strip() or Path(filename).stem
    if dwh and not name:
        name = "dwh_segment"
    column_mapping = _standard_mapping(form)
    extra_columns = _extra_columns(form)
    scope = frozenset(str(g) for g in form.getlist("departments") if g)

    try:
        csv_bytes = await _run(manager.download_temp_file, principal, temp_key)
        result = await _run(
            lambda: manager.upload_custom_dataset(
                principal,
                csv_bytes,
                name,
                column_mapping,
                extra_columns,
                shared_group_ids=scope,
                avoid_name_conflict=dwh,
            )
        )
        # 一時ファイルを削除（best-effort: 失敗しても処理は継続）
        try:
            await _run(manager.delete_temp_file, principal, temp_key)
        except Exception:
            logger.warning("Failed to delete a temporary upload", exc_info=True)
        context = await _persona_data_context(principal)
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.error("Failed to import custom persona data", exc_info=True)
        # 件数不足・形式不正・名前の重複はカタログ文言で具体的に伝える（Issue #118）。
        return _upload_result(
            request,
            user_message_for(
                e, default="データセットの保存に失敗しました。再度お試しください。"
            ),
        )
    return templates.TemplateResponse(
        request,
        "survey/partials/custom_upload_result.html",
        {"request": request, "success": True, "result": result, **context},
    )


@router.post("/persona-data/confirm-mapping", response_class=HTMLResponse)
async def upload_custom_step2(request: Request) -> Any:
    """Step2: マッピング確定 → Parquet変換 → 名前と利用範囲を付けて登録"""
    return await _import(request, dwh=False)


@router.get("/persona-data/custom/{data_id}/detail", response_class=HTMLResponse)
async def custom_dataset_detail(request: Request, data_id: str) -> Any:
    """カスタムデータのマッピング情報（使える人だけ）"""
    manager = get_dataset_manager()
    record = await _run(manager.get_custom_persona_data, _principal(request), data_id)
    return templates.TemplateResponse(
        request,
        "survey/partials/custom_dataset_detail.html",
        {
            "request": request,
            "record": record,
            "standard_columns": manager.get_standard_columns(),
        },
    )


@router.delete("/persona-data/custom/{data_id}", response_class=HTMLResponse)
async def delete_custom_dataset(request: Request, data_id: str) -> Any:
    """カスタムデータを削除（admin だけ）"""
    principal = _principal(request)
    manager = get_dataset_manager()
    try:
        await _run(manager.delete_custom_dataset, principal, data_id)
        context = await _persona_data_context(principal)
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.error("Failed to delete custom persona data", exc_info=True)
        return toast_response(e, default="削除に失敗しました")
    return templates.TemplateResponse(
        request,
        "survey/partials/custom_dataset_list.html",
        {"request": request, **context},
    )


@router.get("/persona-data/custom/{data_id}/scope", response_class=HTMLResponse)
async def custom_dataset_scope_dialog(request: Request, data_id: str) -> Any:
    """カスタムデータの利用範囲のダイアログ（admin だけ、M10）"""
    dialog = await _run(
        get_dataset_manager().scope_dialog, _principal(request), data_id
    )
    return asset_scope_views.dialog_response(
        request, dialog, f"/survey/persona-data/custom/{data_id}/scope"
    )


@router.post("/persona-data/custom/{data_id}/scope", response_class=HTMLResponse)
async def save_custom_dataset_scope(request: Request, data_id: str) -> Any:
    """カスタムデータの利用範囲の保存（開いたときとの差分だけ）"""
    change = await asset_scope_views.change_from_request(request)
    try:
        await _run(
            get_dataset_manager().update_scope, _principal(request), data_id, change
        )
    except CodedError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.warning("Custom persona data scope save failed", exc_info=True)
        return asset_scope_views.error_response(
            request, user_message_for(e), error_kind_of(e)
        )
    return asset_scope_views.saved_response(f"#{CUSTOM_LIST_ID}")


# =========================================================================
# データ分析エージェント連携（DWHセグメント抽出、admin だけ）
# =========================================================================


def _survey_sse_event(event_type: str, data: str) -> str:
    """SSEイベントをフォーマット"""
    lines = data.split("\n") if data else [""]
    data_lines = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event_type}\n{data_lines}\n\n"


@router.post("/persona-data/dwh-extract")
async def dwh_extract(request: Request) -> Any:
    """データ分析エージェント連携: セグメント抽出（SSEストリーミング）"""
    import queue as queue_mod

    principal = _principal(request)
    manager = get_dataset_manager()
    # 流し始める前に止める（流し始めたあとは状態コードを返せない）
    manager.ensure_manageable(principal)
    try:
        # 閉域対応 U3 BR3.7: 使ってよくない環境では error イベント 1 つだけを返す
        manager.ensure_data_agent_allowed(principal)
    except SurveyDatasetManagerError as e:
        refusal = _survey_sse_event("error", user_message_for(e))
        return StreamingResponse(iter([refusal]), media_type="text/event-stream")
    form = await request.form()
    condition = str(form.get("condition", "")).strip()

    if not condition:

        async def err_gen() -> Any:
            yield _survey_sse_event("error", "抽出条件を入力してください")

        return StreamingResponse(err_gen(), media_type="text/event-stream")

    event_queue: queue_mod.Queue = queue_mod.Queue()

    def _run_extraction() -> dict:
        return manager.extract_segment_from_dwh(principal, condition, event_queue)

    async def event_generator() -> Any:
        yield _survey_sse_event("status", "データ分析エージェントに接続中...")

        future = executor.submit(_run_extraction)

        # 待つのはスレッドで行う（イベントループの上で待つと他の要求が止まる）
        while not future.done():
            try:
                evt = await asyncio.to_thread(event_queue.get, timeout=0.3)
                evt_type = evt.get("type", "")
                content = evt.get("content", "")
                if evt_type == "tool_call":
                    yield _survey_sse_event(
                        "thinking",
                        json.dumps(
                            {
                                "type": "tool_call",
                                "content": content,
                                "detail": evt.get("detail", ""),
                            },
                            ensure_ascii=False,
                        ),
                    )
                elif evt_type == "tool_result" and content:
                    yield _survey_sse_event(
                        "thinking",
                        json.dumps(
                            {
                                "type": "tool_result",
                                "tool_name": evt.get("tool_name", ""),
                                "content": content[:500],
                            },
                            ensure_ascii=False,
                        ),
                    )
                elif evt_type == "status" and content:
                    yield _survey_sse_event("status", content)
            except queue_mod.Empty:
                yield _survey_sse_event("keepalive", "")

        # flush remaining events
        while not event_queue.empty():
            try:
                event_queue.get_nowait()
            except queue_mod.Empty:
                break

        try:
            result = future.result()
            temp_key = f"persona-dataset/temp/dwh-{uuid.uuid4().hex[:8]}.csv"
            await _run(
                manager.upload_temp_file, principal, result["csv_bytes"], temp_key
            )

            yield _survey_sse_event(
                "complete",
                json.dumps(
                    {
                        "temp_key": temp_key,
                        "dataset_name": result.get("suggested_name", "dwh_segment"),
                        "row_count": result["row_count"],
                        "columns": result["columns"],
                        "samples": result["samples"],
                    },
                    ensure_ascii=False,
                ),
            )
            yield _survey_sse_event("done", "")

        except (SurveyDatasetValidationError, SurveyDatasetManagerError) as e:
            logger.warning("DWH segment extraction error", exc_info=True)
            yield _survey_sse_event("error", user_message_for(e))
        except Exception as e:
            logger.exception("DWH segment extraction error")
            yield _survey_sse_event(
                "error",
                user_message_for(e, default="セグメント抽出中にエラーが発生しました。"),
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/persona-data/dwh-preview", response_class=HTMLResponse)
async def dwh_preview(request: Request) -> Any:
    """DWH抽出結果のプレビュー＋マッピングUIを返す"""
    principal = _principal(request)
    manager = get_dataset_manager()
    manager.ensure_manageable(principal)
    form = await request.form()
    temp_key = str(form.get("temp_key", ""))
    dataset_name = str(form.get("dataset_name", ""))

    try:
        csv_bytes = await _run(manager.download_temp_file, principal, temp_key)
        parsed = await _run(manager.parse_csv_columns, principal, csv_bytes)

        # LLMマッピング提案（標準カラム + その他カラム）
        suggestion = await _run(
            manager.suggest_column_mapping,
            principal,
            parsed["columns"],
            parsed["samples"],
        )
        # auto_mapping と suggested mapping をマージ（suggestedを優先）
        merged_mapping = {**parsed["auto_mapping"], **suggestion.get("mapping", {})}
        return await _column_mapping(
            request,
            principal,
            filename=dataset_name + ".csv",
            temp_key=temp_key,
            parsed=parsed,
            auto_mapping=merged_mapping,
            dataset_name=dataset_name,
            is_dwh=True,
            suggested_extra=suggestion.get("extra_columns", []),
        )
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.error("DWH preview failed", exc_info=True)
        return _upload_result(
            request,
            user_message_for(
                e, default="プレビューの生成に失敗しました。再度お試しください。"
            ),
        )


@router.post("/persona-data/dwh-confirm", response_class=HTMLResponse)
async def dwh_confirm(request: Request) -> Any:
    """DWH抽出データのマッピング確定 → Parquet変換 → 登録（重なる名前は (2) 以降）"""
    return await _import(request, dwh=True)


# =========================================================================
# テンプレート管理
# =========================================================================


@router.get("/templates", response_class=HTMLResponse)
async def templates_list(
    request: Request,
    tab: Optional[str] = None,
    search: Optional[str] = None,
    cursor: Optional[str] = None,
    append: bool = False,
) -> Any:
    """テンプレート一覧（タブ別、M6、BR1.2）"""
    search_query = (search or "").strip()
    try:
        listing = await _run(
            get_template_manager().list_templates,
            _principal(request),
            tab,
            cursor,
            search_query or None,
        )
        error = None
    except CodedError as e:
        logger.warning("Template list fetch error", exc_info=True)
        listing = None
        error = user_message_for(e, default="テンプレート一覧の取得に失敗しました")

    panel_only = _panel_only(request, TEMPLATE_PANEL_ID, append)
    if listing is None and panel_only:
        return _list_error(request, error or "")
    context = {
        "request": request,
        "title": "テンプレート一覧",
        "listing": listing,
        "tab": tab,
        "current_search": search_query,
        "is_append": append,
        "error": error,
    }
    if panel_only:
        return templates.TemplateResponse(
            request, "survey/partials/template_list.html", context
        )
    return templates.TemplateResponse(request, "survey/templates_list.html", context)


@router.get("/templates/new", response_class=HTMLResponse)
async def template_new(request: Request) -> Any:
    """テンプレート作成画面"""
    return templates.TemplateResponse(
        request,
        "survey/template_form.html",
        {"request": request, "title": "テンプレート作成", "template": None},
    )


# -------------------------------------------------------------------------
# アンケートAI生成（Issue #23）
# -------------------------------------------------------------------------


def _parse_ai_messages(payload: Any) -> list[dict[str, str]]:
    """リクエストbodyから会話履歴(messages)をパース。"""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="リクエストボディが不正です")
    raw = payload.get("messages")
    if not isinstance(raw, list):
        raise HTTPException(
            status_code=400, detail="messages はリストである必要があります"
        )
    normalized: list[dict[str, str]] = []
    for m in raw:
        if not isinstance(m, dict):
            raise HTTPException(status_code=400, detail="messages の要素が不正です")
        role = str(m.get("role", "")).strip()
        content = str(m.get("content", "")).strip()
        if role not in ("user", "assistant") or not content:
            raise HTTPException(
                status_code=400, detail="messages の role/content が不正です"
            )
        normalized.append({"role": role, "content": content})
    return normalized


_AI_INPUT_HINT = (
    "入力内容が不正です。1メッセージは2000文字以内、会話履歴は40件までにしてください。"
)


@router.post("/templates/ai-chat")
async def template_ai_chat(request: Request) -> JSONResponse:
    """AIアンケート作成ヒアリングの1ターンを処理する。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSONの解析に失敗しました")
    messages = _parse_ai_messages(payload)

    manager = get_template_manager()
    try:
        assistant_message = await _run(
            manager.generate_ai_chat_response, _principal(request), messages
        )
    except SurveyTemplateValidationError as e:
        logger.info("AI chat validation error", exc_info=True)
        return JSONResponse(
            {"error": user_message_for(e, default=_AI_INPUT_HINT)}, status_code=400
        )
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.error("AI chat failed", exc_info=True)
        return JSONResponse(
            {"error": user_message_for(e, default="AIの応答生成に失敗しました")},
            status_code=500,
        )
    return JSONResponse({"assistant_message": assistant_message})


@router.post("/templates/ai-generate")
async def template_ai_generate(request: Request) -> JSONResponse:
    """AIがアンケート設問ドラフトを生成する。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSONの解析に失敗しました")
    messages = _parse_ai_messages(payload)

    manager = get_template_manager()
    try:
        result = await _run(
            manager.generate_ai_questions_draft, _principal(request), messages
        )
    except SurveyTemplateValidationError as e:
        logger.info("AI draft validation error", exc_info=True)
        return JSONResponse(
            {"error": user_message_for(e, default=_AI_INPUT_HINT)}, status_code=400
        )
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.error("AI draft generation failed", exc_info=True)
        return JSONResponse(
            {
                "error": user_message_for(
                    e,
                    default=(
                        "設問ドラフトの生成に失敗しました。"
                        "もう少し会話を続けてから再度お試しください。"
                    ),
                )
            },
            status_code=500,
        )
    return JSONResponse(result)


def _signed_preview_url(principal: Principal, file_path: str) -> str:
    """A preview URL through FileAttachments (the parent template's check, BR1.9).

    Empty when the image cannot be signed (deleted, or not readable).
    """
    try:
        return get_file_manager().preview_url_for_path(principal, file_path)
    except CodedError:
        logger.warning("Template image preview is unavailable", exc_info=True)
        return ""


@router.get("/templates/{template_id}/edit", response_class=HTMLResponse)
async def template_edit(request: Request, template_id: str) -> Any:
    """テンプレート編集画面（管理できる人だけ。共有された人は 403）"""
    principal = _principal(request)
    tmpl = await _run(
        get_template_manager().get_template_for_edit, principal, template_id
    )
    # 既存画像のプレビューURLは親のテンプレートで判定して作る（任意のパスは受けない）
    images_with_urls = []
    for img in tmpl.images:
        d = img.to_dict()
        d["preview_url"] = await _run(_signed_preview_url, principal, img.file_path)
        images_with_urls.append(d)
    return templates.TemplateResponse(
        request,
        "survey/template_form.html",
        {
            "request": request,
            "title": "テンプレート編集",
            "template": tmpl,
            "questions_json": [q.to_dict() for q in tmpl.questions],
            "images_json": images_with_urls,
        },
    )


# =========================================================================
# アンケート開始
# =========================================================================


@router.get("/start", response_class=HTMLResponse)
async def survey_start_page(
    request: Request,
    tab: Optional[str] = None,
    search: Optional[str] = None,
    template_id: Optional[str] = None,
) -> Any:
    """アンケート開始画面（読めるテンプレートと使えるデータソースだけ）

    ``template_id``（一覧の「使う」）は、読めるときだけ選んでおく。
    """
    principal = _principal(request)
    search_query = (search or "").strip()
    template_manager = get_template_manager()
    try:
        listing = await _run(
            template_manager.list_templates,
            principal,
            tab,
            None,
            search_query or None,
        )
        error = None
    except CodedError as e:
        logger.warning("Template list fetch error", exc_info=True)
        listing = None
        error = user_message_for(e, default="テンプレート一覧の取得に失敗しました")

    chosen = None
    if template_id and listing is not None:
        try:
            chosen = await _run(template_manager.get_template, principal, template_id)
        except CodedError:
            logger.info("The chosen template is not readable", exc_info=True)
    if chosen is not None and listing is not None:
        if all(t.id != chosen.id for t in listing.items):
            listing = replace(listing, items=[chosen, *listing.items])

    template_context = {
        "request": request,
        "listing": listing,
        "tab": tab,
        "current_search": search_query,
        "selected_template": chosen.id if chosen is not None else None,
        "error": error,
    }
    if _panel_only(request, START_PANEL_ID):
        if listing is None:
            return _list_error(request, error or "")
        return templates.TemplateResponse(
            request, "survey/partials/start_template_select.html", template_context
        )

    ds_mgr = get_dataset_manager()
    filter_values: dict = {}
    nemotron_available = False
    datasource_counts: dict[str, int] = {}
    custom_data: tuple = ()
    try:
        nemotron_available = (await _run(ds_mgr.check_nemotron_status)).available
        custom_data = (await _run(ds_mgr.list_persona_data, principal)).custom
        sources = (["nemotron"] if nemotron_available else []) + [
            f"custom:{d.id}" for d in custom_data
        ]
        if sources:
            filter_values = await _run(
                ds_mgr.get_available_filter_values, principal, sources[0]
            )
        for source in sources:
            datasource_counts[source] = await _run(
                ds_mgr.get_datasource_count, principal, source
            )
    except Exception:
        logger.warning("Failed to get the persona data sources", exc_info=True)

    return templates.TemplateResponse(
        request,
        "survey/start.html",
        {
            **template_context,
            "title": "アンケート開始",
            "filter_values": filter_values,
            "custom_data": custom_data,
            "nemotron_available": nemotron_available,
            "datasource_counts": datasource_counts,
        },
    )


def _clean_filters(raw: dict) -> dict | None:
    """フィルタJSONから空値を除去して返す。"""
    return SurveyExecutionManager.normalize_filters(raw)


@router.get("/filter-options", response_class=HTMLResponse)
async def filter_options(request: Request) -> Any:
    """データソースに応じたフィルタ選択肢を返す（使えるデータソースだけ）"""
    datasource = request.query_params.get("datasource", "nemotron")
    manager = get_dataset_manager()
    filter_values = {}
    try:
        filter_values = await _run(
            manager.get_available_filter_values, _principal(request), datasource
        )
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.warning("Failed to get filter values", exc_info=True)
    return templates.TemplateResponse(
        request,
        "survey/partials/filter_fields.html",
        {"request": request, "filter_values": filter_values},
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview_personas(request: Request) -> Any:
    """フィルタ条件に基づくペルソナプレビュー"""
    principal = _principal(request)
    form = await request.form()
    filters_json = str(form.get("filters_json", "{}"))
    datasource = str(form.get("datasource", "nemotron"))

    try:
        raw = json.loads(filters_json) if filters_json.strip() else {}
        filters = _clean_filters(raw) if raw else None
    except json.JSONDecodeError:
        filters = None

    manager = get_dataset_manager()

    def _collect_preview() -> tuple[int, int, dict]:
        """DuckDB/S3 への同期クエリ3本をまとめてスレッド上で実行する。"""
        total = manager.get_filtered_count(principal, None, datasource)
        count = (
            manager.get_filtered_count(principal, filters, datasource)
            if filters
            else total
        )
        stats = manager.get_preview_stats(principal, filters, datasource)
        return total, count, stats

    try:
        # DuckDB/httpfs 経由の S3 参照は同期処理。イベントループ上で実行すると
        # S3 遅延時に他リクエストまで停止する（Issue #118）。
        total, count, stats = await _run(_collect_preview)
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.error("Preview failed", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_inline.html",
            {
                "request": request,
                "error": user_message_for(e, default="プレビューに失敗しました"),
            },
        )

    return templates.TemplateResponse(
        request,
        "survey/partials/filter_preview.html",
        {
            "request": request,
            "count": count,
            "total": total,
            "stats": stats,
            "has_filter": filters is not None,
        },
    )


# =========================================================================
# 結果
# =========================================================================


@router.get("/results", response_class=HTMLResponse)
async def results_list(
    request: Request,
    tab: Optional[str] = None,
    search: Optional[str] = None,
    cursor: Optional[str] = None,
    append: bool = False,
) -> Any:
    """結果一覧（タブ別、M6、BR1.2）。名前は実行したときの写しで出す"""
    search_query = (search or "").strip()
    try:
        listing = await _run(
            get_execution_manager().list_surveys,
            _principal(request),
            tab,
            cursor,
            search_query or None,
        )
        error = None
    except CodedError as e:
        logger.warning("Survey list fetch error", exc_info=True)
        listing = None
        error = user_message_for(e, default="アンケート結果一覧の取得に失敗しました")

    panel_only = _panel_only(request, RESULT_PANEL_ID, append)
    if listing is None and panel_only:
        return _list_error(request, error or "")
    context = {
        "request": request,
        "title": "アンケート結果一覧",
        "listing": listing,
        "tab": tab,
        "current_search": search_query,
        "is_append": append,
        "error": error,
    }
    if panel_only:
        return templates.TemplateResponse(
            request, "survey/partials/result_list.html", context
        )
    return templates.TemplateResponse(request, "survey/results_list.html", context)


@router.get("/results/{survey_id}", response_class=HTMLResponse)
async def result_detail(request: Request, survey_id: str) -> Any:
    """結果詳細画面（操作は管理できる人だけ、BR2.7〜BR2.11）"""
    detail = await _run(
        get_execution_manager().get_survey_detail, _principal(request), survey_id
    )
    survey = detail.survey
    snapshot = survey.template_snapshot
    images = list(snapshot.images) if snapshot is not None else []
    image_urls = {
        img.id: f"/survey/results/{survey.id}/images/{img.id}" for img in images
    }
    # 実行はバックグラウンドで走るため失敗理由はコードとしてDBに残っている。
    # 文言への解決はここ（表示層）で行う（Issue #118）。
    execution_error = (
        user_message_for_code(survey.error_code, survey.error_context)
        if survey.status == "error"
        else None
    )
    return templates.TemplateResponse(
        request,
        "survey/result_detail.html",
        {
            "request": request,
            "title": "アンケート結果詳細",
            "survey": survey,
            "detail": detail,
            "images": images,
            "image_urls": image_urls,
            "execution_error": execution_error,
        },
    )


@router.delete("/results/{survey_id}")
async def delete_survey(
    request: Request, survey_id: str, redirect: bool = False
) -> Any:
    """アンケート削除（管理できる人だけ）。詳細画面からは一覧へ戻す"""
    manager = get_execution_manager()
    try:
        await _run(manager.delete_survey, _principal(request), survey_id)
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.error("Failed to delete survey", exc_info=True)
        return toast_response(e, default="削除に失敗しました")
    if redirect:
        return Response(status_code=200, headers={"HX-Redirect": "/survey/results"})
    return Response(status_code=200)


@router.get("/results/{survey_id}/images/{image_id}")
async def result_image(request: Request, survey_id: str, image_id: str) -> Any:
    """結果の写しの画像（読める人だけ、署名付きURLへリダイレクト、BR2.8）"""
    url = await _run(
        get_execution_manager().get_result_image_url,
        _principal(request),
        survey_id,
        image_id,
    )
    return RedirectResponse(url=url)


# =========================================================================
# 画像アップロード
# =========================================================================


@router.post("/templates/upload-image")
async def upload_survey_image(request: Request, file: UploadFile = File(...)) -> Any:
    """アンケートテンプレート用画像をアップロード（利用者のキー、24 時間の期限、BR5.1）"""
    principal = _principal(request)
    try:
        file_manager = get_file_manager()
        file_content = await file.read()
        metadata = await _run(
            file_manager.upload_survey_image,
            principal,
            file_content,
            file.filename or "",
        )
        return JSONResponse(
            {
                "file_id": metadata.file_id,
                "file_path": metadata.file_path,
                "mime_type": metadata.mime_type,
                "original_filename": metadata.original_filename,
                "preview_url": await _run(
                    _signed_preview_url, principal, metadata.file_path
                ),
            }
        )
    except Exception as e:
        logger.error("Survey image upload error", exc_info=True)
        return JSONResponse(
            {"error": user_message_for(e, default="画像のアップロードに失敗しました")},
            status_code=400,
        )


@router.get("/image-preview")
async def image_preview(request: Request, file_path: str) -> Any:
    """画像のプレビューURL（署名付きURL）を返す。

    読めるアップロード・読めるテンプレートの画像のパスだけ（BR1.9、BR5.8）。任意の
    パス・他人のパスは 404 で URL を作らない（共通の例外ハンドラが答える）。
    """
    url = await _run(
        get_file_manager().preview_url_for_path, _principal(request), file_path
    )
    return JSONResponse({"url": url})


# =========================================================================
# テンプレートCRUD操作
# =========================================================================


def _form_template(form: Any) -> tuple[str, list[Question], list[TemplateImage]] | None:
    """Name, questions and images of the template form (``None``: bad questions)."""
    name = str(form.get("name", "")).strip()
    try:
        questions_data = json.loads(str(form.get("questions_json", "[]")))
    except json.JSONDecodeError:
        return None
    images_json = str(form.get("images_json", "[]"))
    try:
        images_data = json.loads(images_json) if images_json.strip() else []
    except json.JSONDecodeError:
        images_data = []
    return name, _parse_questions(questions_data), _parse_images(images_data)


def _questions_invalid(request: Request) -> Any:
    return _template_save_error(
        request,
        user_message_for_code(ErrorCode.SURVEY_TEMPLATE_QUESTIONS_INVALID),
        400,
    )


@router.post("/templates", response_class=HTMLResponse)
async def create_template(request: Request) -> Any:
    """テンプレート保存（作った人が所有者になる、BR1.1）"""
    parsed = _form_template(await request.form())
    if parsed is None:
        return _questions_invalid(request)
    name, questions, images = parsed

    manager = get_template_manager()
    try:
        # 画像は保存する人のアップロードとして紐付ける（BR5.3）
        await _run(
            manager.create_template,
            _principal(request),
            name,
            questions,
            images or None,
        )
    except SurveyTemplateManagerError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            # 他人の・期限切れの画像は、ないものと同じ 404 のトースト
            raise
        if isinstance(e, SurveyTemplateValidationError):
            logger.warning("Template validation error", exc_info=True)
            return _template_save_error(request, user_message_for(e), 400)
        logger.error("Template save error", exc_info=True)
        return _template_save_error(
            request, user_message_for(e, default="保存に失敗しました"), 500
        )

    # 成功時はテンプレート一覧にリダイレクト（htmx対応）
    response = HTMLResponse(content="", status_code=200)
    response.headers["HX-Redirect"] = "/survey/templates"
    return response


def _template_save_error(request: Request, message: str, status_code: int) -> Any:
    """Inline error of a template save (the catalog wording, 400 or 500)."""
    return mark_renderable(
        templates.TemplateResponse(
            request,
            "partials/error_inline.html",
            {"request": request, "error": message},
            status_code=status_code,
        )
    )


@router.put("/templates/{template_id}", response_class=HTMLResponse)
async def update_template(request: Request, template_id: str) -> Any:
    """テンプレート更新（管理できる人だけ。変えた欄だけを書く）"""
    parsed = _form_template(await request.form())
    if parsed is None:
        return _questions_invalid(request)
    name, questions, images = parsed

    manager = get_template_manager()
    try:
        # 新しく足した画像だけを更新する人のアップロードとして紐付ける（BR5.3）
        await _run(
            manager.update_template,
            _principal(request),
            template_id,
            name,
            questions,
            images or None,
        )
    except SurveyTemplateManagerError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        if isinstance(e, SurveyTemplateValidationError):
            logger.warning("Template validation error", exc_info=True)
            return _template_save_error(request, user_message_for(e), 400)
        logger.error("Template update error", exc_info=True)
        return _template_save_error(
            request, user_message_for(e, default="更新に失敗しました"), 500
        )

    response = HTMLResponse(content="", status_code=200)
    response.headers["HX-Redirect"] = "/survey/templates"
    return response


@router.delete("/templates/{template_id}", response_class=HTMLResponse)
async def delete_template(request: Request, template_id: str) -> Any:
    """テンプレート削除（管理できる人だけ。共有も一緒に消す）"""
    manager = get_template_manager()
    try:
        await _run(manager.delete_template, _principal(request), template_id)
    except SurveyTemplateManagerError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.error("Template delete error", exc_info=True)
        return toast_response(e, default="削除に失敗しました")
    # 削除成功 - カードを空にする
    return HTMLResponse(content="")


# =========================================================================
# アンケート実行
# =========================================================================


def _execute_error(request: Request, message: str, status_code: int) -> Any:
    """The error under the start form (``#execute-error``)."""
    # HX-Retarget はスワップ判定より前に処理されるが、それだけでは非2xx本文が
    # スワップされないため mark_renderable が必要（Issue #117）
    response = mark_renderable(
        templates.TemplateResponse(
            request,
            "partials/error_inline.html",
            {"request": request, "error": message},
            status_code=status_code,
        )
    )
    response.headers["HX-Retarget"] = "#execute-error"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@router.post("/execute", response_class=HTMLResponse)
async def execute_survey(request: Request) -> Any:
    """アンケート実行（読めるテンプレート・使えるデータだけ。保存後に裏で実行）"""
    principal = _principal(request)
    form = await request.form()
    template_id = str(form.get("template_id", ""))
    name = str(form.get("name", "")).strip()
    description = str(form.get("description", "")).strip()
    persona_count_str = str(form.get("persona_count", "100"))
    filters_json = str(form.get("filters_json", "{}"))
    datasource = str(form.get("datasource", "nemotron"))

    try:
        persona_count = int(persona_count_str)
    except (ValueError, TypeError):
        return _execute_error(
            request,
            user_message_for_code(ErrorCode.SURVEY_PERSONA_COUNT_INVALID),
            400,
        )

    try:
        raw = json.loads(filters_json) if filters_json.strip() else None
        filters = _clean_filters(raw) if raw else None
    except json.JSONDecodeError:
        filters = None

    manager = get_execution_manager()

    # 1. アンケートレコードを作成（判定とバリデーションを含む）
    #    件数検証が DuckDB/httpfs 経由で S3 を同期参照するため、スレッドへ
    #    委譲する（Issue #118）。
    try:
        start = await _run(
            lambda: manager.create_survey(
                principal,
                template_id,
                name or None,
                description,
                persona_count,
                filters,
                datasource,
            )
        )
    except SurveyExecutionValidationError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.warning("Survey execution validation error", exc_info=True)
        return _execute_error(request, user_message_for(e), 400)
    except SurveyExecutionManagerError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.error("Survey creation error", exc_info=True)
        # 件数取得のS3/DuckDB障害など一時的な失敗（TRANSIENT）は、画面を書き換えず
        # トーストで通知して入力を保持する（error-catalog.md「TRANSIENT の表示」）。
        if is_transient(e):
            return toast_response(e, default="アンケート作成に失敗しました")
        return _execute_error(
            request, user_message_for(e, default="アンケート作成に失敗しました"), 500
        )

    # 2. バックグラウンドで実行を開始（デフォルトスレッドプールで実行し、executorを占有しない）。
    #    失敗は実行の中でアンケートに記録される（例外を投げない）
    asyncio.create_task(asyncio.to_thread(start.run))

    # 3. 開始メッセージを表示し、結果一覧へ自動遷移
    return templates.TemplateResponse(
        request,
        "survey/partials/survey_submitted.html",
        {"request": request, "survey_name": start.survey.name},
    )


# =========================================================================
# 結果取得・分析
# =========================================================================


@router.get("/results/{survey_id}/download")
async def download_csv(request: Request, survey_id: str) -> Any:
    """CSVダウンロード（読める人だけ、署名付きURLへリダイレクト）"""
    presigned_url = await _run(
        get_execution_manager().get_download_url,
        _principal(request),
        survey_id,
        300,
    )
    return RedirectResponse(url=presigned_url)


def _analysis_error(request: Request, message: str) -> Any:
    return mark_renderable(
        templates.TemplateResponse(
            request,
            "partials/error_banner.html",
            {"request": request, "error": message},
            status_code=400,
        )
    )


@router.get("/results/{survey_id}/personas", response_class=HTMLResponse)
async def persona_statistics(request: Request, survey_id: str) -> Any:
    """調査対象ペルソナ統計データ取得"""
    manager = get_analysis_manager()
    try:
        stats = await _run(
            manager.get_persona_statistics, _principal(request), survey_id
        )
    except SurveyAnalysisManagerError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.warning("Survey analysis data retrieval error", exc_info=True)
        return _analysis_error(request, user_message_for(e))
    return templates.TemplateResponse(
        request,
        "survey/partials/persona_statistics.html",
        {"request": request, "stats": stats},
    )


@router.get("/results/{survey_id}/visual", response_class=HTMLResponse)
async def visual_analysis(request: Request, survey_id: str) -> Any:
    """ビジュアル分析データ取得"""
    manager = get_analysis_manager()
    try:
        data = await _run(manager.get_visual_analysis, _principal(request), survey_id)
    except SurveyAnalysisManagerError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.warning("Survey analysis data retrieval error", exc_info=True)
        return _analysis_error(request, user_message_for(e))
    return templates.TemplateResponse(
        request,
        "survey/partials/visual_analysis.html",
        {"request": request, "analysis": data},
    )


@router.get("/results/{survey_id}/report/generate")
async def generate_report_stream(request: Request, survey_id: str) -> Any:
    """インサイトレポートをSSEストリーミングで生成し、完了時に自動保存する

    判定（管理できる人だけ、BR2.11）は流し始める前に済ませ、読めない・ないものは
    404、共有された人は 403 を共通の例外ハンドラが返す。
    """
    principal = _principal(request)
    manager = get_analysis_manager()
    chunks = await _run(manager.generate_insight_report_streaming, principal, survey_id)

    def stream_generator() -> Any:
        try:
            full_content = []
            for chunk in chunks:
                full_content.append(chunk)
                data = json.dumps(
                    {"type": "chunk", "content": chunk}, ensure_ascii=False
                )
                yield f"data: {data}\n\n"

            # 自動保存
            manager.save_insight_report(principal, survey_id, "".join(full_content))
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except SurveyAnalysisManagerError:
            logger.warning("Report generation error", exc_info=True)
            data = json.dumps(
                {"type": "error", "message": "レポートの生成に失敗しました"},
                ensure_ascii=False,
            )
            yield f"data: {data}\n\n"
        except Exception as e:
            logger.exception("Unexpected error during report generation")
            data = json.dumps(
                {
                    "type": "error",
                    "message": user_message_for(
                        e, default="レポートの生成に失敗しました"
                    ),
                },
                ensure_ascii=False,
            )
            yield f"data: {data}\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# =========================================================================
# ヘルパー関数
# =========================================================================


def _parse_questions(questions_data: list) -> list[Question]:
    """フォームから送信された質問データをQuestionオブジェクトに変換する"""
    questions = []
    for q in questions_data:
        q_type = q.get("question_type", "free_text")
        text = q.get("text", "")
        if q_type == "multiple_choice":
            options = q.get("options", [])
            allow_multiple = bool(q.get("allow_multiple", False))
            max_selections = int(q.get("max_selections", 0))
            questions.append(
                Question.create_multiple_choice(
                    text=text,
                    options=options,
                    allow_multiple=allow_multiple,
                    max_selections=max_selections,
                )
            )
        elif q_type == "scale_rating":
            questions.append(Question.create_scale_rating(text=text))
        else:
            questions.append(Question.create_free_text(text=text))
    return questions


def _parse_images(images_data: list) -> list[TemplateImage]:
    """フォームから送信された画像データをTemplateImageオブジェクトに変換する"""
    images = []
    for img in images_data:
        images.append(
            TemplateImage(
                id=img.get("id", ""),
                name=img.get("name", ""),
                file_path=img.get("file_path", ""),
                mime_type=img.get("mime_type", ""),
                original_filename=img.get("original_filename", ""),
            )
        )
    return images
