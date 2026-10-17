"""AuthManager の単体テスト（BR1.3〜BR1.18、BR1.21）。

外部（SessionStore・OidcService）はコンストラクタで注入する。状態の振る舞いは
本物の MemorySessionStore（呼び出しの記録のため Mock(wraps=...)）で、失敗は
Mock(spec=...) で確かめる。時刻は clock を注入して進め、time.sleep は使わない。
トークン・セッション ID の値はテストの出力に出さない。
"""

import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.managers.auth_manager import (
    AuthConfigError,
    AuthManager,
    AuthSettings,
    LocalUser,
    LoginFailedError,
)
from src.managers.shared.token_cipher import TokenCipher
from src.models.authz import Role
from src.models.errors import (
    AuthRequiredError,
    ErrorCode,
    LookupUnavailableError,
)
from src.models.login_session import AuthMethod, LoginSession, SessionStore
from src.services.memory_session_store import MemorySessionStore
from src.services.cognito.oidc_service import (
    AuthorizationRequest,
    OidcError,
    OidcService,
    TokenSet,
)
from src.services.session_store_service import SessionStoreError
from tests.error_helpers import raises_code

T0 = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)
KEYS = json.dumps({"current": "v1", "k_v1": "unit-test-key-material-0123456789"})
BASE_URL = "https://app.example.com"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


def _claims(groups: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "sub": "user-1",
        "name": "山田 太郎",
        "email": "yamada@example.com",
        "cognito:groups": (
            ["role:member", "group:sales"] if groups is None else groups
        ),
    }
    claims.update(extra)
    return claims


def _tokens(groups: list[str] | None = None, access: str = "access-1") -> TokenSet:
    return TokenSet(
        access_token=access,
        refresh_token="refresh-1",
        expires_in=3600,
        claims=_claims(groups),
    )


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def _hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store() -> Mock:
    return Mock(wraps=MemorySessionStore())


@pytest.fixture
def oidc() -> Mock:
    mock = Mock(spec=OidcService)
    mock.create_authorization_request.return_value = AuthorizationRequest(
        url="https://cognito.example.com/oauth2/authorize?x=1",
        state="state-1",
        nonce="nonce-1",
        code_verifier="verifier-1",
    )
    mock.exchange_code.return_value = _tokens()
    mock.refresh.return_value = _tokens(access="access-2")
    mock.logout_url.side_effect = (
        lambda uri: f"https://cognito.example.com/logout?u={uri}"
    )
    return mock


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(KEYS)


def _settings(mode: str = "oidc") -> AuthSettings:
    return AuthSettings(
        mode=mode,
        app_base_url=BASE_URL,
        local_user=LocalUser(
            display_name="開発ユーザー",
            email="dev@example.com",
            roles=frozenset({Role.MEMBER}),
            group_ids=frozenset(),
        ),
    )


@pytest.fixture
def manager(store, oidc, cipher, clock) -> AuthManager:
    return AuthManager(
        settings=_settings("oidc"),
        session_store=store,
        oidc_service=oidc,
        cipher=cipher,
        clock=clock,
    )


@pytest.fixture
def local_manager(store, clock) -> AuthManager:
    return AuthManager(settings=_settings("local"), session_store=store, clock=clock)


def _transaction(manager: AuthManager, next_url: str = "/persona") -> dict[str, Any]:
    return manager.begin_login(next_url).transaction


def _login(manager: AuthManager, previous: str | None = None) -> str:
    result = manager.complete_login(
        _transaction(manager),
        state="state-1",
        code="code-1",
        error=None,
        previous_session_id=previous,
    )
    return result.session_id


# =============================================================================
# Step 13: ログイン
# =============================================================================


class TestNextUrl:
    def test_relative_app_path_is_kept(self):
        assert AuthManager.sanitize_next("/persona/management?tab=1") == (
            "/persona/management?tab=1"
        )

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "persona",
            "//evil.example.com",
            "https://evil.example.com/x",
            "javascript:alert(1)",
            "/\\evil.example.com",
            "/ok\r\nSet-Cookie: x=1",
        ],
    )
    def test_anything_else_becomes_root(self, value):
        assert AuthManager.sanitize_next(value) == "/"

    @given(st.text())
    def test_result_never_leaves_the_app(self, value):
        result = AuthManager.sanitize_next(value)
        parts = urlsplit(result)
        assert result.startswith("/")
        assert not result.startswith("//")
        assert parts.scheme == "" and parts.netloc == ""
        assert "\\" not in result


