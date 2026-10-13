"""Share dialog (U4, M7, WF1, WF2, BR7.1〜BR7.5, NFR6.13〜NFR6.15, AC3.2.1, AC3.2.2, AC3.3.3)."""

import json
from typing import Any

import pytest

from src.managers.sharing_manager import ShareSaveResult
from src.models.authz import Department, ResourceType
from src.models.sharing import BulkShareFailure, ShareDialogView
from src.models.errors import ErrorCode
from tests.ui.conftest import UiHarness, render_macro, render_page

ROWS = [("p1", "佐藤 太郎"), ("p2", "鈴木 花子")]
CHOICES = (Department("sales", "営業部"), Department("plan", "企画部"))
DIALOG = "#share-dialog"
SAVE = "[data-testid='share-save']"
CANCEL = "[data-testid='share-cancel']"
RELOAD = "/persona/management?tab=mine"


def _view(**overrides: Any) -> ShareDialogView:
    values: dict[str, Any] = {
        "resource_type": ResourceType.PERSONA,
        "target_ids": ("p1",),
        "target_label": "佐藤 太郎",
        "choices": CHOICES,
        "selected": frozenset({"sales"}),
        "show_scope_note": True,
        "show_persona_notice": True,
    }
    values.update(overrides)
    return ShareDialogView(**values)


def _content(view: ShareDialogView, post_url: str = "/share/persona/p1") -> str:
    return render_page(
        "sharing/partials/dialog_content.html",
        view=view,
        post_url=post_url,
        vanished_label="（存在しないグループ）",
    )


def _list_html() -> str:
    """What the list panel reload returns (a bar, the rows and their buttons)."""
    bar = render_macro(
        "bulk_select.html",
        "bulk_select_bar",
        "persona",
        True,
        "共有先を設定",
        "share-dialog",
    )
    rows = "".join(
        f"<li>{render_macro('bulk_select.html', 'bulk_select_row', v, n, True)}"
        f"<span>{n}</span>"
        f'<button type="button" aria-haspopup="dialog" data-dialog-open="share-dialog"'
        f' data-share-url="/share/persona/{v}/dialog" data-testid="share-{v}">'
        f"{n} の共有先を設定</button></li>"
        for v, n in ROWS
    )
    return f"{bar}<ul>{rows}</ul>"


def _triggers(**events: Any) -> dict[str, str]:
    return {"HX-Trigger": json.dumps(events)}


SAVED = _triggers(
    dialogClose={"id": "share-dialog"},
    listReload={"target": "#persona-list"},
    showToast={"message": "共有先を更新しました", "type": "success"},
)


@pytest.fixture
def share(ui: UiHarness) -> UiHarness:
    ui.load_component("share_dialog.html", rows=ROWS)
    ui.settle()
    return ui


def _open_single(ui: UiHarness, view: ShareDialogView | None = None) -> None:
    ui.respond("/share/persona/p1/dialog", body=_content(view or _view()))
    ui.page.click("[data-testid='share-p1']")
    ui.page.wait_for_selector(f"{DIALOG} [data-department-picker]")
    ui.settle()


def _is_open(ui: UiHarness) -> bool:
    ui.settle()
    return ui.page.evaluate("() => !document.getElementById('share-dialog').hidden")


def test_keyboard_only_open_choose_save_and_close(share: UiHarness) -> None:
    share.respond("/share/persona/p1/dialog", body=_content(_view()))
    share.respond("/share/persona/p1", headers=SAVED)
    share.respond(RELOAD, body=_list_html())

    share.page.focus("[data-testid='share-p1']")
    share.page.keyboard.press("Enter")
    share.page.wait_for_selector(f"{DIALOG} [data-department-picker]")
    assert share.active("document.activeElement.value") == "sales"

    share.page.keyboard.press("Tab")
    assert share.active("document.activeElement.value") == "plan"
    share.page.keyboard.press("Space")
    for _ in range(6):
        if share.active("document.activeElement.dataset.testid") == "share-save":
            break
        share.page.keyboard.press("Tab")
    share.page.keyboard.press("Enter")

    share.page.wait_for_selector(DIALOG, state="hidden")
    assert share.active("document.activeElement.dataset.testid") == "share-p1"
    assert "/share/persona/p1" in share.requests
    share.page.wait_for_selector("#flash-messages :text('共有先を更新しました')")


def test_notes_and_removal_warning_describe_the_save_button(share: UiHarness) -> None:
    _open_single(share)

    described = share.page.get_attribute(SAVE, "aria-describedby").split()
    assert described == [
        "share-persona-notice",
        "share-scope-note",
        "departments-removing",
    ]
    for element_id in described:
        assert share.page.locator(f"#{element_id}").count() == 1
    share.page.uncheck("[data-department-option][value='sales']")
    assert "営業部" in share.page.inner_text("#departments-removing")


