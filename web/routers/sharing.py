"""
Entry points for sharing.

The four kinds (personas, discussions, templates, survey results) share the same URL shape.
A kind with no registration gets 404. SharingManager -> ResourceSharing -> AccessPolicy decide;
this module only takes the input and shapes the response (HX-Trigger, errors inside the dialog).

- GET  /share/{resource_type}/{resource_id}/dialog ... contents of the dialog for one item
- GET  /share/{resource_type}/dialog?ids=...       ... contents of the bulk dialog
- POST /share/{resource_type}/{resource_id}        ... save for one item
- POST /share/{resource_type}                      ... bulk save

Unreadable and nonexistent targets get the same 404 from the shared exception handler.
"""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from src.managers.sharing_manager import (
    VANISHED_LABEL,
    ShareSaveResult,
    SharingManager,
)
from src.models.authz import Principal
from src.models.errors import CodedError, ErrorKind
from web.error_messages import (
    error_kind_of,
    mark_renderable,
    toast_response,
    user_message_for,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

executor = ThreadPoolExecutor(max_workers=4)

#: The one share dialog of a page (``components/dialog.html``).
DIALOG_ID = "share-dialog"

#: The list panel each type reloads after a save (``list_tabpanel`` id).
_LIST_TARGETS: dict[str, str] = {
    "persona": "#persona-list",
    # List of discussion/interview results (/discussion/results)
    "discussion": "#discussion-list",
    # List of survey templates and results (/survey/templates, /survey/results)
    "survey_template": "#survey-template-list",
    "survey": "#survey-list",
}

#: Detail-screen regions that show the shares and are reloaded after a save
#: (their own ``data-reload-url``). The list and the detail are never on the
#: same screen, so the response names both.
_DETAIL_TARGETS: dict[str, tuple[str, ...]] = {"persona": ("#persona-detail-header",)}

#: Status code of an error shown inside the dialog, by kind.
_DIALOG_ERROR_STATUS: dict[ErrorKind, int] = {
    ErrorKind.FORBIDDEN: 403,
    ErrorKind.VALIDATION: 400,
    ErrorKind.CAPACITY: 400,
    ErrorKind.TRANSIENT: 503,
}

_sharing_manager: SharingManager | None = None


def get_sharing_manager() -> SharingManager:
    """Get the SharingManager singleton (lazy initialization)."""
    global _sharing_manager
    if _sharing_manager is None:
        _sharing_manager = SharingManager()
    return _sharing_manager


def _principal(request: Request) -> Principal:
    principal: Principal = request.state.principal
    return principal


async def _run(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, lambda: func(*args, **kwargs))


def _split_ids(values: list[str]) -> list[str]:
    """``ids=a&ids=b`` and ``ids=a,b`` are both accepted."""
    return [part.strip() for value in values for part in value.split(",")]


# =============================================================================
# Contents of the dialog
# =============================================================================


def _dialog_response(
    request: Request, resource_type: str, view: Any, post_url: str
) -> Any:
    return templates.TemplateResponse(
        request,
        "sharing/partials/dialog_content.html",
        {
            "request": request,
            "view": view,
            "resource_type": resource_type,
            "post_url": post_url,
            "dialog_id": DIALOG_ID,
            "vanished_label": VANISHED_LABEL,
        },
    )


@router.get("/share/{resource_type}/dialog", response_class=HTMLResponse)
async def bulk_share_dialog(
    request: Request, resource_type: str, ids: list[str] = Query(default=[])
) -> Any:
    """Contents of the bulk sharing dialog."""
    view = await _run(
        get_sharing_manager().bulk_dialog,
        _principal(request),
        resource_type,
        _split_ids(ids),
    )
    return _dialog_response(request, resource_type, view, f"/share/{resource_type}")


@router.get("/share/{resource_type}/{resource_id}/dialog", response_class=HTMLResponse)
async def share_dialog(request: Request, resource_type: str, resource_id: str) -> Any:
    """Contents of the sharing dialog for one item. 404 and 403 go to the shared exception handler."""
    view = await _run(
        get_sharing_manager().dialog, _principal(request), resource_type, resource_id
    )
    return _dialog_response(
        request, resource_type, view, f"/share/{resource_type}/{resource_id}"
    )


# =============================================================================
# Save
# =============================================================================


async def _picker_values(request: Request) -> dict[str, list[str]]:
    form = await request.form()
    return {
        "selected": [str(v) for v in form.getlist("departments")],
        "unchanged": [str(v) for v in form.getlist("departments_unchanged")],
        "initial": [str(v) for v in form.getlist("departments_initial")],
    }


def _saved_response(resource_type: str, result: ShareSaveResult, *, bulk: bool) -> Any:
    """All succeeded: close with no body, reload, and show a toast."""
    message = (
        f"{len(result.updated)} 件の共有先を更新しました"
        if bulk
        else "共有先を更新しました"
    )
    triggers: dict[str, Any] = {
        "dialogClose": {"id": DIALOG_ID},
        "showToast": {"message": message, "type": "success"},
    }
    target = _LIST_TARGETS.get(resource_type)
    if target:
        # Refers to both the list (admin screen) and the detail header (detail screen). app.js skips the one that is not on the screen
        targets = [target, *_DETAIL_TARGETS.get(resource_type, ())]
        triggers["listReload"] = {"targets": targets}
    return Response(content="", headers={"HX-Trigger": json.dumps(triggers)})


def _dialog_error(
    request: Request, resource_type: str, kind: ErrorKind, message: str
) -> Any:
    """Show invalid input, failures that a retry fixes, and 403 inside the dialog (keeping the selection).

    Takes the resolved wording and kind rather than the exception itself (so the
    check on exposed exception text, tests/api/test_error_exposure.py, applies).
    """
    headers: dict[str, str] = {}
    target = _LIST_TARGETS.get(resource_type)
    if kind is ErrorKind.FORBIDDEN and target:
        # After closing, reload the list (the user may have lost permission)
        headers["HX-Trigger"] = json.dumps({"shareReloadOnClose": {"target": target}})
    return mark_renderable(
        templates.TemplateResponse(
            request,
            "partials/error_inline.html",
            {"request": request, "error": message},
            status_code=_DIALOG_ERROR_STATUS.get(kind, 503),
            headers=headers,
        )
    )


async def _save(
    request: Request,
    resource_type: str,
    save: Callable[[], ShareSaveResult],
    *,
    bulk: bool,
) -> Any:
    try:
        result = await _run(save)
    except CodedError as e:
        kind = error_kind_of(e)
        if kind is ErrorKind.NOT_FOUND:
            raise  # Unreadable, nonexistent and unregistered kinds all give the same 404 (shared exception handler)
        logger.info("Share save rejected", exc_info=True)
        return _dialog_error(request, resource_type, kind, user_message_for(e))
    except Exception as e:
        logger.error("Share save error", exc_info=True)
        return toast_response(e, status_code=500)
    if result.is_complete:
        return _saved_response(resource_type, result, bulk=bulk)
    return templates.TemplateResponse(
        request,
        "sharing/partials/bulk_result.html",
        {"request": request, "result": result},
    )


@router.post("/share/{resource_type}", response_class=HTMLResponse)
async def save_bulk_shares(request: Request, resource_type: str) -> Any:
    """Bulk save. A partial failure shows the result inside the dialog."""
    values = await _picker_values(request)
    form = await request.form()
    ids = _split_ids([str(v) for v in form.getlist("ids")])
    principal = _principal(request)
    manager = get_sharing_manager()
    return await _save(
        request,
        resource_type,
        lambda: manager.bulk_save(principal, resource_type, ids, **values),
        bulk=True,
    )


@router.post("/share/{resource_type}/{resource_id}", response_class=HTMLResponse)
async def save_shares(request: Request, resource_type: str, resource_id: str) -> Any:
    """Save for one item."""
    values = await _picker_values(request)
    principal = _principal(request)
    manager = get_sharing_manager()
    return await _save(
        request,
        resource_type,
        lambda: manager.save(principal, resource_type, resource_id, **values),
        bulk=False,
    )