class TestBeginLogin:
    def test_returns_cognito_url_and_transaction(self, manager, clock):
        start = manager.begin_login("//evil.example.com")
        assert start.authorization_url.startswith("https://cognito.example.com/")
        tx = start.transaction
        assert tx["state"] == "state-1"
        assert tx["nonce"] == "nonce-1"
        assert tx["code_verifier"] == "verifier-1"
        assert tx["next"] == "/"
        assert tx["created_at"] == int(clock.now.timestamp())


class TestCompleteLogin:
    def test_success_creates_a_new_session_storing_only_the_hash(
        self, manager, store, oidc, capsys
    ):
        result = manager.complete_login(
            _transaction(manager, "/persona"),
            state="state-1",
            code="code-1",
            error=None,
            previous_session_id=None,
        )

        assert len(result.session_id) >= 43  # 256 bit の token_urlsafe
        assert result.next == "/persona"
        assert store.get(result.session_id) is None
        stored = store.get(_hash(result.session_id))
        assert stored.auth_method is AuthMethod.OIDC
        assert stored.access_token_enc != "access-1"
        assert stored.refresh_token_enc != "refresh-1"
        assert stored.key_version == "v1"
        assert stored.access_expires_at == T0 + timedelta(seconds=3600)
        oidc.exchange_code.assert_called_once_with(
            "code-1", "verifier-1", nonce="nonce-1"
        )
        events = _events(capsys)
        assert [e["event"] for e in events] == ["auth.login_succeeded"]
        assert events[0]["sub"] == "user-1"
        raw = json.dumps(events)
        assert result.session_id not in raw and "code-1" not in raw

    def test_previous_session_is_removed(self, manager, store):
        first = _login(manager)
        second = _login(manager, previous=first)
        assert first != second
        assert store.get(_hash(first)) is None
        assert store.get(_hash(second)) is not None

    @pytest.mark.parametrize(
        ("case", "reason"),
        [
            ("no_transaction", "no_transaction"),
            ("state_mismatch", "state_mismatch"),
            ("expired", "transaction_expired"),
            ("cancelled", "cognito_error"),
            ("missing_code", "missing_code"),
            ("exchange_failed", "exchange_failed"),
        ],
    )
    def test_failures_create_no_session(
        self, manager, store, oidc, clock, capsys, case, reason
    ):
        tx: dict[str, Any] | None = _transaction(manager)
        state, code, error = "state-1", "code-1", None
        if case == "no_transaction":
            tx = None
        elif case == "state_mismatch":
            state = "forged"
        elif case == "expired":
            clock.advance(minutes=10, seconds=1)
        elif case == "cancelled":
            code, error = None, "access_denied"
        elif case == "missing_code":
            code = None
        else:
            oidc.exchange_code.side_effect = OidcError(
                "boom", code=ErrorCode.OIDC_TOKEN_INVALID
            )
        capsys.readouterr()

        with raises_code(LoginFailedError, ErrorCode.AUTH_REQUIRED):
            manager.complete_login(
                tx, state=state, code=code, error=error, previous_session_id=None
            )

        store.put.assert_not_called()
        events = _events(capsys)
        assert [e["event"] for e in events] == ["auth.login_failed"]
        assert events[0]["reason"] == reason

    def test_transaction_is_still_valid_at_ten_minutes(self, manager, clock):
        tx = _transaction(manager)
        clock.advance(minutes=10)
        result = manager.complete_login(
            tx, state="state-1", code="code-1", error=None, previous_session_id=None
        )
        assert result.session_id

    def test_store_unreachable_on_login_is_lookup_unavailable(
        self, oidc, cipher, clock
    ):
        broken = Mock(spec=SessionStore)
        broken.put.side_effect = SessionStoreError("down")
        manager = AuthManager(
            settings=_settings("oidc"),
            session_store=broken,
            oidc_service=oidc,
            cipher=cipher,
            clock=clock,
        )
        with raises_code(LookupUnavailableError, ErrorCode.LOOKUP_UNAVAILABLE):
            _login(manager)


