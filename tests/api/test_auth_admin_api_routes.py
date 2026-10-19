"""閉域構成のログイン（AUTH_MODE=admin_api）の URL の API テスト（U8）。

GET/POST /login（M2）と GET/POST /auth/new-password（M3）。テストのアプリは
AUTH_MODE=local で起動しているため、AuthManager を admin_api 用のもの
（CognitoAuthService は Mock、ストアは MemorySessionStore）に差し替える。
session_id は Secure のため、https のクライアントで確かめる。

約束 C11、BR1.1〜BR1.4、BR2.4〜BR2.6、BR4.5、BR4.6、NFR3.15、NFR3.16、NFR4.21、
NFR9.45、AC1.2.1〜AC1.2.5。
"""

import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.config import config
from src.managers.auth_manager import AuthManager, AuthSettings, LocalUser
from src.managers.shared.token_cipher import TokenCipher
from src.models.errors import ErrorCode, LookupUnavailableError
from src.models.password_policy import PasswordPolicy
from src.services.cognito.cognito_auth_service import (
    CognitoAuthError,
    CognitoAuthService,
    CognitoChallenge,
    CognitoTokens,
)
from src.services.memory_session_store import MemorySessionStore
from src.services.session_store_service import SessionStoreError
from tests.log_helpers import SENTINEL_PASSWORD, assert_not_leaked

KEYS = json.dumps({"current": "v1", "k_v1": "api-test-key-material-0123456789"})
POOL_ID = "ap-northeast-1_ClosedPool"
CLIENT_ID = "closed-client-id"
SUB = "0b5c0f2e-user-sub"
EMAIL = "yamada@example.com"
NEW_PASSWORD = "NewPw-SENTINEL-1a"
ORIGIN = {"Origin": config.APP_BASE_URL}
POLICY = PasswordPolicy(
    min_length=8,
    require_uppercase=True,
    require_lowercase=True,
    require_digits=True,
    require_symbols=False,
)


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _id_token() -> str:
    claims = {
        "iss": f"https://cognito-idp.ap-northeast-1.amazonaws.com/{POOL_ID}",
        "aud": CLIENT_ID,
        "token_use": "id",
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        "sub": SUB,
        "cognito:username": SUB,
        "cognito:groups": ["role:member"],
        "email": EMAIL,
    }
    return f"{_b64({'alg': 'RS256'})}.{_b64(claims)}.sig"


def _tokens() -> CognitoTokens:
    return CognitoTokens(
        id_token=_id_token(),
        access_token="access-1",
        refresh_token="refresh-1",
        expires_in=3600,
    )


def _challenge() -> CognitoChallenge:
    return CognitoChallenge(
        name="NEW_PASSWORD_REQUIRED",
        session="cognito-session-0123456789",
        parameters={"USER_ID_FOR_SRP": SUB, "requiredAttributes": "[]"},
    )


@pytest.fixture
def store() -> MemorySessionStore:
    return MemorySessionStore()


@pytest.fixture
def cognito() -> Mock:
    mock = Mock(spec=CognitoAuthService)
    mock.initiate_auth.return_value = _tokens()
    mock.respond_new_password.return_value = _tokens()
    return mock


@pytest.fixture
def admin_manager(monkeypatch, store, cognito) -> AuthManager:
    from web.routers import auth

    manager = AuthManager(
        settings=AuthSettings(
            mode="admin_api",
            app_base_url="https://app.example.com",
            local_user=LocalUser(display_name=None, email=None),
            user_pool_id=POOL_ID,
            client_id=CLIENT_ID,
            password_policy=POLICY,
        ),
        session_store=store,
        cognito_auth_service=cognito,
        cipher=TokenCipher(KEYS),
    )
    monkeypatch.setattr(auth, "_auth_manager", manager)
    return manager


@pytest.fixture
def browser(test_app, admin_manager) -> Any:
    with TestClient(
        test_app, base_url="https://testserver", follow_redirects=False
    ) as c:
        yield c


def _post_login(browser: Any, **form: str) -> Any:
    data = {"email": EMAIL, "password": SENTINEL_PASSWORD, "next": "/persona"}
    data.update(form)
    data = {k: v for k, v in data.items() if v is not None}
    return browser.post("/login", data=data, headers=ORIGIN)


def _post_new_password(browser: Any, **form: str) -> Any:
    data = {"new_password": NEW_PASSWORD, "confirmation": NEW_PASSWORD}
    data.update(form)
    data = {k: v for k, v in data.items() if v is not None}
    return browser.post("/auth/new-password", data=data, headers=ORIGIN)


