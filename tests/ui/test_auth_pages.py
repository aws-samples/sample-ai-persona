"""M2（閉域構成のログイン）と M3（初回パスワード変更）の画面の振る舞い（U8、NFR6.23）。

テンプレートをアプリの Jinja 環境で描き、ヘッドレスの Chromium で開く。送信先
（``/login``、``/auth/new-password``）はハーネスで止め、送信中の表示を確かめる。
mockups.md M2・M3、interaction-spec.md C8。
"""

from typing import Any

import pytest

from src.models.password_policy import PasswordPolicy
from tests.ui.conftest import UiHarness, render_page

POLICY = PasswordPolicy(
    min_length=8,
    require_uppercase=True,
    require_lowercase=True,
    require_digits=True,
    require_symbols=False,
)


def _m2(**context: Any) -> str:
    params: dict[str, Any] = {
        "title": "ログイン",
        "next": "/persona",
        "email": "",
        "notice": None,
        "error": None,
    }
    params.update(context)
    return render_page("auth/login.html", **params)


def _m3(missed: set[str] | None = None, **context: Any) -> str:
    from web.routers.auth import _password_rules

    missed_rules = None if missed is None else frozenset(missed)
    params: dict[str, Any] = {
        "title": "パスワードの設定",
        "policy": POLICY,
        "rules": _password_rules(POLICY, missed_rules),  # type: ignore[arg-type]
        "error": None,
        "error_field": None,
    }
    params.update(context)
    return render_page("auth/new_password.html", **params)


@pytest.mark.parametrize(
    "html",
    [
        pytest.param(lambda: _m2(), id="m2-initial"),
        pytest.param(
            lambda: _m2(
                email="yamada@example.com",
                error="メールアドレスまたはパスワードが違います",
            ),
            id="m2-failed",
        ),
        pytest.param(lambda: _m2(notice="challenge_expired"), id="m2-notice"),
        pytest.param(lambda: _m3(), id="m3-initial"),
        pytest.param(
            lambda: _m3(
                missed={"uppercase", "digits"},
                error="パスワードが条件を満たしていません",
                error_field="new_password",
            ),
            id="m3-policy-miss",
        ),
        pytest.param(
            lambda: _m3(
                missed=set(),
                error="確認用のパスワードが一致しません",
                error_field="confirmation",
            ),
            id="m3-mismatch",
        ),
    ],
)
def test_states_have_no_axe_violations(ui: UiHarness, html: Any) -> None:
    ui.load(html())
    ui.check_a11y()


def test_m2_fields_are_labelled_and_the_error_is_announced(ui: UiHarness) -> None:
    ui.load(
        _m2(
            email="yamada@example.com", error="メールアドレスまたはパスワードが違います"
        )
    )
    page = ui.page
    assert page.get_by_label("メールアドレス").get_attribute("type") == "email"
    assert page.get_by_label("パスワード").input_value() == ""
    assert page.get_by_role("alert").inner_text() == (
        "メールアドレスまたはパスワードが違います"
    )
    # 失敗のあとはパスワード欄にフォーカス（BR1.4）
    assert ui.active() == "login-password"


def test_m2_initial_focus_is_the_email(ui: UiHarness) -> None:
    ui.load(_m2())
    assert ui.active() == "login-email"


def _stop_navigation(ui: UiHarness) -> None:
    """Record the submit and keep the page (a held navigation blocks locators)."""
    ui.page.evaluate(
        """() => window.addEventListener("submit", (event) => {
            window.__submitted = (window.__submitted || 0) + 1;
            event.preventDefault();
        })"""
    )


def test_m2_can_be_sent_with_the_keyboard_and_disables_the_button(
    ui: UiHarness,
) -> None:
    ui.load(_m2())
    _stop_navigation(ui)
    page = ui.page
    page.keyboard.type("yamada@example.com")
    page.keyboard.press("Tab")
    page.keyboard.type("Secret-123")
    page.keyboard.press("Enter")
    ui.settle()

    assert page.evaluate("window.__submitted") == 1
    button = page.get_by_test_id("login-submit-button")
    assert button.is_disabled()
    assert "ログイン中..." in button.inner_text()
    # 送信中は二度目の送信をしない
    page.keyboard.press("Enter")
    ui.settle()
    assert page.evaluate("window.__submitted") == 1


def test_m3_rules_are_tied_to_the_new_password_field(ui: UiHarness) -> None:
    ui.load(_m3())
    field = ui.page.get_by_label("新しいパスワード", exact=True)
    described = field.get_attribute("aria-describedby") or ""
    assert "password-rules" in described.split()
    items = ui.page.locator("#password-rules li")
    assert items.count() == len(POLICY.rules())
    # 色だけにしない: 記号と文字の両方がある
    first = items.first.inner_text()
    assert "✗" in first and "満たしていない" in first


def test_m3_rules_follow_the_typing(ui: UiHarness) -> None:
    ui.load(_m3())
    page = ui.page
    page.get_by_label("新しいパスワード", exact=True).fill("abcdefgh")
    ui.settle()
    min_length = page.get_by_test_id("password-rule-min_length")
    upper = page.get_by_test_id("password-rule-uppercase")
    assert min_length.get_attribute("data-met") == "true"
    assert "満たしている" in min_length.inner_text()
    assert upper.get_attribute("data-met") == "false"


def test_m3_missed_rules_are_listed_after_submission(ui: UiHarness) -> None:
    ui.load(
        _m3(
            missed={"uppercase"},
            error="パスワードが条件を満たしていません",
            error_field="new_password",
        )
    )
    page = ui.page
    assert (
        page.get_by_test_id("password-rule-uppercase").get_attribute("data-met")
        == "false"
    )
    assert page.get_by_test_id("password-rule-digits").get_attribute("data-met") == (
        "true"
    )
    assert (
        page.get_by_role("alert").inner_text() == "パスワードが条件を満たしていません"
    )
    assert ui.active() == "new-password"


def test_m3_can_be_sent_with_the_keyboard_and_disables_the_button(
    ui: UiHarness,
) -> None:
    ui.load(_m3())
    _stop_navigation(ui)
    page = ui.page
    page.keyboard.type("NewSecret-1a")
    page.keyboard.press("Tab")
    page.keyboard.type("NewSecret-1a")
    page.keyboard.press("Enter")
    ui.settle()

    assert page.evaluate("window.__submitted") == 1
    button = page.get_by_test_id("new-password-submit-button")
    assert button.is_disabled()
    assert "設定中..." in button.inner_text()