class TestGroups:
    def test_roles_and_departments_come_from_prefixed_groups(
        self, manager, store, oidc, capsys
    ):
        oidc.exchange_code.return_value = _tokens(
            ["role:admin", "role:owner", "group:sales", "group:hr", "Everyone"]
        )
        session_id = _login(manager)

        principal = manager.check_request(session_id, passive=False)
        assert principal.roles == frozenset({Role.ADMIN})
        assert principal.group_ids == frozenset({"sales", "hr"})
        assert principal.display_name == "山田 太郎"
        assert principal.email == "yamada@example.com"
        events = [e for e in _events(capsys) if e["event"] == "auth.unknown_role_group"]
        assert len(events) == 1
        assert events[0]["group"] == "role:owner"

    def test_no_groups_means_no_role_and_no_department(self, manager, oidc):
        oidc.exchange_code.return_value = _tokens([])
        principal = manager.check_request(_login(manager), passive=False)
        assert principal.roles == frozenset()
        assert principal.group_ids == frozenset()


# =============================================================================
# Step 14: 要求ごとの照合
# =============================================================================


def _stored(store: Mock, session_id: str) -> LoginSession:
    session = store.get(_hash(session_id))
    assert session is not None
    return session


class TestCheckRequest:
    def test_valid_session_returns_the_principal_without_writing(
        self, manager, store, clock
    ):
        session_id = _login(manager)
        store.reset_mock()
        clock.advance(minutes=4, seconds=59)

        principal = manager.check_request(session_id, passive=False)

        assert principal.sub == "user-1"
        assert principal.roles == frozenset({Role.MEMBER})
        store.touch.assert_not_called()
        store.update_versioned.assert_not_called()

    def test_missing_cookie_or_unknown_session_requires_login(self, manager):
        with raises_code(AuthRequiredError, ErrorCode.AUTH_REQUIRED):
            manager.check_request(None, passive=False)
        with raises_code(AuthRequiredError, ErrorCode.AUTH_REQUIRED):
            manager.check_request("unknown-session-id", passive=False)

    def test_idle_for_exactly_eight_hours_is_still_valid(self, manager, clock, oidc):
        session_id = _login(manager)
        oidc.refresh.return_value = _tokens()
        clock.advance(hours=8)
        assert manager.check_request(session_id, passive=False).sub == "user-1"

    def test_idle_past_eight_hours_expires_and_deletes(
        self, manager, store, clock, capsys
    ):
        session_id = _login(manager)
        clock.advance(hours=8, seconds=1)
        capsys.readouterr()

        with raises_code(AuthRequiredError, ErrorCode.SESSION_EXPIRED):
            manager.check_request(session_id, passive=False)

        assert store.get(_hash(session_id)) is None
        events = _events(capsys)
        assert events[-1]["event"] == "auth.session_expired"
        assert events[-1]["reason"] == "idle_timeout"

    def test_seven_days_after_login_expires_even_when_active(
        self, manager, store, clock, oidc, capsys
    ):
        session_id = _login(manager)
        oidc.refresh.return_value = _tokens()
        for _ in range(7 * 4):  # 6 時間ごとに操作し続ける
            clock.advance(hours=6)
            manager.check_request(session_id, passive=False)
        clock.advance(seconds=1)
        capsys.readouterr()

        with raises_code(AuthRequiredError, ErrorCode.SESSION_EXPIRED):
            manager.check_request(session_id, passive=False)
        assert store.get(_hash(session_id)) is None
        assert _events(capsys)[-1]["reason"] == "absolute_timeout"

    def test_session_from_another_auth_method_is_deleted(
        self, local_manager, store, clock
    ):
        foreign = LoginSession.create_new(
            session_id_hash=_hash("foreign-session"),
            user_sub="user-1",
            auth_method=AuthMethod.OIDC,
            now=clock.now,
        )
        store.put(foreign)

        with raises_code(AuthRequiredError, ErrorCode.AUTH_REQUIRED):
            local_manager.check_request("foreign-session", passive=False)
        assert store.get(_hash("foreign-session")) is None

    def test_activity_is_written_once_five_minutes_have_passed(
        self, manager, store, clock
    ):
        session_id = _login(manager)
        store.reset_mock()
        clock.advance(minutes=5)

        manager.check_request(session_id, passive=False)

        store.touch.assert_called_once()
        stored = _stored(store, session_id)
        assert stored.last_accessed_at == clock.now
        assert stored.version == 1

    def test_passive_requests_do_not_count_as_activity(self, manager, store, clock):
        session_id = _login(manager)
        store.reset_mock()
        clock.advance(minutes=30)

        manager.check_request(session_id, passive=True)

        store.touch.assert_not_called()
        assert _stored(store, session_id).last_accessed_at == T0

    def test_store_unreachable_is_lookup_unavailable(self, oidc, cipher, clock):
        broken = Mock(spec=SessionStore)
        broken.get.side_effect = SessionStoreError("down")
        manager = AuthManager(
            settings=_settings("oidc"),
            session_store=broken,
            oidc_service=oidc,
            cipher=cipher,
            clock=clock,
        )
        with raises_code(LookupUnavailableError, ErrorCode.LOOKUP_UNAVAILABLE):
            manager.check_request("some-session", passive=False)