def _to_new_password(browser: Any, cognito: Mock) -> None:
    cognito.initiate_auth.return_value = _challenge()
    response = _post_login(browser)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/new-password"


def _session_cookies(response: Any) -> list[str]:
    return [
        c
        for c in response.headers.get_list("set-cookie")
        if c.startswith("session_id=")
    ]


def _input_value(html: str, name: str) -> str | None:
    match = re.search(rf'<input[^>]*name="{name}"[^>]*>', html)
    assert match, f"no input named {name}"
    value = re.search(r'value="([^"]*)"', match.group(0))
    return value.group(1) if value else None


class TestLoginPage:
    def test_get_login_shows_m2_and_never_goes_to_cognito(self, browser, cognito):
        response = browser.get("/login", params={"next": "/persona/management"})

        assert response.status_code == 200
        assert 'data-testid="login-form"' in response.text
        assert _input_value(response.text, "next") == "/persona/management"
        assert 'type="email"' in response.text
        assert "no-store" in response.headers["cache-control"]
        cognito.initiate_auth.assert_not_called()

    def test_external_next_is_dropped(self, browser):
        response = browser.get("/login", params={"next": "https://evil.example.com"})
        assert _input_value(response.text, "next") == "/"

    @pytest.mark.parametrize(
        ("reason", "testid"),
        [
            ("expired", "login-notice-expired"),
            ("signed_out", "login-notice-signed_out"),
            ("challenge_expired", "login-notice-challenge_expired"),
        ],
    )
    def test_reasons_show_a_notice_on_m2(self, browser, reason, testid):
        response = browser.get("/login", params={"reason": reason})
        assert response.status_code == 200
        assert f'data-testid="{testid}"' in response.text
        assert 'data-testid="login-form"' in response.text

    def test_unknown_reason_shows_no_notice(self, browser):
        response = browser.get("/login", params={"reason": "<script>"})
        assert "login-notice" not in response.text
        assert "<script>" not in response.text

    def test_callback_is_the_login_failed_page(self, browser):
        response = browser.get("/auth/callback", params={"code": "c", "state": "s"})
        assert response.status_code == 200
        assert "login-form" not in response.text


class TestLoginSubmit:
    def test_success_sets_a_secure_session_and_goes_to_next(self, browser, cognito):
        response = _post_login(browser)

        assert response.status_code == 303
        assert response.headers["location"] == "/persona"
        cookie = _session_cookies(response)[0].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
        cognito.initiate_auth.assert_called_once_with(EMAIL, SENTINEL_PASSWORD)

    def test_first_login_goes_to_m3_without_a_session(self, browser, cognito):
        cognito.initiate_auth.return_value = _challenge()
        response = _post_login(browser)

        assert response.status_code == 303
        assert response.headers["location"] == "/auth/new-password"
        assert _session_cookies(response) == []
        assert any(
            c.startswith("login_tx=") for c in response.headers.get_list("set-cookie")
        )

    @pytest.mark.parametrize(
        "error",
        [
            CognitoAuthError("x", code=ErrorCode.LOGIN_FAILED),
            CognitoAuthError("x", unexpected=False, code=ErrorCode.LOGIN_FAILED),
        ],
    )
    def test_wrong_password_shows_m2_keeping_the_email(self, browser, cognito, error):
        cognito.initiate_auth.side_effect = error
        response = _post_login(browser)

        assert response.status_code == 200
        assert 'data-testid="login-error"' in response.text
        assert 'role="alert"' in response.text
        assert _input_value(response.text, "email") == EMAIL
        assert _input_value(response.text, "password") in (None, "")
        assert _session_cookies(response) == []

    def test_every_login_failed_reason_renders_the_same_body(self, browser, cognito):
        """BR1.4 / NFR4.19: 理由で応答の本文が変わらない。"""
        bodies = []
        cognito.initiate_auth.side_effect = CognitoAuthError(
            "x", code=ErrorCode.LOGIN_FAILED
        )
        bodies.append(_post_login(browser).text)
        bodies.append(_post_login(browser, password="").text)  # 入力の誤り
        cognito.initiate_auth.side_effect = None
        cognito.initiate_auth.return_value = CognitoChallenge(
            name="SMS_MFA", session="s" * 30, parameters={}
        )
        bodies.append(_post_login(browser).text)  # 対応外の challenge

        def normalized(body: str) -> str:
            return re.sub(r'name="csrf[^"]*" value="[^"]*"', "", body)

        assert len({normalized(b) for b in bodies}) == 1

    @pytest.mark.parametrize(
        ("code", "testid"),
        [
            (ErrorCode.LOGIN_RATE_LIMITED, "login-error"),
            (ErrorCode.LOGIN_UNAVAILABLE, "login-error"),
        ],
    )
    def test_rate_limit_and_unavailable_are_shown_on_m2(
        self, browser, cognito, code, testid
    ):
        cognito.initiate_auth.side_effect = CognitoAuthError("x", code=code)
        response = _post_login(browser)
        assert response.status_code == 200
        assert f'data-testid="{testid}"' in response.text
        assert _input_value(response.text, "email") == EMAIL

    @pytest.mark.parametrize("missing", ["email", "password", "next"])
    def test_a_missing_field_is_m2_not_422(self, browser, cognito, missing):
        response = _post_login(browser, **{missing: None})
        assert response.status_code in (200, 303)
        assert response.status_code != 422
        if missing != "next":
            assert 'data-testid="login-error"' in response.text
            cognito.initiate_auth.assert_not_called()

    def test_sessions_table_down_is_503(self, browser, cognito, store, monkeypatch):
        cognito.initiate_auth.return_value = _challenge()

        def broken(challenge: Any) -> None:
            raise SessionStoreError("down")

        monkeypatch.setattr(store, "put_challenge", broken)
        response = _post_login(browser)
        assert response.status_code == 503


