"""AuthManager の AUTH_MODE=admin_api（閉域構成のログイン、U8）の単体テスト。

CognitoAuthService は Mock、ストアは本物の MemorySessionStore（呼び出しの記録のため
Mock(wraps=...)）。時刻は clock で与える。パスワードには目印の文字列を使い、ログ・
例外・保存した項目に現れないことを確かめる（NFR3.15、AC1.2.5）。

BR1.2〜BR1.6、BR2.1〜BR2.7、BR3.1〜BR3.3、BR4.1、BR4.5、NFR4.19、NFR4.22、
NFR4.23、NFR5.7、NFR8.28、NFR8.29、NFR9.40〜NFR9.42、NFR9.45、NFR9.46。
"""

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from src.managers.auth_manager import (
    LOGIN_FAILURE_REASONS,
    AdminLoginError,
    AuthConfigError,
    AuthManager,
    AuthSettings,
    LocalUser,
    LoginResult,
    NewPasswordStart,
)
from src.managers.shared.token_cipher import TokenCipher
from src.models.authz import Role
from src.models.errors import (
    AuthRequiredError,
    ErrorCode,
    LookupUnavailableError,
)
from src.models.login_challenge import challenge_key
from src.models.login_session import AuthMethod
from src.models.password_policy import PasswordPolicy, PasswordRule
from src.services.cognito.cognito_auth_service import (
    CognitoAuthError,
    CognitoAuthService,
    CognitoChallenge,
    CognitoTokens,
)
from src.services.stores.memory_session_store import MemorySessionStore
from src.services.session_store_service import SessionStoreError
from tests.error_helpers import raises_code
from tests.log_helpers import SENTINEL_PASSWORD, LogCapture, assert_not_leaked

