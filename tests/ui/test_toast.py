"""Toast behaviour (WF4, NFR3.10, NFR6.5, NFR6.6, B19-B22)."""

import json

from tests.ui.conftest import UiHarness

STATUS = "#toast-status"
ALERT = "#toast-alert"


def _show(ui: UiHarness, message: str, kind: str) -> None:
    ui.page.evaluate("([m, k]) => showFlashMessage(m, k)", [message, kind])


def _toasts(ui: UiHarness, container: str) -> list[str]:
    return ui.page.locator(f"{container} [data-toast]").all_inner_texts()


def test_live_regions_exist_on_load_with_their_roles(ui: UiHarness) -> None:
    ui.load_component("toast.html")

    assert ui.page.get_attribute(STATUS, "role") == "status"
    assert ui.page.get_attribute(ALERT, "role") == "alert"
    # 外側に読み上げ領域を重ねない（二重に読まれるため）
    assert ui.page.get_attribute("#flash-messages", "aria-live") is None


def test_kind_selects_the_container(ui: UiHarness) -> None:
    ui.load_component("toast.html")

    _show(ui, "保存しました", "success")
    _show(ui, "お知らせです", "info")
    _show(ui, "失敗しました", "error")
    _show(ui, "確認してください", "warning")

    assert [t.strip() for t in _toasts(ui, STATUS)] == ["保存しました", "お知らせです"]
    assert [t.strip() for t in _toasts(ui, ALERT)] == [
        "失敗しました",
        "確認してください",
    ]


def test_success_disappears_after_five_seconds(ui: UiHarness) -> None:
    ui.page.clock.install()
    ui.load_component("toast.html")

    _show(ui, "保存しました", "success")
    ui.page.clock.fast_forward(4900)
    assert len(_toasts(ui, STATUS)) == 1
    ui.page.clock.fast_forward(200)
    assert _toasts(ui, STATUS) == []


def test_pointer_and_focus_pause_the_countdown(ui: UiHarness) -> None:
    ui.page.clock.install()
    ui.load_component("toast.html")
    _show(ui, "保存しました", "success")
    toast = ui.page.locator(f"{STATUS} [data-toast]")

    ui.page.clock.fast_forward(3000)
    toast.hover()
    ui.page.clock.fast_forward(10000)
    assert toast.count() == 1
    ui.page.mouse.move(0, 0)
    # 残りの約 2 秒から数え直す
    ui.page.clock.fast_forward(1500)
    assert toast.count() == 1
    ui.page.clock.fast_forward(700)
    assert toast.count() == 0

    _show(ui, "もう一度保存しました", "success")
    toast.locator("button").focus()
    ui.page.clock.fast_forward(10000)
    assert toast.count() == 1
    ui.page.locator("#probe-input").focus()
    ui.page.clock.fast_forward(5100)
    assert toast.count() == 0


def test_error_stays_until_closed(ui: UiHarness) -> None:
    ui.page.clock.install()
    ui.load_component("toast.html")

    _show(ui, "失敗しました", "error")
    _show(ui, "確認してください", "warning")
    ui.page.clock.fast_forward(60000)

    assert len(_toasts(ui, ALERT)) == 2


def test_close_button_is_labelled_and_keyboard_reachable(ui: UiHarness) -> None:
    ui.load_component("toast.html")
    _show(ui, "失敗しました", "error")
    close = ui.page.locator(f"{ALERT} [data-toast] button")

    assert close.get_attribute("aria-label") == "通知を閉じる"
    assert close.get_attribute("type") == "button"
    for _ in range(40):
        ui.page.keyboard.press("Tab")
        if ui.page.evaluate(
            "document.activeElement && document.activeElement.closest('[data-toast]') !== null"
        ):
            break
    else:
        raise AssertionError("Tab で閉じるボタンに届かない")
    ui.page.keyboard.press("Enter")
    assert _toasts(ui, ALERT) == []


def test_showing_a_toast_does_not_move_focus(ui: UiHarness) -> None:
    ui.load_component("toast.html")
    ui.page.locator("#probe-input").focus()

    _show(ui, "保存しました", "success")
    _show(ui, "失敗しました", "error")

    assert ui.page.evaluate("document.activeElement.id") == "probe-input"


def test_hx_trigger_accepts_level_or_type(ui: UiHarness) -> None:
    ui.load_component("toast.html")

    ui.respond(
        "/toast-probe",
        headers={
            "HX-Trigger": json.dumps(
                {"showToast": {"level": "success", "message": "共有しました"}}
            )
        },
    )
    ui.page.click("#probe-button")
    ui.page.wait_for_selector(f"{STATUS} [data-toast]")

    ui.respond(
        "/toast-probe",
        status=400,
        headers={
            "HX-Trigger": json.dumps(
                {"showToast": {"type": "error", "message": "失敗しました"}}
            )
        },
    )
    ui.page.click("#probe-button")
    ui.page.wait_for_selector(f"{ALERT} [data-toast]")

    assert [t.strip() for t in _toasts(ui, STATUS)] == ["共有しました"]
    assert [t.strip() for t in _toasts(ui, ALERT)] == ["失敗しました"]


def test_markup_in_message_is_shown_as_text(ui: UiHarness) -> None:
    ui.load_component("toast.html")

    _show(ui, "<script>window.__pwned = 1</script><b>太字</b>", "error")

    toast = ui.page.locator(f"{ALERT} [data-toast]")
    assert "<script>" in toast.inner_text()
    assert toast.locator("script, b").count() == 0
    assert ui.page.evaluate("window.__pwned") is None
