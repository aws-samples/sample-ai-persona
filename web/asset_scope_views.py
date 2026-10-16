"""The usage-scope dialog of organization assets (M10, U7 BR5.2), for the routers.

The settings screen (datasets, knowledge bases) and the persona data screen
(custom persona data) show the same dialog. This module only turns requests
and Manager results into responses; the decisions stay in the Managers.
"""

import json
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from src.models.errors import ErrorKind
from src.models.sharing import AssetScopeChange, AssetScopeDialog
from web.error_messages import mark_renderable

#: The dialog id (one on the screen at a time, inserted and removed).
SCOPE_DIALOG_ID = "asset-scope-dialog"

_templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


async def change_from_request(request: Request) -> AssetScopeChange:
    """The change the dialog sent (``departments`` / ``departments_initial``)."""
    form = await request.form()
    return AssetScopeChange.from_form(
        [str(v) for v in form.getlist("departments")],
        [str(v) for v in form.getlist("departments_initial")],
    )


def dialog_response(request: Request, dialog: AssetScopeDialog, save_url: str) -> Any:
    """The dialog (htmx inserts it into the body; it opens itself)."""
    return _templates.TemplateResponse(
        request,
        "settings/partials/asset_scope_dialog.html",
        {
            "request": request,
            "scope": dialog,
            "dialog_id": SCOPE_DIALOG_ID,
            "save_url": save_url,
        },
    )


def saved_response(list_target: str) -> Response:
    """Close the dialog and reload the list (its ``data-reload-url``).

    ``listReload`` comes first: htmx fires the events on the form in this
    order, and closing removes the dialog (``remove_on_close``), after which
    an event fired on the form no longer reaches the body.
    """
    triggers = {
        "listReload": {"target": list_target},
        "dialogClose": {"id": SCOPE_DIALOG_ID},
    }
    return Response(content="", headers={"HX-Trigger": json.dumps(triggers)})


def error_response(request: Request, message: str, kind: ErrorKind) -> Any:
    """A save failure shown inside the dialog (the choice is kept).

    ``message`` is the catalog wording. Input errors (an unknown department)
    answer 400; anything else is a retryable failure (503). Not-found and
    forbidden are not handled here: the routers let them reach the common
    exception handler.
    """
    status = 400 if kind is ErrorKind.VALIDATION else 503
    return mark_renderable(
        _templates.TemplateResponse(
            request,
            "partials/error_inline.html",
            {"request": request, "error": message},
            status_code=status,
        )
    )