T0 = datetime(2026, 10, 17, 0, 0, 0, tzinfo=timezone.utc)
KEYS = json.dumps({"current": "v1", "k_v1": "unit-test-key-material-0123456789"})
POOL_ID = "ap-northeast-1_ClosedPool"
CLIENT_ID = "closed-client-id"
ISSUER = f"https://cognito-idp.ap-northeast-1.amazonaws.com/{POOL_ID}"
SUB = "0b5c0f2e-user-sub"
EMAIL = "yamada@example.com"
NEW_PASSWORD = "NewPw-SENTINEL-1a"
POLICY = PasswordPolicy(
    min_length=8,
    require_uppercase=True,
    require_lowercase=True,
    require_digits=True,
    require_symbols=False,
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


def _b64(data: dict[str, Any]) -> str:
    raw = json.dumps(data).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _id_token(**overrides: Any) -> str:
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "token_use": "id",
        "exp": int((T0 + timedelta(hours=1)).timestamp()),
        "sub": SUB,
        "cognito:username": SUB,
        "cognito:groups": ["role:member", "group:sales"],
        "name": "山田 太郎",
        "email": EMAIL,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return f"{_b64({'alg': 'RS256', 'kid': 'k'})}.{_b64(claims)}.not-a-real-signature"


def _tokens(
    id_token: str | None = None, refresh: str | None = "refresh-1"
) -> CognitoTokens:
    return CognitoTokens(
        id_token=id_token or _id_token(),
        access_token="access-1",
        refresh_token=refresh,
        expires_in=3600,
    )


def _new_password_challenge(required: str = "[]") -> CognitoChallenge:
    return CognitoChallenge(
        name="NEW_PASSWORD_REQUIRED",
        session="cognito-session-0123456789",
        parameters={"USER_ID_FOR_SRP": SUB, "requiredAttributes": required},
    )


def _settings(mode: str = "admin_api") -> AuthSettings:
    return AuthSettings(
        mode=mode,
        app_base_url="https://app.example.com",
        local_user=LocalUser(display_name=None, email=None),
        user_pool_id=POOL_ID,
        client_id=CLIENT_ID,
        password_policy=POLICY,
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store() -> Mock:
    return Mock(wraps=MemorySessionStore())


@pytest.fixture
def cognito() -> Mock:
    mock = Mock(spec=CognitoAuthService)
    mock.initiate_auth.return_value = _tokens()
    mock.respond_new_password.return_value = _tokens()
    mock.refresh.return_value = _tokens(refresh="refresh-1")
    return mock


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(KEYS)


def _manager(store, cognito, cipher, clock) -> AuthManager:
    return AuthManager(
        settings=_settings(),
        session_store=store,
        cognito_auth_service=cognito,
        cipher=cipher,
        clock=clock,
    )


@pytest.fixture
def manager(store, cognito, cipher, clock) -> AuthManager:
    return _manager(store, cognito, cipher, clock)


def _login(manager: AuthManager, **overrides: Any) -> Any:
    params: dict[str, Any] = {
        "email": EMAIL,
        "password": SENTINEL_PASSWORD,
        "next_url": "/persona",
        "previous_session_id": None,
    }
    params.update(overrides)
    return manager.login_admin_api(**params)


def _start_challenge(manager: AuthManager, cognito: Mock) -> str:
    cognito.initiate_auth.return_value = _new_password_challenge()
    start = _login(manager)
    assert isinstance(start, NewPasswordStart)
    return start.challenge_id


def _complete(manager: AuthManager, challenge_id: str | None, **overrides: Any) -> Any:
    params: dict[str, Any] = {
        "challenge_id": challenge_id,
        "new_password": NEW_PASSWORD,
        "confirmation": NEW_PASSWORD,
        "previous_session_id": None,
    }
    params.update(overrides)
    return manager.complete_new_password(**params)


def _stored_items(store: Mock) -> list[Any]:
    inner: MemorySessionStore = store._mock_wraps
    return [*inner._items.values(), *inner._challenges.values()]


# =============================================================================
# 起動時の検査（BR4.1）
# =============================================================================


class TestStartupCheck:
    _COMPLETE = {
        "AUTH_MODE": "admin_api",
        "COGNITO_USER_POOL_ID": POOL_ID,
        "COGNITO_CLIENT_ID": CLIENT_ID,
        "COGNITO_CLIENT_SECRET": "secret",
        "TOKEN_ENCRYPTION_KEYS": KEYS,
        "LOGIN_COOKIE_SIGNING_KEY": "signing-key",
        "SESSIONS_TABLE_NAME": "Sessions",
        "PASSWORD_MIN_LENGTH": "8",
        "PASSWORD_REQUIRE_UPPERCASE": "true",
        "PASSWORD_REQUIRE_LOWERCASE": "true",
        "PASSWORD_REQUIRE_DIGITS": "true",
        "PASSWORD_REQUIRE_SYMBOLS": "false",
    }

    def _config(self, monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
        from src.config import Config

        for name in (
            *self._COMPLETE,
            "COGNITO_DOMAIN",
            "ECS_CONTAINER_METADATA_URI_V4",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return Config()

    def test_complete_settings_start_without_cognito_domain(self, monkeypatch):
        AuthManager.check_startup(self._config(monkeypatch, **self._COMPLETE))

    @pytest.mark.parametrize("missing", sorted(set(_COMPLETE) - {"AUTH_MODE"}))
    def test_each_setting_is_required(self, monkeypatch, missing):
        env = {k: v for k, v in self._COMPLETE.items() if k != missing}
        with raises_code(AuthConfigError, ErrorCode.AUTH_CONFIG_INVALID):
            AuthManager.check_startup(self._config(monkeypatch, **env))

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("PASSWORD_MIN_LENGTH", "five"),
            ("PASSWORD_MIN_LENGTH", "100"),
            ("PASSWORD_REQUIRE_DIGITS", "sometimes"),
            ("TOKEN_ENCRYPTION_KEYS", "not json"),
        ],
    )
    def test_malformed_settings_stop_startup(self, monkeypatch, name, value):
        env = {**self._COMPLETE, name: value}
        with raises_code(AuthConfigError, ErrorCode.AUTH_CONFIG_INVALID):
            AuthManager.check_startup(self._config(monkeypatch, **env))


# =============================================================================
# ログイン（BR1.2〜BR1.6、BR3.1）
# =============================================================================


class TestLogin:
    def test_tokens_create_an_admin_api_session_and_return_next(
        self, manager, cognito, store
    ):
        result = _login(manager, previous_session_id="old-session")

        assert isinstance(result, LoginResult)
        assert result.next == "/persona"
        assert result.principal.sub == SUB
        assert result.principal.roles == frozenset({Role.MEMBER})
        assert result.principal.group_ids == frozenset({"sales"})
        cognito.initiate_auth.assert_called_once_with(EMAIL, SENTINEL_PASSWORD)
        session = store.put.call_args.args[0]
        assert session.auth_method is AuthMethod.ADMIN_API
        assert (
            session.refresh_token_enc and "refresh-1" not in session.refresh_token_enc
        )
        assert store.delete.call_args_list[0].args[0] != "old-session"  # ハッシュで消す

    def test_next_is_validated_again(self, manager):
        result = _login(manager, next_url="https://evil.example.com/")
        assert result.next == "/"

    def test_email_is_trimmed(self, manager, cognito):
        _login(manager, email=f"  {EMAIL} ")
        cognito.initiate_auth.assert_called_once_with(EMAIL, SENTINEL_PASSWORD)

    @pytest.mark.parametrize(
        ("email", "password"),
        [
            ("", SENTINEL_PASSWORD),
            ("   ", SENTINEL_PASSWORD),
            (EMAIL, ""),
            ("a" * 243 + "@example.com", SENTINEL_PASSWORD),
            (EMAIL, "x" * 257),
        ],
    )
    def test_bad_input_never_calls_cognito(
        self, manager, cognito, capsys, email, password
    ):
        with raises_code(AdminLoginError, ErrorCode.LOGIN_FAILED):
            _login(manager, email=email, password=password)
        cognito.initiate_auth.assert_not_called()
        events = LogCapture(capsys).events("auth.login_failed")
        assert [e["reason"] for e in events] == ["invalid_input"]

    @pytest.mark.parametrize(
        ("code", "unexpected", "reason"),
        [
            (ErrorCode.LOGIN_FAILED, False, "bad_credentials"),
            (ErrorCode.LOGIN_RATE_LIMITED, False, "rate_limited"),
            (ErrorCode.LOGIN_UNAVAILABLE, False, "unavailable"),
            (ErrorCode.LOGIN_UNAVAILABLE, True, "unexpected_error"),
        ],
    )
    def test_cognito_failures_keep_their_code_and_create_no_session(
        self, manager, cognito, store, capsys, code, unexpected, reason
    ):
        cognito.initiate_auth.side_effect = CognitoAuthError(
            "x",
            code=code,
            unexpected=unexpected,
            error_type="InvalidParameterException",
        )
        with raises_code(AdminLoginError, code):
            _login(manager)
        store.put.assert_not_called()
        store.put_challenge.assert_not_called()
        events = LogCapture(capsys).events("auth.login_failed")
        assert len(events) == 1
        assert events[0]["reason"] == reason
        assert events[0]["auth_method"] == "admin_api"
        if unexpected:
            assert events[0]["error_type"] == "InvalidParameterException"
        else:
            assert "error_type" not in events[0]

    def test_new_password_required_creates_a_challenge(
        self, manager, cognito, store, cipher
    ):
        cognito.initiate_auth.return_value = _new_password_challenge()

        start = _login(manager)

        assert isinstance(start, NewPasswordStart)
        assert len(start.challenge_id) >= 43  # 256 bit
        challenge = store.get_challenge(challenge_key(start.challenge_id))
        assert challenge is not None
        assert challenge.username == SUB
        assert challenge.next == "/persona"
        assert "cognito-session" not in challenge.cognito_session_enc
        assert cipher.decrypt(challenge.cognito_session_enc) == (
            "cognito-session-0123456789"
        )
        store.put.assert_not_called()

    @pytest.mark.parametrize("required", ['["name"]', "not json", '{"a": 1}'])
    def test_required_attributes_are_unsupported(
        self, manager, cognito, store, capsys, required
    ):
        cognito.initiate_auth.return_value = _new_password_challenge(required)
        with raises_code(AdminLoginError, ErrorCode.LOGIN_FAILED):
            _login(manager)
        store.put_challenge.assert_not_called()
        events = LogCapture(capsys).events("auth.login_failed")
        assert [e["reason"] for e in events] == ["unsupported_challenge"]

    @pytest.mark.parametrize("name", ["SOFTWARE_TOKEN_MFA", "SMS_MFA", "MFA_SETUP"])
    def test_other_challenges_are_unsupported(self, manager, cognito, capsys, name):
        cognito.initiate_auth.return_value = CognitoChallenge(
            name=name, session="s" * 30, parameters={}
        )
        with raises_code(AdminLoginError, ErrorCode.LOGIN_FAILED):
            _login(manager)
        events = LogCapture(capsys).events("auth.login_failed")
        assert [e["reason"] for e in events] == ["unsupported_challenge"]

    def test_challenge_store_unreachable_is_503(self, manager, cognito, store):
        cognito.initiate_auth.return_value = _new_password_challenge()
        store.put_challenge.side_effect = SessionStoreError("down")
        with pytest.raises(LookupUnavailableError):
            _login(manager)

    def test_session_store_unreachable_is_503(self, manager, store):
        store.put.side_effect = SessionStoreError("down")
        with pytest.raises(LookupUnavailableError):
            _login(manager)

    def test_success_is_logged(self, manager, capsys):
        _login(manager)
        events = LogCapture(capsys).events("auth.login_succeeded")
        assert len(events) == 1
        assert events[0]["auth_method"] == "admin_api"
        assert "first_login" not in events[0]

    def test_failure_reasons_are_the_seven_of_the_spec(self):
        assert LOGIN_FAILURE_REASONS == frozenset(
            {
                "bad_credentials",
                "invalid_input",
                "rate_limited",
                "unavailable",
                "unexpected_error",
                "unsupported_challenge",
                "token_invalid",
            }
        )


class TestIdToken:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"iss": "https://cognito-idp.ap-northeast-1.amazonaws.com/other"},
            {"aud": "another-client"},
            {"token_use": "access"},
            {"exp": int((T0 - timedelta(seconds=1)).timestamp())},
            {"exp": int(T0.timestamp())},
            {"cognito:username": "someone-else"},
            {"sub": None},
            {"iss": None},
        ],
    )
    def test_any_wrong_claim_refuses_the_login(
        self, manager, cognito, store, capsys, overrides
    ):
        cognito.initiate_auth.return_value = _tokens(_id_token(**overrides))
        with raises_code(AdminLoginError, ErrorCode.LOGIN_FAILED):
            _login(manager)
        store.put.assert_not_called()
        events = LogCapture(capsys).events("auth.login_failed")
        assert [e["reason"] for e in events] == ["token_invalid"]

    @pytest.mark.parametrize("token", ["", "abc", "a.b", "a.!!!.c", "a.bm90IGpzb24.c"])
    def test_unreadable_token_refuses_the_login(self, manager, cognito, token):
        cognito.initiate_auth.return_value = _tokens(token or "x")
        with raises_code(AdminLoginError, ErrorCode.LOGIN_FAILED):
            _login(manager)

    def test_signature_is_not_checked(self, manager, cognito):
        """BR3.1 / Q2 A: TLS と IAM 署名の応答なので署名は確かめない（JWKS を取らない）。"""
        assert isinstance(_login(manager), LoginResult)