class TestNewPasswordPage:
    def test_get_shows_m3_with_the_rules(self, browser, cognito):
        _to_new_password(browser, cognito)
        response = browser.get("/auth/new-password")

        assert response.status_code == 200
        assert 'data-testid="new-password-form"' in response.text
        assert 'id="password-rules"' in response.text
        assert 'aria-describedby="password-rules"' in response.text
        for rule in POLICY.rules():
            assert f'data-testid="password-rule-{rule.value}"' in response.text
        assert "no-store" in response.headers["cache-control"]

    def test_get_without_a_challenge_goes_back_to_login(self, browser):
        response = browser.get("/auth/new-password")
        assert response.status_code == 303
        assert response.headers["location"] == "/login?reason=challenge_expired"

    def test_reachable_without_a_login(self, test_app, admin_manager):
        with TestClient(
            test_app, base_url="https://testserver", follow_redirects=False
        ) as anonymous:
            response = anonymous.get("/auth/new-password")
        assert response.headers["location"].startswith("/login?reason=challenge")

    def test_success_signs_in_and_goes_to_next(self, browser, cognito):
        _to_new_password(browser, cognito)
        response = _post_new_password(browser)

        assert response.status_code == 303
        assert response.headers["location"] == "/persona"
        assert len(_session_cookies(response)) == 1
        cognito.respond_new_password.assert_called_once()

    def test_policy_miss_shows_m3_with_the_missed_rules(self, browser, cognito):
        _to_new_password(browser, cognito)
        response = _post_new_password(browser, new_password="weak", confirmation="weak")

        assert response.status_code == 200
        assert 'data-testid="new-password-error"' in response.text
        assert 'data-met="false"' in response.text
        assert _input_value(response.text, "new_password") in (None, "")
        cognito.respond_new_password.assert_not_called()

    def test_confirmation_mismatch_shows_m3(self, browser, cognito):
        _to_new_password(browser, cognito)
        response = _post_new_password(browser, confirmation="Different-1a")
        assert response.status_code == 200
        assert 'data-testid="confirmation-error"' in response.text
        cognito.respond_new_password.assert_not_called()

    def test_expired_challenge_goes_back_to_login(self, browser, cognito):
        _to_new_password(browser, cognito)
        cognito.respond_new_password.side_effect = CognitoAuthError(
            "x", code=ErrorCode.LOGIN_CHALLENGE_EXPIRED
        )
        response = _post_new_password(browser)
        assert response.status_code == 303
        assert response.headers["location"] == "/login?reason=challenge_expired"

    def test_unavailable_shows_m3_and_can_be_resent(self, browser, cognito):
        _to_new_password(browser, cognito)
        cognito.respond_new_password.side_effect = CognitoAuthError("x")
        response = _post_new_password(browser)
        assert response.status_code == 200
        assert 'data-testid="new-password-error"' in response.text

        cognito.respond_new_password.side_effect = None
        assert _post_new_password(browser).status_code == 303

    @pytest.mark.parametrize("missing", ["new_password", "confirmation"])
    def test_a_missing_field_is_m3_not_422(self, browser, cognito, missing):
        _to_new_password(browser, cognito)
        response = _post_new_password(browser, **{missing: None})
        assert response.status_code == 200
        cognito.respond_new_password.assert_not_called()

    def test_post_without_a_challenge_goes_back_to_login(self, browser):
        response = _post_new_password(browser)
        assert response.status_code == 303
        assert response.headers["location"] == "/login?reason=challenge_expired"