def test_removing_every_share_asks_first_with_back_focused(share: UiHarness) -> None:
    _open_single(share)

    share.page.uncheck("[data-department-option][value='sales']")
    share.page.click(SAVE)

    assert share.page.is_visible("[data-department-confirm]")
    assert share.active("document.activeElement.dataset.testid") == (
        "department-confirm-back"
    )
    assert "/share/persona/p1" not in share.requests


def test_both_buttons_are_disabled_while_saving(share: UiHarness) -> None:
    held = share.hold("/share/persona/p1")
    share.respond(RELOAD, body=_list_html())
    _open_single(share)

    share.page.check("[data-department-option][value='plan']")
    share.page.click(SAVE)
    held.wait_for_request()

    assert share.page.is_disabled(SAVE) and share.page.is_disabled(CANCEL)
    held.release(headers=SAVED)
    share.page.wait_for_selector(DIALOG, state="hidden")


def test_bulk_dialog_is_opened_for_the_selected_rows(share: UiHarness) -> None:
    bulk = _view(
        target_ids=("p1", "p2"),
        target_label="佐藤 太郎 ほか 1 件",
        selected=frozenset(),
        mixed={"sales": (1, 2)},
    )
    share.respond(
        "/share/persona/dialog?ids=p1&ids=p2", body=_content(bulk, "/share/persona")
    )
    share.page.check("[data-bulk-row][value='p1']")
    share.page.check("[data-bulk-row][value='p2']")

    share.page.click("[data-bulk-action]")

    share.page.wait_for_selector(f"{DIALOG} :text('2 件中 1 件で共有中')")
    assert share.page.locator(f"{DIALOG} input[name='ids']").count() == 2


def test_partial_failure_is_announced_and_failed_rows_are_selected_again(
    share: UiHarness,
) -> None:
    bulk = _view(target_ids=("p1", "p2"), target_label="佐藤 太郎 ほか 1 件")
    share.respond(
        "/share/persona/dialog?ids=p1&ids=p2", body=_content(bulk, "/share/persona")
    )
    result = ShareSaveResult(
        ResourceType.PERSONA,
        2,
        ("p1",),
        (BulkShareFailure("p2", ErrorCode.ACCESS_DENIED, "鈴木 花子"),),
    )
    share.respond(
        "/share/persona",
        body=render_page("sharing/partials/bulk_result.html", result=result),
    )
    share.respond(RELOAD, body=_list_html())
    share.page.check("[data-bulk-row][value='p1']")
    share.page.check("[data-bulk-row][value='p2']")
    share.page.click("[data-bulk-action]")
    share.page.wait_for_selector(f"{DIALOG} [data-department-picker]")

    share.page.check("[data-department-option][value='plan']")
    share.page.click(SAVE)
    result_box = share.page.wait_for_selector(f"{DIALOG} [data-share-result]")
    assert result_box.get_attribute("role") == "status"
    assert "鈴木 花子" in result_box.inner_text()

    share.page.click(CANCEL)
    share.page.wait_for_selector(DIALOG, state="hidden")
    share.page.wait_for_function(
        """() => document.querySelector("[data-bulk-row][value='p2']")?.checked === true
            && document.querySelector("[data-bulk-row][value='p1']")?.checked === false"""
    )
    assert RELOAD in share.requests
    assert share.page.inner_text("[data-bulk-count]").strip() == "1 件を選択中"


def test_forbidden_is_shown_and_the_list_is_reloaded_on_close(share: UiHarness) -> None:
    share.respond(
        "/share/persona/p1",
        status=403,
        body=render_page(
            "partials/error_inline.html", error="この操作の権限がありません"
        ),
        headers={
            "X-Render-Response": "true",
            **_triggers(shareReloadOnClose={"target": "#persona-list"}),
        },
    )
    share.respond(RELOAD, body=_list_html())
    _open_single(share)

    share.page.check("[data-department-option][value='plan']")
    share.page.click(SAVE)
    share.page.wait_for_selector(f"{DIALOG} :text('この操作の権限がありません')")
    assert _is_open(share)

    share.page.click(CANCEL)
    share.page.wait_for_selector(DIALOG, state="hidden")
    share.page.wait_for_timeout(100)
    assert RELOAD in share.requests


def test_open_share_dialog_has_no_axe_violations(share: UiHarness) -> None:
    _open_single(share)
    share.check_a11y()