# =============================================================================
# 初回パスワード変更（BR2.4〜BR2.7）
# =============================================================================


class TestNewPassword:
    def test_valid_challenge_is_accepted_for_the_page(self, manager, cognito):
        challenge_id = _start_challenge(manager, cognito)
        assert manager.check_challenge(challenge_id) == POLICY

    @pytest.mark.parametrize("challenge_id", [None, "", "unknown-id"])
    def test_missing_challenge_is_expired(self, manager, challenge_id):
        with raises_code(AdminLoginError, ErrorCode.LOGIN_CHALLENGE_EXPIRED):
            manager.check_challenge(challenge_id)
        with raises_code(AdminLoginError, ErrorCode.LOGIN_CHALLENGE_EXPIRED):
            _complete(manager, challenge_id)

    def test_after_three_minutes_it_is_expired_and_removed(
        self, manager, cognito, store, clock
    ):
        challenge_id = _start_challenge(manager, cognito)
        clock.advance(minutes=3)
        manager.check_challenge(challenge_id)  # ちょうど 3 分は有効
        clock.advance(seconds=1)
        with raises_code(AdminLoginError, ErrorCode.LOGIN_CHALLENGE_EXPIRED):
            _complete(manager, challenge_id)
        assert store.get_challenge(challenge_key(challenge_id)) is None
        cognito.respond_new_password.assert_not_called()

    def test_undecryptable_challenge_is_expired(self, store, cognito, clock):
        first = _manager(store, cognito, TokenCipher(KEYS), clock)
        challenge_id = _start_challenge(first, cognito)
        other_keys = json.dumps({"current": "v9", "k_v9": "other-key-material-012345"})
        second = _manager(store, cognito, TokenCipher(other_keys), clock)
        with raises_code(AdminLoginError, ErrorCode.LOGIN_CHALLENGE_EXPIRED):
            second.check_challenge(challenge_id)
        assert store.get_challenge(challenge_key(challenge_id)) is None

    def test_policy_violation_never_calls_cognito_and_keeps_the_challenge(
        self, manager, cognito, store
    ):
        challenge_id = _start_challenge(manager, cognito)
        with raises_code(
            AdminLoginError,
            ErrorCode.PASSWORD_POLICY_NOT_MET,
            rules=[PasswordRule.UPPERCASE.value, PasswordRule.DIGITS.value],
        ):
            _complete(
                manager,
                challenge_id,
                new_password="lowercase",
                confirmation="lowercase",
            )
        cognito.respond_new_password.assert_not_called()
        assert store.get_challenge(challenge_key(challenge_id)) is not None

    def test_confirmation_mismatch_never_calls_cognito(self, manager, cognito, store):
        challenge_id = _start_challenge(manager, cognito)
        with raises_code(AdminLoginError, ErrorCode.PASSWORD_CONFIRMATION_MISMATCH):
            _complete(manager, challenge_id, confirmation=NEW_PASSWORD + "x")
        cognito.respond_new_password.assert_not_called()
        assert store.get_challenge(challenge_key(challenge_id)) is not None

    def test_success_creates_a_session_and_removes_the_challenge(
        self, manager, cognito, store, capsys
    ):
        challenge_id = _start_challenge(manager, cognito)

        result = _complete(manager, challenge_id)

        assert isinstance(result, LoginResult)
        assert result.next == "/persona"
        cognito.respond_new_password.assert_called_once_with(
            SUB, NEW_PASSWORD, "cognito-session-0123456789"
        )
        assert store.get_challenge(challenge_key(challenge_id)) is None
        events = LogCapture(capsys).events("auth.login_succeeded")
        assert events[-1]["first_login"] is True
        assert events[-1]["auth_method"] == "admin_api"

    def test_a_second_submission_after_success_is_expired(self, manager, cognito):
        challenge_id = _start_challenge(manager, cognito)
        _complete(manager, challenge_id)
        with raises_code(AdminLoginError, ErrorCode.LOGIN_CHALLENGE_EXPIRED):
            _complete(manager, challenge_id)
        assert cognito.respond_new_password.call_count == 1

    def test_lost_race_is_expired_without_calling_cognito(
        self, manager, cognito, store
    ):
        challenge_id = _start_challenge(manager, cognito)
        store.claim_challenge.side_effect = lambda key: None  # 別の送信が先に取り出した
        with raises_code(AdminLoginError, ErrorCode.LOGIN_CHALLENGE_EXPIRED):
            _complete(manager, challenge_id)
        cognito.respond_new_password.assert_not_called()

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.PASSWORD_POLICY_NOT_MET,
            ErrorCode.LOGIN_RATE_LIMITED,
            ErrorCode.LOGIN_UNAVAILABLE,
        ],
    )
    def test_retryable_answers_write_the_challenge_back(
        self, manager, cognito, store, clock, code
    ):
        challenge_id = _start_challenge(manager, cognito)
        original = store.get_challenge(challenge_key(challenge_id))
        clock.advance(minutes=1)
        cognito.respond_new_password.side_effect = CognitoAuthError("x", code=code)

        with raises_code(AdminLoginError, code):
            _complete(manager, challenge_id)

        restored = store.get_challenge(challenge_key(challenge_id))
        assert restored == original  # 元の created_at のまま
        store.put.assert_not_called()

    @pytest.mark.parametrize(
        "code", [ErrorCode.LOGIN_CHALLENGE_EXPIRED, ErrorCode.LOGIN_FAILED]
    )
    def test_cognito_refusing_the_session_leaves_it_removed(
        self, manager, cognito, store, capsys, code
    ):
        challenge_id = _start_challenge(manager, cognito)
        capsys.readouterr()
        cognito.respond_new_password.side_effect = CognitoAuthError("x", code=code)
        with raises_code(AdminLoginError, ErrorCode.LOGIN_CHALLENGE_EXPIRED):
            _complete(manager, challenge_id)
        assert store.get_challenge(challenge_key(challenge_id)) is None
        # 時間切れはログインの失敗として数えない（NFR8.28）
        assert LogCapture(capsys).events("auth.login_failed") == []

    def test_another_challenge_after_the_new_password_restarts_the_login(
        self, manager, cognito, store, capsys
    ):
        challenge_id = _start_challenge(manager, cognito)
        cognito.respond_new_password.return_value = CognitoChallenge(
            name="SOFTWARE_TOKEN_MFA", session="s" * 30, parameters={}
        )
        with raises_code(AdminLoginError, ErrorCode.LOGIN_CHALLENGE_EXPIRED):
            _complete(manager, challenge_id)
        store.put.assert_not_called()
        reasons = [e["reason"] for e in LogCapture(capsys).events("auth.login_failed")]
        assert reasons == ["unsupported_challenge"]

    def test_invalid_id_token_after_the_new_password_restarts_the_login(
        self, manager, cognito, store
    ):
        challenge_id = _start_challenge(manager, cognito)
        cognito.respond_new_password.return_value = _tokens(_id_token(aud="x"))
        with raises_code(AdminLoginError, ErrorCode.LOGIN_CHALLENGE_EXPIRED):
            _complete(manager, challenge_id)
        store.put.assert_not_called()

    def test_failed_write_back_still_reports_the_cognito_answer(
        self, manager, cognito, store
    ):
        challenge_id = _start_challenge(manager, cognito)
        cognito.respond_new_password.side_effect = CognitoAuthError("x")
        store.restore_challenge.side_effect = SessionStoreError("down")
        with raises_code(AdminLoginError, ErrorCode.LOGIN_UNAVAILABLE):
            _complete(manager, challenge_id)

    def test_another_task_can_finish_the_change(self, store, cognito, cipher, clock):
        """NFR9.42: 台をまたいでも同じ challenge で続けられる。"""
        challenge_id = _start_challenge(
            _manager(store, cognito, cipher, clock), cognito
        )
        other = _manager(store, cognito, cipher, clock)
        assert isinstance(_complete(other, challenge_id), LoginResult)

    def test_store_unreachable_is_503(self, manager, cognito, store):
        challenge_id = _start_challenge(manager, cognito)
        store.get_challenge.side_effect = SessionStoreError("down")
        with pytest.raises(LookupUnavailableError):
            _complete(manager, challenge_id)

    def test_only_admin_api_can_use_it(self, store, cipher, clock):
        oidc_manager = AuthManager(
            settings=_settings("local"), session_store=store, clock=clock
        )
        with pytest.raises(AuthConfigError):
            oidc_manager.login_admin_api(
                email=EMAIL, password="x", next_url="/", previous_session_id=None
            )