class TestTokenRefresh:
    def test_no_refresh_until_sixty_seconds_before_expiry(self, manager, oidc, clock):
        session_id = _login(manager)
        clock.advance(minutes=58, seconds=59)
        manager.check_request(session_id, passive=True)
        oidc.refresh.assert_not_called()

    def test_refresh_at_sixty_seconds_rereads_roles_and_bumps_version(
        self, manager, store, oidc, clock, cipher
    ):
        session_id = _login(manager)
        oidc.refresh.return_value = _tokens(["role:admin", "group:hr"], "access-2")
        clock.advance(minutes=59)

        principal = manager.check_request(session_id, passive=True)

        oidc.refresh.assert_called_once_with("refresh-1")
        assert principal.roles == frozenset({Role.ADMIN})
        assert principal.group_ids == frozenset({"hr"})
        stored = _stored(store, session_id)
        assert stored.version == 2
        assert cipher.decrypt(stored.access_token_enc) == "access-2"
        assert stored.access_expires_at == clock.now + timedelta(seconds=3600)
        assert stored.refresh_failing_since is None

    def test_permanent_failure_deletes_the_session(
        self, manager, store, oidc, clock, capsys
    ):
        session_id = _login(manager)
        oidc.refresh.side_effect = OidcError(
            "revoked", code=ErrorCode.OIDC_GRANT_INVALID
        )
        clock.advance(minutes=59)
        capsys.readouterr()

        with raises_code(AuthRequiredError, ErrorCode.SESSION_EXPIRED):
            manager.check_request(session_id, passive=False)

        assert store.get(_hash(session_id)) is None
        store.delete.assert_called_with(_hash(session_id), expected_version=1)
        event = _events(capsys)[-1]
        assert event["event"] == "auth.refresh_failed"
        assert event["reason"] == "permanent"

    def test_permanent_failure_after_another_task_refreshed_keeps_going(
        self, manager, store, oidc, clock, cipher
    ):
        session_id = _login(manager)
        clock.advance(minutes=59)
        original = _stored(store, session_id)
        newer = original.update(
            roles=frozenset({Role.ADMIN}),
            access_token_enc=cipher.encrypt("access-9"),
            access_expires_at=clock.now + timedelta(hours=1),
        )

        def other_task_won(refresh_token: str) -> TokenSet:
            store.update_versioned(newer, expected_version=1)
            raise OidcError("already rotated", code=ErrorCode.OIDC_GRANT_INVALID)

        oidc.refresh.side_effect = other_task_won

        principal = manager.check_request(session_id, passive=False)

        assert principal.roles == frozenset({Role.ADMIN})
        assert _stored(store, session_id).version == 2

    def test_transient_failure_keeps_the_last_roles(
        self, manager, store, oidc, clock, capsys
    ):
        session_id = _login(manager)
        oidc.refresh.side_effect = OidcError("timeout")
        clock.advance(minutes=59)
        capsys.readouterr()

        principal = manager.check_request(session_id, passive=False)

        assert principal.roles == frozenset({Role.MEMBER})
        assert _stored(store, session_id).refresh_failing_since == clock.now
        event = _events(capsys)[-1]
        assert event["event"] == "auth.refresh_failed"
        assert event["reason"] == "transient"

    def test_transient_failures_turn_into_503_after_fifteen_minutes(
        self, manager, store, oidc, clock
    ):
        session_id = _login(manager)
        oidc.refresh.side_effect = OidcError("timeout")
        clock.advance(minutes=59)
        manager.check_request(session_id, passive=False)
        first_failure = clock.now

        clock.advance(minutes=15)
        manager.check_request(session_id, passive=False)  # ちょうど 15 分は続ける
        clock.advance(seconds=1)
        with raises_code(LookupUnavailableError, ErrorCode.LOOKUP_UNAVAILABLE):
            manager.check_request(session_id, passive=False)

        stored = _stored(store, session_id)
        assert stored.refresh_failing_since == first_failure

    def test_success_after_failures_clears_the_failure_time(
        self, manager, store, oidc, clock
    ):
        session_id = _login(manager)
        oidc.refresh.side_effect = OidcError("timeout")
        clock.advance(minutes=59)
        manager.check_request(session_id, passive=False)

        oidc.refresh.side_effect = None
        oidc.refresh.return_value = _tokens(access="access-3")
        clock.advance(minutes=1)
        manager.check_request(session_id, passive=False)

        assert _stored(store, session_id).refresh_failing_since is None

    def test_losing_the_versioned_write_rereads_and_continues(
        self, manager, store, oidc, clock, cipher
    ):
        session_id = _login(manager)
        clock.advance(minutes=59)
        original = _stored(store, session_id)
        winner = original.update(
            roles=frozenset({Role.ADMIN}),
            access_token_enc=cipher.encrypt("access-w"),
            access_expires_at=clock.now + timedelta(hours=1),
        )

        def refresh_while_other_task_writes(refresh_token: str) -> TokenSet:
            store.update_versioned(winner, expected_version=1)
            return _tokens(access="access-lost")

        oidc.refresh.side_effect = refresh_while_other_task_writes

        principal = manager.check_request(session_id, passive=False)

        assert principal.roles == frozenset({Role.ADMIN})
        stored = _stored(store, session_id)
        assert cipher.decrypt(stored.access_token_enc) == "access-w"

    def test_concurrent_refreshes_in_one_task_call_cognito_once(
        self, manager, oidc, clock
    ):
        session_id = _login(manager)
        clock.advance(minutes=59)
        entered = threading.Event()
        release = threading.Event()

        def slow_refresh(refresh_token: str) -> TokenSet:
            entered.set()
            release.wait(5)
            return _tokens(access="access-2")

        oidc.refresh.side_effect = slow_refresh
        results: list[Any] = []

        def run() -> None:
            results.append(manager.check_request(session_id, passive=False))

        first = threading.Thread(target=run)
        first.start()
        assert entered.wait(5)
        second = threading.Thread(target=run)
        second.start()
        time.sleep(0.1)  # 2 本目が同じセッションの更新を待つところまで進める
        release.set()
        first.join(5)
        second.join(5)

        assert len(results) == 2
        assert oidc.refresh.call_count == 1

    def test_undecryptable_tokens_delete_the_session(
        self, manager, store, clock, capsys
    ):
        session_id = _login(manager)
        broken = _stored(store, session_id).update(refresh_token_enc="v7.garbage")
        store.update_versioned(broken, expected_version=1)
        clock.advance(minutes=59)
        capsys.readouterr()

        with raises_code(AuthRequiredError, ErrorCode.AUTH_REQUIRED):
            manager.check_request(session_id, passive=False)

        assert store.get(_hash(session_id)) is None
        assert _events(capsys)[-1]["event"] == "auth.decrypt_failed"

    def test_local_sessions_never_refresh(self, local_manager, store, clock):
        session_id = local_manager.start_local_session(
            display_name="開発ユーザー",
            email=None,
            roles=frozenset({Role.MEMBER}),
            group_ids=frozenset({"sales"}),
        )
        clock.advance(hours=7)

        principal = local_manager.check_request(session_id, passive=False)

        assert principal.group_ids == frozenset({"sales"})
        stored = _stored(store, session_id)
        assert stored.auth_method is AuthMethod.LOCAL
        assert stored.access_token_enc is None
        clock.advance(hours=8, seconds=1)
        with raises_code(AuthRequiredError, ErrorCode.SESSION_EXPIRED):
            local_manager.check_request(session_id, passive=False)


