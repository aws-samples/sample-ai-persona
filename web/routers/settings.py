"""
System settings router - dataset management, MCP settings, knowledge base management

Settings are admin-only (settings:manage). The Manager decides, and members get
a 404 from the shared exception handler. Also the usage-scope dialog of organization assets.
"""

import asyncio
import html
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from src.managers.dataset_manager import DatasetManager, DatasetManagerError
from src.managers.settings_manager import SettingsManager, SettingsManagerError
from src.models.authz import Principal
from src.models.dataset import DatasetColumn
from src.models.errors import CodedError, ErrorCode, ErrorKind
from src.models.knowledge_base import (
    KB_TYPE_LABELS,
    KB_TYPE_RECOMMENDED,
    KB_TYPE_SELF_MANAGED,
)
from src.models.sharing import ScopedAssets
from web import asset_scope_views
from web.error_messages import (
    error_kind_of,
    error_payload,
    is_correctable,
    mark_renderable,
    toast_response,
    user_message_for,
    user_message_for_code,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
# Display helper: display name of a KB type (used for the option order and the list)
templates.env.globals["kb_type_labels"] = KB_TYPE_LABELS
templates.env.globals["kb_type_recommended"] = KB_TYPE_RECOMMENDED

executor = ThreadPoolExecutor(max_workers=4)

#: Kinds answered by the common exception handler (404 / 403), never by the screen.
_ACCESS_KINDS = (ErrorKind.NOT_FOUND, ErrorKind.FORBIDDEN, ErrorKind.UNAUTHORIZED)

#: The list each asset kind reloads after a scope save (its ``data-reload-url``).
_SCOPE_LISTS = {"datasets": "#dataset-list", "knowledge-bases": "#kb-list"}

# Singleton managers
_dataset_manager: Optional[DatasetManager] = None
_settings_manager: Optional[SettingsManager] = None


def get_dataset_manager() -> DatasetManager:
    global _dataset_manager
    if _dataset_manager is None:
        _dataset_manager = DatasetManager()
    return _dataset_manager


def get_settings_manager() -> SettingsManager:
    global _settings_manager
    if _settings_manager is None:
        _settings_manager = SettingsManager()
    return _settings_manager


def _principal(request: Request) -> Principal:
    """The signed-in user that ``AuthMiddleware`` put on the request."""
    principal: Principal = request.state.principal
    return principal


async def _run(func: Callable[..., Any], *args: Any) -> Any:
    """Run a synchronous Manager call (DynamoDB / S3) in the worker pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, func, *args)


def _dataset_context(assets: ScopedAssets) -> dict[str, Any]:
    return {
        "datasets": [d.to_dict() for d in assets.items],
        "dataset_scopes": assets.scope_labels,
    }


def _kb_context(assets: ScopedAssets) -> dict[str, Any]:
    return {
        "knowledge_bases": [kb.to_dict() for kb in assets.items],
        "kb_scopes": assets.scope_labels,
    }


def _dataset_list(request: Request, assets: ScopedAssets) -> Any:
    return templates.TemplateResponse(
        request,
        "settings/partials/dataset_list.html",
        {"request": request, **_dataset_context(assets)},
    )


def _kb_list(request: Request, assets: ScopedAssets) -> Any:
    return templates.TemplateResponse(
        request,
        "settings/partials/kb_list.html",
        {"request": request, **_kb_context(assets)},
    )


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request) -> Any:
    """System settings page (admin only)."""
    principal = _principal(request)
    datasets = await _run(get_dataset_manager().get_datasets, principal)
    settings_manager = get_settings_manager()
    knowledge_bases = await _run(settings_manager.get_all_knowledge_bases, principal)
    data_agent = settings_manager.get_data_agent_settings(principal)

    return templates.TemplateResponse(
        request,
        "settings/index.html",
        {
            "request": request,
            "title": "システム設定",
            **_dataset_context(datasets),
            **_kb_context(knowledge_bases),
            **data_agent,
            "show_advanced_generation_settings": (
                settings_manager.is_advanced_generation_section_offered()
            ),
        },
    )


@router.get("/datasets", response_class=HTMLResponse)
async def list_datasets(request: Request) -> Any:
    """Dataset list."""
    datasets = await _run(get_dataset_manager().get_datasets, _principal(request))
    return _dataset_list(request, datasets)


@router.get("/datasets/form", response_class=HTMLResponse)
async def dataset_form(request: Request, dataset_id: Optional[str] = None) -> Any:
    """Dataset edit form."""
    principal = _principal(request)
    dataset_manager = get_dataset_manager()
    if dataset_id:
        dataset = await _run(dataset_manager.get_dataset, principal, dataset_id)
    else:
        # A new form goes through the same check as the list (admin only)
        await _run(dataset_manager.get_datasets, principal)
        dataset = None

    if dataset_id and not dataset:
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_banner.html",
                {
                    "request": request,
                    "error": user_message_for_code(ErrorCode.DATASET_NOT_FOUND),
                },
                status_code=404,
            )
        )

    return templates.TemplateResponse(
        request,
        "settings/partials/dataset_form.html",
        {"request": request, "dataset": dataset.to_dict() if dataset else None},
    )


@router.post("/datasets/analyze", response_class=HTMLResponse)
async def analyze_csv(request: Request, file: UploadFile = File(...)) -> Any:
    """Parse the schema of a CSV file."""
    principal = _principal(request)
    content = await file.read()
    dataset_manager = get_dataset_manager()
    try:
        # Format and size checks also come after the Manager's check (admin only)
        if not (file.filename or "").endswith(".csv"):
            await _run(dataset_manager.get_datasets, principal)
            return _inline_error(
                request,
                user_message_for_code(
                    ErrorCode.FILE_FORMAT_NOT_ALLOWED, {"allowed_formats": "CSV"}
                ),
            )
        if len(content) > 10 * 1024 * 1024:  # 10MB limit
            await _run(dataset_manager.get_datasets, principal)
            return _inline_error(
                request,
                user_message_for_code(ErrorCode.FILE_TOO_LARGE, {"max_size_mb": 10.0}),
            )
        columns, row_count = await _run(
            dataset_manager.analyze_schema, principal, content
        )
    except DatasetManagerError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        # A CSV whose empty/duplicate headers etc. diverge from the query column names
        # shows the reason inline near the form instead of a generic 500 (like other validation).
        logger.warning("CSV schema analysis rejected", exc_info=True)
        return _inline_error(request, user_message_for(e))

    # Convert DatasetColumn to dict
    columns_dict = [
        {"name": c.name, "data_type": c.data_type, "description": c.description}
        for c in columns
    ]

    return templates.TemplateResponse(
        request,
        "settings/partials/schema_preview.html",
        {
            "request": request,
            "columns": columns_dict,
            "row_count": row_count,
            "filename": file.filename,
        },
    )


def _inline_error(request: Request, message: str) -> Any:
    """An input error shown next to the form (400)."""
    return mark_renderable(
        templates.TemplateResponse(
            request,
            "partials/error_inline.html",
            {"request": request, "error": message},
            status_code=400,
        )
    )


def _parse_columns(columns_json: str) -> list[DatasetColumn]:
    return [
        DatasetColumn(
            name=c["name"],
            data_type=c["data_type"],
            description=c.get("description", ""),
        )
        for c in json.loads(columns_json)
    ]


@router.post("/datasets", response_class=HTMLResponse)
async def create_dataset(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(""),
    notes: str = Form(""),
    columns_json: str = Form(...),
) -> Any:
    """Create a dataset."""
    principal = _principal(request)
    dataset_manager = get_dataset_manager()
    try:
        content = await file.read()
        columns = _parse_columns(columns_json)
        await _run(
            lambda: dataset_manager.upload_csv(
                principal,
                file_content=content,
                filename=file.filename or "",
                name=name,
                description=description,
                notes=notes,
                columns=columns,
            )
        )
        datasets = await _run(dataset_manager.get_datasets, principal)
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        # Errors that a retry can fix are shown as a toast without clearing the input form
        logger.error("Dataset creation failed", exc_info=True)
        return toast_response(
            e, default="データセット作成に失敗しました", status_code=500
        )
    return _dataset_list(request, datasets)


@router.put("/datasets/{dataset_id}", response_class=HTMLResponse)
async def update_dataset(
    request: Request,
    dataset_id: str,
    name: str = Form(...),
    description: str = Form(""),
    notes: str = Form(""),
    columns_json: str = Form(...),
) -> Any:
    """Update a dataset."""
    principal = _principal(request)
    dataset_manager = get_dataset_manager()
    try:
        columns = _parse_columns(columns_json)
        dataset = await _run(
            lambda: dataset_manager.update_dataset(
                principal,
                dataset_id=dataset_id,
                name=name,
                description=description,
                notes=notes,
                columns=columns,
            )
        )
        datasets = (
            await _run(dataset_manager.get_datasets, principal) if dataset else None
        )
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        # Errors that a retry can fix are shown as a toast without clearing the input form
        logger.error("Dataset update failed", exc_info=True)
        return toast_response(
            e, default="データセット更新に失敗しました", status_code=500
        )

    if datasets is None:
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_banner.html",
                {
                    "request": request,
                    "error": user_message_for_code(ErrorCode.DATASET_NOT_FOUND),
                },
                status_code=404,
            )
        )
    return _dataset_list(request, datasets)


@router.delete("/datasets/{dataset_id}", response_class=HTMLResponse)
async def delete_dataset(request: Request, dataset_id: str) -> Any:
    """Delete a dataset."""
    principal = _principal(request)
    dataset_manager = get_dataset_manager()
    success = await _run(dataset_manager.delete_dataset, principal, dataset_id)

    if not success:
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_banner.html",
                {
                    "request": request,
                    "error": user_message_for_code(ErrorCode.DATASET_OPERATION_FAILED),
                },
                status_code=404,
            )
        )

    datasets = await _run(dataset_manager.get_datasets, principal)
    return _dataset_list(request, datasets)


# JSON API (used from other pages)
@router.get("/api/datasets")
async def api_list_datasets(request: Request) -> Any:
    """Dataset list API."""
    datasets = await _run(get_dataset_manager().get_datasets, _principal(request))
    return [d.to_dict() for d in datasets.items]


# ==================== Knowledge base management ====================


@router.get("/knowledge-bases", response_class=HTMLResponse)
async def list_knowledge_bases(request: Request) -> Any:
    """Knowledge base list."""
    knowledge_bases = await _run(
        get_settings_manager().get_all_knowledge_bases, _principal(request)
    )
    return _kb_list(request, knowledge_bases)


@router.post("/knowledge-bases", response_class=HTMLResponse)
async def create_knowledge_base(
    request: Request,
    knowledge_base_id: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    kb_type: str = Form(KB_TYPE_SELF_MANAGED),
) -> Any:
    """Register a knowledge base."""
    principal = _principal(request)
    settings_manager = get_settings_manager()
    try:
        await _run(
            settings_manager.create_knowledge_base,
            principal,
            knowledge_base_id,
            name,
            description,
            kb_type,
        )
        knowledge_bases = await _run(
            settings_manager.get_all_knowledge_bases, principal
        )
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        if is_correctable(e):
            # Bad input (e.g. an unknown kind). Not treated as a server failure
            logger.warning("KB registration was refused: %s", error_kind_of(e))
            return toast_response(
                e, default="ナレッジベースの登録に失敗しました", status_code=400
            )
        # Errors that a retry can fix are shown as a toast without clearing the input form
        logger.error("KB registration failed", exc_info=True)
        return toast_response(
            e, default="ナレッジベースの登録に失敗しました", status_code=500
        )
    return _kb_list(request, knowledge_bases)


@router.delete("/knowledge-bases/{kb_id}", response_class=HTMLResponse)
async def delete_knowledge_base(request: Request, kb_id: str) -> Any:
    """Delete a knowledge base."""
    principal = _principal(request)
    settings_manager = get_settings_manager()
    await _run(settings_manager.delete_knowledge_base, principal, kb_id)
    knowledge_bases = await _run(settings_manager.get_all_knowledge_bases, principal)
    return _kb_list(request, knowledge_bases)


# JSON API (used from other pages)
@router.get("/api/knowledge-bases")
async def api_list_knowledge_bases(request: Request) -> Any:
    """Knowledge base list API."""
    knowledge_bases = await _run(
        get_settings_manager().get_all_knowledge_bases, _principal(request)
    )
    return [kb.to_dict() for kb in knowledge_bases.items]


# ==================== Usage scope of organization assets ====================


def _scope_owner(kind: str) -> Any:
    """The Manager owning the usage scope of ``kind`` (datasets / knowledge-bases)."""
    return get_dataset_manager() if kind == "datasets" else get_settings_manager()


@router.get("/{kind}/{asset_id}/scope", response_class=HTMLResponse)
async def asset_scope_dialog(request: Request, kind: str, asset_id: str) -> Any:
    """Usage-scope dialog (admin only)."""
    if kind not in _SCOPE_LISTS:
        return Response(status_code=404)
    dialog = await _run(_scope_owner(kind).scope_dialog, _principal(request), asset_id)
    return asset_scope_views.dialog_response(
        request, dialog, f"/settings/{kind}/{asset_id}/scope"
    )


@router.post("/{kind}/{asset_id}/scope", response_class=HTMLResponse)
async def save_asset_scope(request: Request, kind: str, asset_id: str) -> Any:
    """Save the usage scope (changes only the difference from the departments when it was opened)."""
    if kind not in _SCOPE_LISTS:
        return Response(status_code=404)
    change = await asset_scope_views.change_from_request(request)
    try:
        await _run(
            _scope_owner(kind).update_scope, _principal(request), asset_id, change
        )
    except CodedError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.warning("Asset scope save failed", exc_info=True)
        return asset_scope_views.error_response(
            request, user_message_for(e), error_kind_of(e)
        )
    return asset_scope_views.saved_response(_SCOPE_LISTS[kind])


# ==================== Data analysis agent integration settings ====================


# --- Advanced generation (beta) section ---

#: The toast of a saved section.
_SAVED_TOAST = "保存しました。次に開いたページから反映されます"


def _advanced_section(
    request: Request,
    view: Any,
    *,
    status_code: int = 200,
    error: Optional[str] = None,
    form: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    return templates.TemplateResponse(
        request,
        "settings/partials/advanced_generation_settings.html",
        {"request": request, "view": view, "error": error, "form": form},
        status_code=status_code,
        headers=headers,
    )


@router.get("/advanced-generation", response_class=HTMLResponse)
async def advanced_generation_section(request: Request) -> Any:
    """Advanced generation section (404 without settings:manage)."""
    view = await _run(
        get_settings_manager().advanced_generation_section, _principal(request)
    )
    return _advanced_section(request, view)


@router.post("/advanced-generation", response_class=HTMLResponse)
async def save_advanced_generation(
    request: Request,
    audience_mode: str = Form(""),
    departments: list[str] = Form(default=[]),
    allow_detailed_analysis: str = Form(""),
    version: str = Form(""),
) -> Any:
    """Save the advanced generation section (403 without permission).

    Bad input gives 400 with the reason in the section and keeps the entered values. A version
    conflict gives 409 with the section showing the latest values. Success gives the new version's section and a toast.
    """
    principal = _principal(request)
    manager = get_settings_manager()
    form = {
        "audience_mode": audience_mode,
        "department_groups": list(departments),
        "allow_detailed_analysis": allow_detailed_analysis == "true",
        "version": version,
    }
    try:
        view = await _run(manager.save_advanced_generation_setting, principal, form)
    except SettingsManagerError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        code = error_payload(e)["error_code"]
        if code == ErrorCode.SETTINGS_VERSION_CONFLICT.value:
            # The latest values and version, read again
            logger.info("advanced generation setting lost its version")
            latest = await _run(manager.advanced_generation_section, principal)
            return mark_renderable(
                _advanced_section(
                    request,
                    latest.update(notice="conflict"),
                    status_code=409,
                    error=user_message_for(e),
                )
            )
        if is_correctable(e):
            logger.info("advanced generation setting refused")
            current = await _run(manager.advanced_generation_section, principal)
            return mark_renderable(
                _advanced_section(
                    request,
                    current,
                    status_code=400,
                    error=user_message_for(e),
                    form=form,
                )
            )
        logger.warning("advanced generation setting not saved", exc_info=True)
        return toast_response(e, status_code=500)
    trigger = {"showToast": {"message": _SAVED_TOAST, "type": "success"}}
    return _advanced_section(request, view, headers={"HX-Trigger": json.dumps(trigger)})


@router.get("/data-agent/status", response_class=HTMLResponse)
async def data_agent_status(request: Request) -> Any:
    """Get the data analysis agent settings state."""
    data_agent = get_settings_manager().get_data_agent_settings(_principal(request))
    return templates.TemplateResponse(
        request,
        "settings/partials/data_agent_settings.html",
        {"request": request, **data_agent},
    )


@router.post("/data-agent", response_class=HTMLResponse)
async def save_data_agent_settings(
    request: Request,
    runtime_arn: str = Form(""),
    region: str = Form("ap-northeast-1"),
    enabled: str = Form(""),
) -> Any:
    """Save the data analysis agent connection settings."""
    principal = _principal(request)
    settings_manager = get_settings_manager()
    settings_manager.save_data_agent_settings(
        principal,
        runtime_arn.strip() or None,
        region.strip() or "ap-northeast-1",
        enabled == "true",
    )

    return templates.TemplateResponse(
        request,
        "settings/partials/data_agent_settings.html",
        {
            "request": request,
            **settings_manager.get_data_agent_settings(principal),
            "save_message": "設定を保存しました",
        },
    )


@router.post("/data-agent/test", response_class=HTMLResponse)
async def test_data_agent_connection(request: Request) -> Any:
    """Test the data analysis agent connection."""
    settings_manager = get_settings_manager()
    try:
        result_text = await _run(
            settings_manager.test_data_agent_connection, _principal(request)
        )
        escaped = html.escape(result_text)
        return HTMLResponse(
            '<div class="text-sm bg-green-50 rounded p-3 border border-green-200">'
            '<div class="text-green-700 font-semibold mb-2">✅ 接続成功</div>'
            f'<div class="prose prose-sm max-w-none max-h-96 overflow-y-auto text-gray-700" id="data-agent-test-md">{escaped}</div>'
            "</div>"
            "<script>"
            "if(window.marked&&window.DOMPurify){"
            'var el=document.getElementById("data-agent-test-md");'
            "el.innerHTML=DOMPurify.sanitize(marked.parse(el.textContent,{breaks:true}));"
            "}"
            "</script>"
        )
    except SettingsManagerError as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            # FORBIDDEN gives a toast and 403 (the body does not replace the screen)
            raise
        logger.warning("Data analysis agent connection test failed", exc_info=True)
        message = user_message_for(e, default="接続テストに失敗しました")
        return HTMLResponse(
            f'<div class="text-sm text-red-600 bg-red-50 rounded p-2">接続失敗: {html.escape(message)}</div>'
        )
    except Exception as e:
        if error_kind_of(e) in _ACCESS_KINDS:
            raise
        logger.exception("Data analysis agent connection test error")
        message = user_message_for(e, default="接続テスト中にエラーが発生しました")
        return HTMLResponse(
            f'<div class="text-sm text-red-600 bg-red-50 rounded p-2">❌ 接続失敗: {html.escape(message)}</div>'
        )