# =============================================================================
# 更新・ログアウト（BR3.2、BR3.3、NFR5.7、NFR9.40、NFR9.46）
# =============================================================================


def _session_id(manager: AuthManager) -> str:
    return _login(manager).session_id


class TestRefreshAndLogout:
    def test_no_cognito_call_before_sixty_seconds_to_expiry(
        self, manager, cognito, clock
    ):
        session_id = _session_id(manager)
        clock.advance(minutes=58, seconds=59)
        manager.check_request(session_id, passive=True)
        cognito.refresh.assert_not_called()

    def test_refresh_uses_the_user_sub_and_keeps_the_refresh_token(
        self, manager, cognito, store, clock, cipher
    ):
        session_id = _session_id(manager)
        cognito.refresh.return_value = _tokens(
            _id_token(
                **{
                    "cognito:groups": ["role:admin"],
                    "exp": int((T0 + timedelta(hours=2)).timestamp()),
                }
            ),
            refresh="refresh-1",
        )
        clock.advance(minutes=59)

        principal = manager.check_request(session_id, passive=True)

        cognito.refresh.assert_called_once_with(SUB, "refresh-1")
        assert principal.roles == frozenset({Role.ADMIN})
        stored = store.get(store.put.call_args.args[0].session_id_hash)
        assert cipher.decrypt(stored.refresh_token_enc) == "refresh-1"

    def test_permanent_refresh_failure_ends_the_session(self, manager, cognito, clock):
        session_id = _session_id(manager)
        cognito.refresh.side_effect = CognitoAuthError("x", code=ErrorCode.LOGIN_FAILED)
        clock.advance(minutes=59)
        with raises_code(AuthRequiredError, ErrorCode.SESSION_EXPIRED):
            manager.check_request(session_id, passive=True)

    @pytest.mark.parametrize(
        "code", [ErrorCode.LOGIN_UNAVAILABLE, ErrorCode.LOGIN_RATE_LIMITED]
    )
    def test_transient_refresh_failure_has_fifteen_minutes_of_grace(
        self, manager, cognito, clock, code
    ):
        session_id = _session_id(manager)
        cognito.refresh.side_effect = CognitoAuthError("x", code=code)
        clock.advance(minutes=59)
        assert manager.check_request(session_id, passive=True).sub == SUB
        clock.advance(minutes=15, seconds=1)
        with pytest.raises(LookupUnavailableError):
            manager.check_request(session_id, passive=True)

    def test_invalid_id_token_on_refresh_ends_the_session(
        self, manager, cognito, clock
    ):
        session_id = _session_id(manager)
        cognito.refresh.return_value = _tokens(_id_token(aud="other"))
        clock.advance(minutes=59)
        with raises_code(AuthRequiredError, ErrorCode.SESSION_EXPIRED):
            manager.check_request(session_id, passive=True)

    def test_logout_revokes_and_returns_to_the_app_login(self, manager, cognito, store):
        session_id = _session_id(manager)
        assert manager.logout(session_id) == "/login?reason=signed_out"
        cognito.revoke.assert_called_once_with("refresh-1")
        with pytest.raises(AuthRequiredError):
            manager.check_request(session_id, passive=True)

    def test_revoke_failure_is_logged_and_logout_continues(
        self, manager, cognito, capsys
    ):
        session_id = _session_id(manager)
        cognito.revoke.side_effect = CognitoAuthError("x")
        assert manager.logout(session_id) == "/login?reason=signed_out"
        reasons = [e.get("reason") for e in LogCapture(capsys).events("auth.logout")]
        assert "revoke_failed" in reasons

    def test_session_of_another_method_is_refused(self, manager, store, cipher, clock):
        local = AuthManager(
            settings=_settings("local"), session_store=store, clock=clock
        )
        session_id = local.start_local_session(
            display_name=None, email=None, roles=["member"], group_ids=[]
        )
        with raises_code(AuthRequiredError, ErrorCode.AUTH_REQUIRED):
            manager.check_request(session_id, passive=True)

    def test_circuit_open_is_logged(self, store, cognito, cipher, clock, capsys):
        _manager(store, cognito, cipher, clock)
        listener = cognito.add_circuit_open_listener.call_args.args[0]
        listener()
        events = LogCapture(capsys).events("auth.circuit_opened")
        assert len(events) == 1