# =============================================================================
# Step 15: ログアウト・起動時の検査
# =============================================================================


class TestLogout:
    def test_oidc_logout_revokes_deletes_and_returns_cognito_logout(
        self, manager, store, oidc, capsys
    ):
        session_id = _login(manager)
        capsys.readouterr()

        redirect = manager.logout(session_id)

        oidc.revoke.assert_called_once_with("refresh-1")
        assert store.get(_hash(session_id)) is None
        oidc.logout_url.assert_called_once_with(f"{BASE_URL}/login")
        assert redirect.startswith("https://cognito.example.com/logout")
        assert _events(capsys)[-1]["event"] == "auth.logout"

    def test_revoke_failure_is_logged_and_logout_continues(
        self, manager, store, oidc, capsys
    ):
        session_id = _login(manager)
        oidc.revoke.side_effect = OidcError("down")
        capsys.readouterr()

        redirect = manager.logout(session_id)

        assert store.get(_hash(session_id)) is None
        assert redirect.startswith("https://cognito.example.com/logout")
        reasons = [e.get("reason") for e in _events(capsys)]
        assert "revoke_failed" in reasons

    def test_local_logout_returns_to_the_signed_out_page(self, local_manager, store):
        session_id = local_manager.start_local_session(
            display_name="開発ユーザー",
            email=None,
            roles=frozenset(),
            group_ids=frozenset(),
        )

        assert local_manager.logout(session_id) == "/login?reason=signed_out"
        assert store.get(_hash(session_id)) is None

    def test_delete_failure_is_not_reported_as_logged_out(
        self, manager, store, oidc, capsys
    ):
        """If the session row cannot be deleted, the old cookie would still work."""
        session_id = _login(manager)
        store.delete.side_effect = SessionStoreError("down")
        capsys.readouterr()

        with raises_code(LookupUnavailableError, ErrorCode.LOOKUP_UNAVAILABLE):
            manager.logout(session_id)

        oidc.revoke.assert_called_once_with("refresh-1")
        oidc.logout_url.assert_not_called()
        assert "auth.logout" not in [e["event"] for e in _events(capsys)]

    def test_logout_without_a_session_still_redirects(self, manager, oidc):
        assert manager.logout(None).startswith("https://cognito.example.com/logout")
        oidc.revoke.assert_not_called()