class TestCsrf:
    @pytest.mark.parametrize("path", ["/login", "/auth/new-password"])
    @pytest.mark.parametrize("origin", [None, "https://evil.example.com"])
    def test_posts_need_the_app_origin(self, browser, cognito, path, origin):
        headers = {} if origin is None else {"Origin": origin}
        response = browser.post(path, data={"email": EMAIL}, headers=headers)
        assert response.status_code == 403
        assert response.json()["error_code"] == ErrorCode.CSRF_FAILED.value
        cognito.initiate_auth.assert_not_called()


class TestPasswordNeverLeaks:
    @pytest.mark.parametrize(
        "error",
        [
            CognitoAuthError("x", code=ErrorCode.LOGIN_FAILED),
            CognitoAuthError("x", code=ErrorCode.LOGIN_UNAVAILABLE),
            CognitoAuthError("x", unexpected=True, error_type="ForbiddenException"),
        ],
    )
    def test_login_failures(self, browser, cognito, caplog, capsys, error):
        caplog.set_level(logging.DEBUG)
        cognito.initiate_auth.side_effect = error
        response = _post_login(browser)
        assert_not_leaked(
            SENTINEL_PASSWORD, response.text, caplog, capsys.readouterr().out
        )

    def test_new_password_paths(self, browser, cognito, caplog, capsys):
        caplog.set_level(logging.DEBUG)
        _to_new_password(browser, cognito)
        bodies = [
            _post_new_password(browser, new_password="weak", confirmation="weak").text,
            _post_new_password(browser, confirmation="Other-Pw-1a").text,
        ]
        cognito.respond_new_password.side_effect = CognitoAuthError("x")
        bodies.append(_post_new_password(browser).text)
        cognito.respond_new_password.side_effect = None
        bodies.append(_post_new_password(browser).text)
        out = capsys.readouterr().out
        for secret in (SENTINEL_PASSWORD, NEW_PASSWORD, "Other-Pw-1a"):
            assert_not_leaked(secret, bodies, caplog, out)

    def test_missing_field_posts(self, browser, cognito, caplog, capsys):
        caplog.set_level(logging.DEBUG)
        response = _post_login(browser, email=None)
        _to_new_password(browser, cognito)
        other = _post_new_password(browser, new_password=None)
        assert_not_leaked(
            SENTINEL_PASSWORD,
            response.text,
            other.text,
            caplog,
            capsys.readouterr().out,
        )


# =============================================================================
# Sessions テーブルに LoginChallenge を書けない・読めない（NFR9.45、Loop-back 1）
# =============================================================================

#: Cognito が返す session の目印。本物の値ではない。
COGNITO_SESSION = "cognito-session-SENTINEL-7d2e9b"
SESSIONS_TABLE = "SessionsLoopBack"
REGION = "ap-northeast-1"


def _sentinel_challenge() -> CognitoChallenge:
    return CognitoChallenge(
        name="NEW_PASSWORD_REQUIRED",
        session=COGNITO_SESSION,
        parameters={"USER_ID_FOR_SRP": SUB, "requiredAttributes": "[]"},
    )


def _fail(store: Any, operation: str) -> None:
    """Make one DynamoDB call of the moto-backed store fail like a real outage."""
    from botocore.exceptions import ClientError

    if not isinstance(store.client, Mock):
        store.client = Mock(wraps=store.client)
    getattr(store.client, operation).side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "injected"}},
        operation,
    )


def _table_items() -> list[dict[str, Any]]:
    """Every item of the moto Sessions table, read with a client of its own."""
    import boto3

    return boto3.client("dynamodb", region_name=REGION).scan(TableName=SESSIONS_TABLE)[
        "Items"
    ]


def _challenge_items() -> list[dict[str, Any]]:
    return [
        item
        for item in _table_items()
        if item.get("item_type", {}).get("S") == "challenge"
        or item["session_id_hash"]["S"].startswith("challenge#")
    ]


def _login_tx_payloads(response: Any, browser: Any) -> list[dict[str, Any]]:
    """Decoded ``login_tx`` cookies of the response and of the client's jar."""
    values = [
        c.split(";", 1)[0].split("=", 1)[1]
        for c in response.headers.get_list("set-cookie")
        if c.startswith("login_tx=")
    ]
    if browser.cookies.get("login_tx"):
        values.append(browser.cookies.get("login_tx"))
    payloads = []
    for value in values:
        data = value.strip('"').split(".", 1)[0]
        payloads.append(json.loads(base64.b64decode(data + "=" * (-len(data) % 4))))
    return payloads


