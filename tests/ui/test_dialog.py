"""Shared dialog (C3, WF2, NFR3.9, NFR6.2-NFR6.5, NFR6.12, B4-B6, B16, B18, B22)."""

import json

import pytest

from tests.ui.conftest import UiHarness, render_page

INERTED = """() => Array.from(document.querySelectorAll('[inert]')).map(
    (el) => el.id || el.dataset.testid || el.tagName.toLowerCase())"""


@pytest.fixture
def dlg(ui: UiHarness) -> UiHarness:
    ui.load_component("dialog.html")
    return ui


def _open(ui: UiHarness, dialog_id: str) -> None:
    ui.page.click(f"[data-dialog-open='{dialog_id}']")
    ui.settle()


def _is_open(ui: UiHarness, dialog_id: str) -> bool:
    ui.settle()
    return ui.page.evaluate(
        "(id) => { const d = document.getElementById(id); return !!d && !d.hidden; }",
        dialog_id,
    )


def _active_in(ui: UiHarness, dialog_id: str) -> bool:
    return ui.active(
        f"document.getElementById('{dialog_id}').contains(document.activeElement)"
    )


def test_open_shows_dialog_and_inerts_only_ancestor_siblings(dlg: UiHarness) -> None:
    assert dlg.page.get_attribute("#form-dialog", "hidden") is not None

    _open(dlg, "form-dialog")

    assert _is_open(dlg, "form-dialog")
    flags = dlg.page.evaluate(
        """() => ({
            dialog: document.getElementById('form-dialog').inert,
            main: document.getElementById('main').inert,
            body: document.body.inert,
            toasts: document.getElementById('flash-messages').inert,
            nav: document.querySelector('nav').inert,
            footer: document.querySelector('footer').inert,
            heading: document.querySelector('main h1').inert,
            region: document.getElementById('swap-region').inert,
        })"""
    )
    assert flags == {
        "dialog": False,
        "main": False,
        "body": False,
        "toasts": False,
        "nav": True,
        "footer": True,
        "heading": True,
        "region": True,
    }


def test_close_releases_only_the_inert_it_added(dlg: UiHarness) -> None:
    _open(dlg, "form-dialog")
    dlg.page.keyboard.press("Escape")

    assert not _is_open(dlg, "form-dialog")
    assert dlg.page.evaluate(INERTED) == ["pre-inert"]


def test_focus_moves_to_first_input_or_heading(dlg: UiHarness) -> None:
    _open(dlg, "form-dialog")
    assert dlg.active() == "form-name"
    dlg.page.keyboard.press("Escape")

    _open(dlg, "info-dialog")
    assert dlg.active() == "info-dialog-title"


def test_tab_and_shift_tab_cycle_inside(dlg: UiHarness) -> None:
    _open(dlg, "form-dialog")

    for _ in range(8):
        dlg.page.keyboard.press("Tab")
        assert _active_in(dlg, "form-dialog")
    dlg.page.focus("#form-name")
    dlg.page.keyboard.press("Shift+Tab")
    assert (
        dlg.active("document.activeElement.dataset.testid")
        == "form-dialog-close-button"
    )
    dlg.page.keyboard.press("Shift+Tab")
    assert dlg.active("document.activeElement.dataset.testid") == "form-dialog-submit"


@pytest.mark.parametrize(
    "close",
    [
        "Escape",
        "[data-testid='form-dialog-close-button']",
        "[data-testid='form-dialog-cancel']",
    ],
)
def test_close_returns_focus_to_the_opener(dlg: UiHarness, close: str) -> None:
    _open(dlg, "form-dialog")

    if close == "Escape":
        dlg.page.keyboard.press("Escape")
    else:
        dlg.page.click(close)

    assert not _is_open(dlg, "form-dialog")
    assert dlg.active("document.activeElement.dataset.dialogOpen") == "form-dialog"


def test_focus_falls_back_to_main_when_the_opener_is_gone(dlg: UiHarness) -> None:
    _open(dlg, "form-dialog")
    dlg.page.evaluate(
        "document.querySelector(\"[data-dialog-open='form-dialog']\").remove()"
    )

    dlg.page.keyboard.press("Escape")

    assert dlg.active() == "main"


def test_backdrop_click_closes_only_when_allowed(dlg: UiHarness) -> None:
    _open(dlg, "form-dialog")
    dlg.page.mouse.click(3, 3)
    assert _is_open(dlg, "form-dialog")
    dlg.page.keyboard.press("Escape")

    _open(dlg, "view-dialog")
    dlg.page.mouse.click(3, 3)
    assert not _is_open(dlg, "view-dialog")