# =============================================================================
# パスワードを出さない（NFR3.15、BR4.5、AC1.2.5）
# =============================================================================


class TestPasswordNeverLeaks:
    _FAILURES = [
        CognitoAuthError("x", code=ErrorCode.LOGIN_FAILED),
        CognitoAuthError("x", code=ErrorCode.LOGIN_RATE_LIMITED),
        CognitoAuthError("x", code=ErrorCode.LOGIN_UNAVAILABLE),
        CognitoAuthError("x", unexpected=True, error_type="ForbiddenException"),
    ]

    @pytest.mark.parametrize("failure", _FAILURES)
    def test_login_failures(self, manager, cognito, store, capsys, caplog, failure):
        cognito.initiate_auth.side_effect = failure
        with pytest.raises(AdminLoginError) as info:
            _login(manager)
        out = capsys.readouterr().out
        assert_not_leaked(
            SENTINEL_PASSWORD, info.value, out, caplog, _stored_items(store)
        )
        assert EMAIL not in out  # ログイン失敗のログはメールアドレスも書かない

    def test_success_and_challenge(self, manager, cognito, store, capsys, caplog):
        _login(manager)
        challenge_id = _start_challenge(manager, cognito)
        _complete(manager, challenge_id)
        out = capsys.readouterr().out
        stored = [item.to_dict() for item in _stored_items(store)]
        for secret in (SENTINEL_PASSWORD, NEW_PASSWORD):
            assert_not_leaked(secret, out, caplog, stored)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"new_password": "weak", "confirmation": "weak"},
            {"confirmation": "different"},
        ],
    )
    def test_new_password_input_errors(
        self, manager, cognito, capsys, caplog, overrides
    ):
        challenge_id = _start_challenge(manager, cognito)
        with pytest.raises(AdminLoginError) as info:
            _complete(manager, challenge_id, **overrides)
        out = capsys.readouterr().out
        for secret in (NEW_PASSWORD, "weak", "different"):
            assert_not_leaked(secret, info.value, out, caplog)

    @pytest.mark.parametrize("failure", _FAILURES)
    def test_new_password_cognito_failures(
        self, manager, cognito, store, capsys, caplog, failure
    ):
        challenge_id = _start_challenge(manager, cognito)
        cognito.respond_new_password.side_effect = failure
        with pytest.raises(AdminLoginError) as info:
            _complete(manager, challenge_id)
        out = capsys.readouterr().out
        assert_not_leaked(NEW_PASSWORD, info.value, out, caplog, _stored_items(store))
