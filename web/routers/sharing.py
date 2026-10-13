"""
共有の入口（U4、WF1・WF2、M7）。

4 種類（ペルソナ・議論・テンプレート・アンケート結果）で同じ URL の形にする。
登録のない種類は 404。判定は SharingManager → ResourceSharing → AccessPolicy が持ち、
ここは値の受け取りと応答の形（HX-Trigger、ダイアログの中のエラー）だけを持つ。

- GET  /share/{resource_type}/{resource_id}/dialog ... 1 件のダイアログの中身
- GET  /share/{resource_type}/dialog?ids=...       ... 一括のダイアログの中身
- POST /share/{resource_type}/{resource_id}        ... 1 件の保存
- POST /share/{resource_type}                      ... 一括の保存

読めない対象と存在しない対象は、共通の例外ハンドラが同じ 404 にする（NFR1.14）。
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
_LIST_TARGETS: dict[str, str] = {"persona": "#persona-list"}

#: Status code of an error shown inside the dialog, by kind.
_DIALOG_ERROR_STATUS: dict[ErrorKind, int] = {
    ErrorKind.FORBIDDEN: 403,
    ErrorKind.VALIDATION: 400,
    ErrorKind.CAPACITY: 400,
    ErrorKind.TRANSIENT: 503,
}

_sharing_manager: SharingManager | None = None


def get_sharing_manager() -> SharingManager:
    """SharingManager のシングルトンを取得する（遅延初期化）。"""
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
# ダイアログの中身
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
    """一括の共有ダイアログの中身（WF2、BR7.1）。"""
    view = await _run(
        get_sharing_manager().bulk_dialog,
        _principal(request),
        resource_type,
        _split_ids(ids),
    )
    return _dialog_response(request, resource_type, view, f"/share/{resource_type}")


@router.get("/share/{resource_type}/{resource_id}/dialog", response_class=HTMLResponse)
async def share_dialog(request: Request, resource_type: str, resource_id: str) -> Any:
    """1 件の共有ダイアログの中身（WF1）。404・403 は共通の例外ハンドラ。"""
    view = await _run(
        get_sharing_manager().dialog, _principal(request), resource_type, resource_id
    )
    return _dialog_response(
        request, resource_type, view, f"/share/{resource_type}/{resource_id}"
    )


# =============================================================================
# 保存
# =============================================================================


async def _picker_values(request: Request) -> dict[str, list[str]]:
    form = await request.form()
    return {
        "selected": [str(v) for v in form.getlist("departments")],
        "unchanged": [str(v) for v in form.getlist("departments_unchanged")],
        "initial": [str(v) for v in form.getlist("departments_initial")],
    }


def _saved_response(resource_type: str, result: ShareSaveResult, *, bulk: bool) -> Any:
    """全件成功: 本文なしで閉じる・読み直す・トースト（BR7.3）。"""
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
        triggers["listReload"] = {"target": target}
    return Response(content="", headers={"HX-Trigger": json.dumps(triggers)})


def _dialog_error(
    request: Request, resource_type: str, kind: ErrorKind, message: str
) -> Any:
    """入力の誤り・再試行で直る失敗・403 をダイアログの中に出す（選択を保つ）。

    例外そのものではなく解決済みの文言と種類を受け取る（例外文の露出の検査が
    効くようにするため、tests/api/test_error_exposure.py）。
    """
    headers: dict[str, str] = {}
    target = _LIST_TARGETS.get(resource_type)
    if kind is ErrorKind.FORBIDDEN and target:
        # 閉じたら一覧を読み直す（権限を失っていた、BR7.3）
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
            raise  # 読めない・存在しない・登録のない種類は同じ 404（共通の例外ハンドラ）
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
    """一括の保存（WF2、BR2）。一部失敗はダイアログの中に結果を出す。"""
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
    """1 件の保存（WF1、BR1）。"""
    values = await _picker_values(request)
    principal = _principal(request)
    manager = get_sharing_manager()
    return await _save(
        request,
        resource_type,
        lambda: manager.save(principal, resource_type, resource_id, **values),
        bulk=False,
    )