def test_submitting_blocks_escape_and_close_until_dialog_close(dlg: UiHarness) -> None:
    held = dlg.hold("/submit")
    _open(dlg, "form-dialog")
    dlg.page.fill("#form-name", "新しい名前")
    dlg.page.click("[data-testid='form-dialog-submit']")
    held.wait_for_request()

    dlg.page.keyboard.press("Escape")
    dlg.page.click("[data-testid='form-dialog-close-button']")
    assert _is_open(dlg, "form-dialog")
    assert dlg.page.is_disabled("[data-testid='form-dialog-submit']")

    held.release(
        headers={"HX-Trigger": json.dumps({"dialogClose": {"id": "form-dialog"}})}
    )

    assert not _is_open(dlg, "form-dialog")
    assert dlg.page.is_enabled("[data-testid='form-dialog-submit']")
    assert dlg.page.evaluate(INERTED) == ["pre-inert"]


def test_toast_error_keeps_the_dialog_open_and_ends_submitting(dlg: UiHarness) -> None:
    dlg.respond(
        "/submit",
        status=503,
        headers={
            "HX-Trigger": json.dumps(
                {"showToast": {"type": "error", "message": "失敗しました"}}
            )
        },
    )
    _open(dlg, "form-dialog")
    dlg.page.fill("#form-name", "残る入力")

    dlg.page.click("[data-testid='form-dialog-submit']")
    dlg.page.wait_for_selector("#toast-alert [data-toast]")

    assert _is_open(dlg, "form-dialog")
    assert dlg.page.input_value("#form-name") == "残る入力"
    assert dlg.page.is_enabled("[data-testid='form-dialog-submit']")
    dlg.page.keyboard.press("Escape")
    assert not _is_open(dlg, "form-dialog")


def test_close_on_success_closes_only_after_a_successful_request(
    dlg: UiHarness,
) -> None:
    dlg.respond(
        "/save",
        status=503,
        headers={"HX-Trigger": json.dumps({"showToast": {"message": "失敗"}})},
    )
    _open(dlg, "success-dialog")
    dlg.page.click("[data-testid='success-dialog-submit']")
    dlg.page.wait_for_selector("#toast-alert [data-toast]")
    assert _is_open(dlg, "success-dialog")

    dlg.respond("/save", status=200)
    dlg.page.click("[data-testid='success-dialog-submit']")

    assert not _is_open(dlg, "success-dialog")
    assert dlg.active("document.activeElement.dataset.dialogOpen") == "success-dialog"


def test_content_url_is_busy_while_loading_then_focuses_content(dlg: UiHarness) -> None:
    held = dlg.hold("/dialog-content")
    _open(dlg, "load-dialog")
    held.wait_for_request()

    panel = dlg.page.locator("#load-dialog [role='dialog']")
    assert panel.get_attribute("aria-busy") == "true"
    assert "読み込み中..." in panel.inner_text()

    held.release(
        body='<label for="loaded-input">項目</label><input id="loaded-input" type="text">'
    )

    dlg.settle()
    assert panel.get_attribute("aria-busy") is None
    assert dlg.active() == "loaded-input"


def test_content_load_failure_shows_fixed_text_and_retry_without_generic_toast(
    dlg: UiHarness,
) -> None:
    dlg.respond("/dialog-content", status=500, body="<p>Traceback secret-path</p>")
    _open(dlg, "load-dialog")
    dlg.page.wait_for_selector("#load-dialog [data-dialog-reload]")

    panel = dlg.page.locator("#load-dialog [role='dialog']")
    text = panel.inner_text()
    assert "内容を読み込めませんでした" in text
    assert "secret-path" not in text
    assert panel.get_attribute("aria-busy") is None
    assert dlg.active("document.activeElement.hasAttribute('data-dialog-reload')")
    assert dlg.page.locator("#toast-alert [data-toast]").count() == 0

    dlg.respond("/dialog-content", body='<input id="loaded-input" aria-label="項目">')
    dlg.page.click("#load-dialog [data-dialog-reload]")
    dlg.page.wait_for_selector("#loaded-input")
    assert dlg.active() == "loaded-input"