def _cookie_texts(response: Any, browser: Any) -> list[str]:
    return [*response.headers.get_list("set-cookie"), *browser.cookies.values()]


class TestChallengeStoreFailureLeavesNothing:
    """NFR9.45: 503 and the Cognito session is kept nowhere (moto Sessions table)."""

    @pytest.fixture
    def store(self, monkeypatch) -> Any:
        moto = pytest.importorskip("moto", reason="moto is required")
        import boto3

        from src.services.session_store_service import SessionStoreService

        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        with moto.mock_aws():
            boto3.client("dynamodb", region_name=REGION).create_table(
                TableName=SESSIONS_TABLE,
                KeySchema=[{"AttributeName": "session_id_hash", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "session_id_hash", "AttributeType": "S"}
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            yield SessionStoreService(table_name=SESSIONS_TABLE, region=REGION)

    def _assert_session_nowhere(
        self, response: Any, browser: Any, caplog: Any, out: str
    ) -> None:
        assert_not_leaked(
            COGNITO_SESSION,
            response.text,
            _cookie_texts(response, browser),
            _login_tx_payloads(response, browser),
            _table_items(),
            caplog,
            out,
        )

    def test_unwritable_challenge_is_503_and_keeps_no_session(
        self, browser, admin_manager, cognito, store, caplog, capsys
    ):
        caplog.set_level(logging.DEBUG)
        cognito.initiate_auth.return_value = _sentinel_challenge()
        _fail(store, "put_item")

        response = _post_login(browser)

        assert response.status_code == 503
        assert 'data-testid="lookup-unavailable"' in response.text
        assert "new-password-form" not in response.text
        assert response.headers.get("location") is None
        assert store.client.put_item.call_count == 1  # 書き込みは試みた
        assert _challenge_items() == []
        assert _session_cookies(response) == []
        for payload in _login_tx_payloads(response, browser):
            assert "new_password_challenge" not in payload
        self._assert_session_nowhere(response, browser, caplog, capsys.readouterr().out)
        # Manager の例外（文・context・連鎖した原因）にも session が載らない
        with pytest.raises(LookupUnavailableError) as raised:
            admin_manager.login_admin_api(
                email=EMAIL,
                password=SENTINEL_PASSWORD,
                next_url="/persona",
                previous_session_id=None,
            )
        assert_not_leaked(COGNITO_SESSION, raised.value)
        assert_not_leaked(SENTINEL_PASSWORD, raised.value, response.text)

    def test_unreadable_challenge_is_503_and_m3_is_not_shown(
        self, browser, cognito, store, caplog, capsys
    ):
        caplog.set_level(logging.DEBUG)
        cognito.initiate_auth.return_value = _sentinel_challenge()
        started = _post_login(browser)
        assert started.status_code == 303
        # 対照: 書ければ challenge が 1 件あり、login_tx に challenge の ID が入る
        assert len(_challenge_items()) == 1
        assert any(
            "new_password_challenge" in p for p in _login_tx_payloads(started, browser)
        )
        _fail(store, "get_item")

        shown = browser.get("/auth/new-password")
        sent = _post_new_password(browser)

        for response in (shown, sent):
            assert response.status_code == 503
            assert 'data-testid="lookup-unavailable"' in response.text
            assert "new-password-form" not in response.text
            assert "password-rules" not in response.text
        cognito.respond_new_password.assert_not_called()
        out = capsys.readouterr().out
        for response in (shown, sent):
            assert_not_leaked(
                COGNITO_SESSION,
                response.text,
                _cookie_texts(response, browser),
                _login_tx_payloads(response, browser),
                caplog,
                out,
            )

    def test_failed_take_out_is_503_and_cognito_is_not_called(
        self, browser, cognito, store, caplog, capsys
    ):
        caplog.set_level(logging.DEBUG)
        cognito.initiate_auth.return_value = _sentinel_challenge()
        assert _post_login(browser).status_code == 303
        _fail(store, "delete_item")

        response = _post_new_password(browser)

        assert response.status_code == 503
        assert 'data-testid="lookup-unavailable"' in response.text
        assert "new-password-form" not in response.text
        assert store.client.delete_item.call_count == 1
        cognito.respond_new_password.assert_not_called()
        assert _session_cookies(response) == []
        out = capsys.readouterr().out
        assert_not_leaked(
            COGNITO_SESSION,
            response.text,
            _cookie_texts(response, browser),
            _login_tx_payloads(response, browser),
            caplog,
            out,
        )
        assert_not_leaked(NEW_PASSWORD, response.text, caplog, out)
