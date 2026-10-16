"""Navigation menus (C1, WF1, NFR6.2, NFR6.3, B1-B3)."""

from typing import Any

import pytest

from tests.ui.conftest import UiHarness

INTERVIEW = "[data-nav-toggle='interview']"
SURVEY = "[data-nav-toggle='survey']"
MOBILE = "[data-nav-toggle='mobile']"


@pytest.fixture
def nav(ui: UiHarness, screen_client: Any) -> UiHarness:
    ui.page.set_viewport_size({"width": 1280, "height": 800})
    ui.load_screen(screen_client, "/")
    return ui


def _active_text(ui: UiHarness) -> str:
    return " ".join(ui.active("document.activeElement.innerText").split())


def _expanded(ui: UiHarness, selector: str) -> str | None:
    ui.settle()
    return ui.page.get_attribute(selector, "aria-expanded")


@pytest.mark.parametrize("key", ["Enter", " ", "ArrowDown"])
def test_key_opens_the_menu_and_focuses_the_first_item(
    nav: UiHarness, key: str
) -> None:
    nav.page.focus(INTERVIEW)
    assert _expanded(nav, INTERVIEW) == "false"

    nav.page.keyboard.press(key)

    assert _expanded(nav, INTERVIEW) == "true"
    assert _active_text(nav) == "ペルソナ生成"
    controls = nav.page.get_attribute(INTERVIEW, "aria-controls")
    assert nav.page.locator(f"#{controls}").is_visible()


def test_arrows_move_between_items_and_wrap(nav: UiHarness) -> None:
    nav.page.focus(INTERVIEW)
    nav.page.keyboard.press("ArrowDown")
    nav.settle()

    nav.page.keyboard.press("ArrowDown")
    assert _active_text(nav) == "ペルソナ管理"
    nav.page.keyboard.press("ArrowUp")
    nav.page.keyboard.press("ArrowUp")
    assert _active_text(nav) == "議論結果"
    nav.page.keyboard.press("ArrowDown")
    assert _active_text(nav) == "ペルソナ生成"


def test_escape_closes_and_returns_focus_to_the_button(nav: UiHarness) -> None:
    nav.page.focus(SURVEY)
    nav.page.keyboard.press("ArrowDown")
    nav.settle()
    nav.page.keyboard.press("ArrowDown")

    nav.page.keyboard.press("Escape")

    assert _expanded(nav, SURVEY) == "false"
    assert nav.active("document.activeElement.dataset.navToggle") == "survey"


def test_tabbing_out_closes_without_moving_focus(nav: UiHarness) -> None:
    nav.page.focus(INTERVIEW)
    nav.page.keyboard.press("ArrowDown")
    nav.settle()
    for _ in range(4):
        nav.page.keyboard.press("Tab")

    assert _expanded(nav, INTERVIEW) == "false"
    assert nav.active("document.activeElement.closest('nav') === null")


def test_outside_click_closes(nav: UiHarness) -> None:
    nav.page.click(INTERVIEW)
    assert _expanded(nav, INTERVIEW) == "true"

    nav.page.mouse.click(5, 700)

    assert _expanded(nav, INTERVIEW) == "false"


def test_narrow_screen_menu_button_has_the_same_keys(
    ui: UiHarness, screen_client: Any
) -> None:
    ui.page.set_viewport_size({"width": 500, "height": 800})
    ui.load_screen(screen_client, "/")

    assert ui.page.locator(MOBILE).inner_text().strip() == "メニュー"
    ui.page.focus(MOBILE)
    ui.page.keyboard.press("ArrowDown")
    assert _expanded(ui, MOBILE) == "true"
    assert _active_text(ui) == "ペルソナ生成"
    ui.page.keyboard.press("ArrowUp")
    assert _active_text(ui) == "ログアウト"

    ui.page.keyboard.press("Escape")

    assert _expanded(ui, MOBILE) == "false"
    assert ui.active("document.activeElement.dataset.navToggle") == "mobile"


def test_user_badge_reads_name_role_then_departments(nav: UiHarness) -> None:
    text = " ".join(nav.page.locator("[data-testid='user-badge']").inner_text().split())

    assert text.index("山田 花子") < text.index("admin") < text.index("企画部")


def test_base_page_has_no_axe_violations(nav: UiHarness) -> None:
    nav.check_a11y()
    nav.page.focus(INTERVIEW)
    nav.page.keyboard.press("ArrowDown")
    nav.check_a11y()


def test_admin_navigation_has_settings(nav: UiHarness) -> None:
    """U7 BR3.1: 「設定」は admin のナビゲーションにだけある"""
    assert nav.page.locator("[data-testid='nav-settings']").count() == 1
    assert nav.page.locator("[data-testid='nav-settings-mobile']").count() == 1


def test_member_navigation_has_no_settings(ui: UiHarness, test_app: Any) -> None:
    """U7 BR3.1、AC5.2.1: member のナビゲーション（広い画面・狭い画面）に「設定」がない"""
    from fastapi.testclient import TestClient

    from tests.conftest import MEMBER_A

    with TestClient(test_app, follow_redirects=False) as member:
        MEMBER_A.login(member)
        ui.page.set_viewport_size({"width": 1280, "height": 800})
        ui.load_screen(member, "/")

    assert ui.page.locator("[data-testid='nav-settings']").count() == 0
    assert ui.page.locator("[data-testid='nav-settings-mobile']").count() == 0
    assert ui.page.locator("a[href='/settings']").count() == 0
    ui.check_a11y()