def test_inserted_dialog_opens_itself_and_is_removed_on_close(dlg: UiHarness) -> None:
    dlg.respond("/insert", body=render_page("dialog_inserted.html"))

    dlg.page.click("#insert-button")
    dlg.page.wait_for_selector("#inserted-dialog:not([hidden])", state="attached")
    assert dlg.active() == "inserted-name"

    dlg.page.click("[data-testid='inserted-dialog-cancel']")

    dlg.settle()
    assert dlg.page.locator("#inserted-dialog").count() == 0
    assert dlg.active() == "insert-button"
    assert dlg.page.evaluate(INERTED) == ["pre-inert"]


# evaluate() は戻り値の Promise を待つため、openDialog() の Promise は返さない
OPEN_INFO = """() => {
    window.__result = 'unset';
    openDialog('info-dialog').then((v) => { window.__result = v; });
}"""


def test_open_dialog_promise_resolves_with_the_pressed_value(dlg: UiHarness) -> None:
    dlg.page.evaluate(OPEN_INFO)
    dlg.page.click("[data-testid='info-dialog-ok']")
    dlg.settle()
    assert dlg.page.evaluate("window.__result") == "ok"

    dlg.page.evaluate(OPEN_INFO)
    dlg.settle()
    dlg.page.keyboard.press("Escape")
    dlg.settle()
    assert dlg.page.evaluate("window.__result") is None


def test_swapping_out_the_open_dialog_releases_inert(dlg: UiHarness) -> None:
    dlg.respond("/swap", body="<p>差し替えた</p>")
    _open(dlg, "region-dialog")

    dlg.page.evaluate("htmx.ajax('GET', '/swap', {target: '#swap-region'})")
    dlg.page.wait_for_selector("#swap-region:has-text('差し替えた')")

    dlg.settle()
    assert dlg.page.evaluate(INERTED) == ["pre-inert"]


@pytest.mark.parametrize("event", ["pagehide", "htmx:beforeHistorySave"])
def test_leaving_the_page_releases_inert(dlg: UiHarness, event: str) -> None:
    _open(dlg, "form-dialog")

    target = "window" if event == "pagehide" else "document.body"
    dlg.page.evaluate(f"{target}.dispatchEvent(new Event('{event}'))")

    assert dlg.page.evaluate(INERTED) == ["pre-inert"]


def test_toast_close_button_works_while_a_dialog_is_open(dlg: UiHarness) -> None:
    _open(dlg, "form-dialog")
    dlg.page.evaluate("showFlashMessage('保存に失敗しました', 'error')")

    dlg.page.click("#toast-alert [data-toast] button")

    assert dlg.page.locator("#toast-alert [data-toast]").count() == 0
    assert _is_open(dlg, "form-dialog")


def test_keyboard_reaches_and_closes_a_toast_then_returns_to_the_dialog(
    dlg: UiHarness,
) -> None:
    _open(dlg, "form-dialog")
    dlg.page.evaluate("showFlashMessage('保存に失敗しました', 'error')")
    in_toast = "document.activeElement.closest('#flash-messages') !== null"

    # パネルの最後（保存）の次はトーストの閉じるボタン、その次はパネルの最初へ戻る
    dlg.page.focus("[data-testid='form-dialog-submit']")
    dlg.page.keyboard.press("Tab")
    assert dlg.active(in_toast)
    assert (
        dlg.active("document.activeElement.getAttribute('aria-label')")
        == "通知を閉じる"
    )
    dlg.page.keyboard.press("Tab")
    assert (
        dlg.active("document.activeElement.dataset.testid")
        == "form-dialog-close-button"
    )
    # パネルの最初の前もトーストの閉じるボタン
    dlg.page.keyboard.press("Shift+Tab")
    assert dlg.active(in_toast)

    dlg.page.keyboard.press("Enter")

    assert dlg.page.locator("#toast-alert [data-toast]").count() == 0
    assert _is_open(dlg, "form-dialog")
    assert _active_in(dlg, "form-dialog")


def test_without_a_toast_tab_stays_in_the_panel(dlg: UiHarness) -> None:
    _open(dlg, "form-dialog")
    dlg.page.focus("[data-testid='form-dialog-submit']")

    dlg.page.keyboard.press("Tab")

    assert (
        dlg.active("document.activeElement.dataset.testid")
        == "form-dialog-close-button"
    )


def test_opening_another_dialog_closes_the_first(dlg: UiHarness) -> None:
    _open(dlg, "form-dialog")
    dlg.page.evaluate(OPEN_INFO)
    dlg.settle()

    assert not _is_open(dlg, "form-dialog")
    assert _is_open(dlg, "info-dialog")


def test_open_dialog_has_no_axe_violations(dlg: UiHarness) -> None:
    _open(dlg, "form-dialog")
    dlg.check_a11y()
