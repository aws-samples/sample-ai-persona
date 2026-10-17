"""ログインの URL（/login、/auth/callback、POST /signout）の API テスト。

約束 C11、US1.1（AC1.1.1、AC1.1.3）、US1.3（AC1.3.1、AC1.3.3）、BR1.2〜BR1.5、
BR1.13、BR1.22、NFR3.3、NFR3.5。

テストのアプリは AUTH_MODE=local で動く。oidc の振る舞いは、AuthManager を
oidc 用のもの（OidcService はモック）に差し替えて確かめる。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.managers.auth_manager import AuthManager, AuthSettings, LocalUser
from src.managers.shared.token_cipher import TokenCipher
from src.models.login_session import SessionStore
from src.services.memory_session_store import MemorySessionStore
from src.services.cognito.oidc_service import (
    AuthorizationRequest,
    OidcService,
    TokenSet,
)
from src.services.session_store_service import SessionStoreError

KEYS = json.dumps({"current": "v1", "k_v1": "api-test-key-material-0123456789"})


def _token_set() -> TokenSet:
    return TokenSet(
        access_token="access-1",
        refresh_token="refresh-1",
        expires_in=3600,
        claims={
            "sub": "user-1",
            "name": "山田 太郎",
            "email": "yamada@example.com",
            "cognito:groups": ["role:member", "group:sales"],
        },
    )


def _oidc_mock() -> Mock:
    oidc = Mock(spec=OidcService)
    oidc.create_authorization_request.return_value = AuthorizationRequest(
        url="https://cognito.example.com/oauth2/authorize?client_id=c",
        state="state-1",
        nonce="nonce-1",
        code_verifier="verifier-1",
    )
    oidc.exchange_code.return_value = _token_set()
    oidc.logout_url.side_effect = lambda uri: (
        f"https://cognito.example.com/logout?logout_uri={uri}"
    )
    return oidc


@pytest.fixture
def oidc_manager(monkeypatch) -> tuple[AuthManager, Mock]:
    """AuthManager を oidc の振る舞いに差し替える。"""
    from web.routers import auth

    oidc = _oidc_mock()
    manager = AuthManager(
        settings=AuthSettings(
            mode="oidc",
            app_base_url="https://app.example.com",
            local_user=LocalUser(display_name=None, email=None),
        ),
        session_store=MemorySessionStore(),
        oidc_service=oidc,
        cipher=TokenCipher(KEYS),
        clock=lambda: datetime.now(timezone.utc),
    )
    monkeypatch.setattr(auth, "_auth_manager", manager)
    return manager, oidc


@pytest.fixture
def browser(test_app) -> Any:
    """Cookie を持たないブラウザ（リダイレクトは追わない）。"""
    with TestClient(test_app, follow_redirects=False) as c:
        yield c


def _set_cookies(response: Any) -> list[str]:
    return response.headers.get_list("set-cookie")


def _session_cookie_header(response: Any) -> str:
    cookies = [c for c in _set_cookies(response) if c.startswith("session_id=")]
    assert len(cookies) == 1, _set_cookies(response)
    return cookies[0]


class TestLoginPage:
    def test_oidc_login_redirects_to_cognito_and_keeps_the_transaction(
        self, browser, oidc_manager
    ):
        response = browser.get("/login", params={"next": "/persona/management"})

        assert response.status_code == 302
        assert response.headers["location"].startswith(
            "https://cognito.example.com/oauth2/authorize"
        )
        cookies = _set_cookies(response)
        assert any(c.startswith("login_tx=") for c in cookies)
        assert not any(c.startswith("session_id=") for c in cookies)
        # 途中の状態は署名付き Cookie の中にあり、平文の state をそのまま出さない
        assert all("verifier-1" not in c for c in cookies)

    def test_expired_reason_shows_the_relogin_page(self, browser, oidc_manager):
        _, oidc = oidc_manager
        response = browser.get(
            "/login", params={"reason": "expired", "next": "/persona/management"}
        )

        assert response.status_code == 200
        assert "再度ログインしてください" in response.text
        assert 'href="/login?next=%2Fpersona%2Fmanagement"' in response.text
        oidc.create_authorization_request.assert_not_called()

    def test_signed_out_reason_does_not_log_in_again_under_local(self, browser):
        response = browser.get("/login", params={"reason": "signed_out"})

        assert response.status_code == 200
        assert "ログアウトしました" in response.text
        assert 'data-testid="login-button"' in response.text
        assert not any(c.startswith("session_id=") for c in _set_cookies(response))

    def test_local_login_creates_a_session_and_returns_to_next(self, browser):
        response = browser.get("/login", params={"next": "/persona/management"})

        assert response.status_code == 303
        assert response.headers["location"] == "/persona/management"
        cookie = _session_cookie_header(response).lower()
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert "secure" not in cookie  # local の http では付けない

    def test_external_next_falls_back_to_root(self, browser):
        response = browser.get("/login", params={"next": "https://evil.example.com"})
        assert response.headers["location"] == "/"


class TestClosedNetworkUrlsOutsideAdminApi:
    """U8: M2・M3 の URL は admin_api だけ。oidc・local の振る舞いは変わらない。"""

    def test_oidc_new_password_page_goes_to_login(self, browser, oidc_manager):
        response = browser.get("/auth/new-password")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_oidc_post_login_goes_to_login_without_reading_the_form(
        self, browser, oidc_manager
    ):
        from src.config import config

        _, oidc = oidc_manager
        response = browser.post(
            "/login",
            data={"email": "a@example.com", "password": "x"},
            headers={"Origin": config.APP_BASE_URL},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        oidc.create_authorization_request.assert_not_called()

    def test_challenge_expired_reason_under_oidc_still_logs_in(
        self, browser, oidc_manager
    ):
        response = browser.get("/login", params={"reason": "challenge_expired"})
        assert response.status_code == 302


class TestCallback:
    def test_success_sets_a_secure_session_cookie_and_returns_to_next(
        self, browser, oidc_manager
    ):
        browser.get("/login", params={"next": "/persona/management"})
        response = browser.get(
            "/auth/callback", params={"code": "code-1", "state": "state-1"}
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/persona/management"
        cookie = _session_cookie_header(response).lower()
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert "secure" in cookie

    @pytest.mark.parametrize(
        "params",
        [
            {"code": "code-1", "state": "forged"},
            {"error": "access_denied", "state": "state-1"},
        ],
        ids=["state_mismatch", "cancelled"],
    )
    def test_failure_shows_the_login_failed_page(self, browser, oidc_manager, params):
        browser.get("/login")
        response = browser.get("/auth/callback", params=params)

        assert response.status_code == 200
        assert "ログインできませんでした" in response.text
        assert 'data-testid="login-retry-button"' in response.text
        for detail in ("state_mismatch", "cognito_error", "access_denied"):
            assert detail not in response.text
        assert not any(c.startswith("session_id=") for c in _set_cookies(response))

    def test_callback_without_a_started_login_fails(self, browser, oidc_manager):
        response = browser.get(
            "/auth/callback", params={"code": "code-1", "state": "state-1"}
        )
        assert response.status_code == 200
        assert "ログインできませんでした" in response.text

    def test_unreachable_sessions_table_answers_503(self, browser, monkeypatch):
        from web.routers import auth

        broken = Mock(spec=SessionStore)
        broken.put.side_effect = SessionStoreError("down")
        broken.delete.return_value = True
        manager = AuthManager(
            settings=AuthSettings(
                mode="oidc",
                app_base_url="https://app.example.com",
                local_user=LocalUser(display_name=None, email=None),
            ),
            session_store=broken,
            oidc_service=_oidc_mock(),
            cipher=TokenCipher(KEYS),
        )
        monkeypatch.setattr(auth, "_auth_manager", manager)
        browser.get("/login")

        response = browser.get(
            "/auth/callback", params={"code": "code-1", "state": "state-1"}
        )
        assert response.status_code == 503

    def test_code_and_state_never_reach_the_logs(
        self, browser, oidc_manager, caplog, capsys
    ):
        manager, oidc = oidc_manager
        oidc.create_authorization_request.return_value = AuthorizationRequest(
            url="https://cognito.example.com/oauth2/authorize",
            state="SECRET-STATE-VALUE",
            nonce="nonce-1",
            code_verifier="SECRET-VERIFIER-VALUE",
        )
        caplog.set_level(logging.DEBUG)
        browser.get("/login")
        capsys.readouterr()

        browser.get(
            "/auth/callback",
            params={"code": "SECRET-CODE-VALUE", "state": "SECRET-STATE-VALUE"},
        )

        # httpx のロガーはテスト側のクライアントが要求の URL を出すもので、
        # アプリのログではないため除く
        app_logs = "\n".join(
            record.getMessage()
            for record in caplog.records
            if not record.name.startswith("httpx")
        )
        logged = app_logs + capsys.readouterr().out
        assert '"event": "http.access"' in logged
        for secret in (
            "SECRET-CODE-VALUE",
            "SECRET-STATE-VALUE",
            "SECRET-VERIFIER-VALUE",
        ):
            assert secret not in logged


class TestSignout:
    def test_signout_ends_the_session_and_clears_the_cookie(self, client):
        assert client.get("/persona/management").status_code == 200

        response = client.post("/signout", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/login?reason=signed_out"
        cookie = _session_cookie_header(response).lower()
        assert 'session_id=""' in cookie or "max-age=0" in cookie
        after = client.get("/persona/management", follow_redirects=False)
        assert after.status_code in (303, 401)

    def test_signout_is_503_when_the_session_cannot_be_deleted(
        self, client, monkeypatch
    ):
        """Do not report success (or clear the cookie) while the session still works."""
        assert client.get("/persona/management").status_code == 200

        def broken_delete(
            self: MemorySessionStore,
            session_id_hash: str,
            expected_version: int | None = None,
        ) -> bool:
            raise SessionStoreError("down")

        monkeypatch.setattr(MemorySessionStore, "delete", broken_delete)
        response = client.post(
            "/signout", headers={"Accept": "application/json"}, follow_redirects=False
        )

        assert response.status_code == 503
        assert not any(c.startswith("session_id=") for c in _set_cookies(response))
        monkeypatch.undo()
        retried = client.post("/signout", follow_redirects=False)
        assert retried.status_code == 303
        after = client.get("/persona/management", follow_redirects=False)
        assert after.status_code in (303, 401)

    def test_signout_without_a_session_still_redirects(self, test_app):
        with TestClient(
            test_app, headers={"HX-Request": "true"}, follow_redirects=False
        ) as anonymous:
            response = anonymous.post("/signout")
        assert response.status_code == 303

    def test_oidc_signout_goes_to_cognito_logout(self, oidc_manager, test_app):
        manager, oidc = oidc_manager
        # oidc の session_id は Secure のため、https のクライアントで確かめる
        with TestClient(
            test_app, base_url="https://testserver", follow_redirects=False
        ) as secure_browser:
            secure_browser.get("/login")
            secure_browser.get(
                "/auth/callback", params={"code": "code-1", "state": "state-1"}
            )
            response = secure_browser.post("/signout", headers={"HX-Request": "true"})

        assert response.status_code == 303
        assert response.headers["location"].startswith(
            "https://cognito.example.com/logout"
        )
        oidc.revoke.assert_called_once_with("refresh-1")
