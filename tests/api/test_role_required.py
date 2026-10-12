"""ロールのない利用者の入口（US1.4、BR2.8、BR2.13、NFR5.5、NFR6.1）の API テスト。

ロールのない利用者は、画面ならロール未割り当て画面（M4、``/no-role``）へ 303、htmx は
``HX-Redirect``、``/api/`` は 403（``ROLE_REQUIRED``）。M4・ログアウト・ログイン関連・
静的ファイルはそのまま通す。ロールを付けて再ログインすれば、すぐに使える。
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

from src.models.errors import CodedError, ErrorCode
from web.auth_middleware import NO_ROLE_PATH, ROLE_EXEMPT_PATHS, is_role_exempt_path
from web.error_messages import user_message_for


@pytest.fixture
def browser(test_app):
    """A browser-like client that does not follow redirects."""
    with TestClient(
        test_app,
        follow_redirects=False,
        headers={"X-Requested-With": "XMLHttpRequest"},
    ) as c:
        yield c


def _denied(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    return [line for line in lines if line.get("event") == "authz.denied"]


class TestRoleRequired:
    def test_page_goes_to_the_no_role_page(self, browser, no_role_user):
        no_role_user.login(browser)
        response = browser.get("/persona/management")
        assert response.status_code == 303
        assert response.headers["location"] == NO_ROLE_PATH
        assert NO_ROLE_PATH == "/no-role"

    def test_htmx_gets_hx_redirect(self, browser, no_role_user):
        no_role_user.login(browser)
        response = browser.get("/persona/management", headers={"HX-Request": "true"})
        assert response.status_code == 403
        assert response.headers["HX-Redirect"] == NO_ROLE_PATH

    def test_api_answers_403_role_required(self, browser, no_role_user):
        no_role_user.login(browser)
        response = browser.get("/api/personas")
        assert response.status_code == 403
        assert response.json() == {
            "detail": user_message_for(CodedError(code=ErrorCode.ROLE_REQUIRED)),
            "error_code": ErrorCode.ROLE_REQUIRED.value,
        }
        assert response.headers["cache-control"] == "no-store"

    @pytest.mark.parametrize(
        ("method", "path", "expected"),
        [
            ("GET", "/no-role", 200),
            ("POST", "/signout", 303),
            ("GET", "/static/css/custom.css", 200),
            ("GET", "/health", 200),
        ],
    )
    def test_these_urls_pass_without_a_role(
        self, browser, no_role_user, method, path, expected
    ):
        no_role_user.login(browser)
        assert browser.request(method, path).status_code == expected

    def test_login_is_reachable_without_a_role(self, browser, no_role_user):
        no_role_user.login(browser)
        response = browser.get("/login")
        assert response.headers.get("location") != NO_ROLE_PATH

    def test_a_role_works_right_after_logging_in_again(self, browser, no_role_user):
        """admin がロールを付けたあとの再ログインで、すぐに使える（AC1.4.4、NFR5.5）。"""
        no_role_user.login(browser)
        assert browser.get("/persona/management").status_code == 303

        browser.cookies.clear()
        with_role = no_role_user.__class__(
            no_role_user.name, no_role_user.email, ("member",), no_role_user.groups
        )
        with_role.login(browser)
        assert browser.get("/persona/management").status_code == 200

    def test_members_are_not_stopped(self, browser, member_a):
        member_a.login(browser)
        assert browser.get("/persona/management").status_code == 200

    def test_the_exempt_list_is_exactly_the_documented_one(self):
        assert ROLE_EXEMPT_PATHS == frozenset({"/no-role", "/signout"})

    def test_every_other_route_stops_a_user_without_role(self, test_app, no_role_user):
        """一覧にない URL は、ロールなしでは Manager まで届かない（BR2.8）。"""
        from tests.api.test_auth_middleware import _all_routes

        routes = _all_routes(test_app)
        with TestClient(
            test_app,
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
            raise_server_exceptions=False,
        ) as c:
            no_role_user.login(c)
            for method, path in routes:
                if is_role_exempt_path(path):
                    continue
                response = c.request(method, path)
                assert response.status_code in (303, 403), (method, path)

    def test_the_denial_is_logged(self, browser, no_role_user, capsys):
        no_role_user.login(browser)
        capsys.readouterr()
        browser.get("/api/personas")
        (line,) = _denied(capsys)
        assert line["reason"] == "role_required"
        assert line["sub"] == no_role_user.sub


class TestNoRolePage:
    """ロール未割り当て画面（M4、AC1.4.1、NFR6.1）。"""

    def _page(self, browser, user) -> str:
        user.login(browser)
        response = browser.get(NO_ROLE_PATH)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        return response.text

    def test_has_a_heading_and_explains_what_to_do(self, browser, no_role_user):
        html = self._page(browser, no_role_user)
        assert re.search(r"<h1[^>]*>\s*利用権限がまだありません\s*</h1>", html)
        assert "管理者に連絡してください" in html
        assert "ログインし直す" in html  # ロールが付いたら再ログインで使える旨

    def test_shows_who_is_signed_in(self, browser, no_role_user):
        html = self._page(browser, no_role_user)
        shown = re.search(r'data-testid="no-role-user"[^>]*>([^<]*)<', html)
        assert shown and no_role_user.name in shown.group(1)

    def test_signout_is_a_focusable_button_in_a_post_form(self, browser, no_role_user):
        html = self._page(browser, no_role_user)
        form = re.search(
            r'<form[^>]*action="/signout"[^>]*>(.*?)</form>', html, re.DOTALL
        )
        assert form and 'method="post"' in form.group(0)
        button = re.search(r"<button[^>]*>", form.group(1))
        assert button and 'data-testid="no-role-signout"' in button.group(0)
        assert 'type="submit"' in button.group(0)
        assert 'tabindex="-1"' not in button.group(0)

    def test_has_no_navigation(self, browser, no_role_user):
        html = self._page(browser, no_role_user)
        assert "<nav" not in html
        assert 'href="/persona' not in html

    def test_a_user_with_a_role_goes_to_the_top(self, browser, member_a):
        member_a.login(browser)
        response = browser.get(NO_ROLE_PATH)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