class TestStartupCheck:
    def _config(self, monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
        from src.config import Config

        for name in (
            "AUTH_MODE",
            "ECS_CONTAINER_METADATA_URI_V4",
            "COGNITO_USER_POOL_ID",
            "COGNITO_CLIENT_ID",
            "COGNITO_CLIENT_SECRET",
            "COGNITO_DOMAIN",
            "TOKEN_ENCRYPTION_KEYS",
            "LOGIN_COOKIE_SIGNING_KEY",
            "SESSIONS_TABLE_NAME",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return Config()

    @pytest.mark.parametrize("mode", [None, "", "saml", "admin_api"])
    def test_unset_unknown_or_unimplemented_mode_stops_startup(self, monkeypatch, mode):
        env = {} if mode is None else {"AUTH_MODE": mode}
        with raises_code(AuthConfigError, ErrorCode.AUTH_CONFIG_INVALID):
            AuthManager.check_startup(self._config(monkeypatch, **env))

    def test_local_inside_ecs_stops_startup(self, monkeypatch):
        cfg = self._config(
            monkeypatch,
            AUTH_MODE="local",
            ECS_CONTAINER_METADATA_URI_V4="http://169.254.170.2/v4/x",
        )
        with raises_code(AuthConfigError, ErrorCode.AUTH_CONFIG_INVALID):
            AuthManager.check_startup(cfg)

    def test_local_outside_ecs_starts(self, monkeypatch):
        AuthManager.check_startup(self._config(monkeypatch, AUTH_MODE="local"))

    def test_oidc_requires_every_setting(self, monkeypatch):
        complete = {
            "AUTH_MODE": "oidc",
            "COGNITO_USER_POOL_ID": "ap-northeast-1_x",
            "COGNITO_CLIENT_ID": "c",
            "COGNITO_CLIENT_SECRET": "s",
            "COGNITO_DOMAIN": "d.example.com",
            "TOKEN_ENCRYPTION_KEYS": KEYS,
            "LOGIN_COOKIE_SIGNING_KEY": "signing-key",
            "SESSIONS_TABLE_NAME": "Sessions",
        }
        AuthManager.check_startup(self._config(monkeypatch, **complete))
        for missing in complete:
            if missing == "AUTH_MODE":
                continue
            env = {k: v for k, v in complete.items() if k != missing}
            with raises_code(AuthConfigError, ErrorCode.AUTH_CONFIG_INVALID):
                AuthManager.check_startup(self._config(monkeypatch, **env))
        broken_keys = {**complete, "TOKEN_ENCRYPTION_KEYS": "not json"}
        with raises_code(AuthConfigError, ErrorCode.AUTH_CONFIG_INVALID):
            AuthManager.check_startup(self._config(monkeypatch, **broken_keys))
