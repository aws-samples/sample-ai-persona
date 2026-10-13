"""OidcService（Cognito との通信）と回路遮断器の結合テスト。

httpx の MockTransport を注入し、Cognito の token・revoke・JWKS を模す。ID トークンは
テストで作った RSA 鍵で署名する（unit-test-instructions.md のモックの方針）。
BR1.4、BR1.11、BR1.13、NFR4.5、NFR5.4、reliability-design.md の回路遮断器。
"""

import base64
import hashlib
import json
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from src.models.errors import ErrorCode
from src.services.oidc_service import CircuitBreaker, OidcError, OidcService
from tests.error_helpers import raises_code

POOL_ID = "ap-northeast-1_TestPool"
ISSUER = f"https://cognito-idp.ap-northeast-1.amazonaws.com/{POOL_ID}"
CLIENT_ID = "test-client-id"
DOMAIN = "ai-persona-v1-dev-test.auth.ap-northeast-1.amazoncognito.com"
REDIRECT = "https://app.example.com/auth/callback"
NOW = 1_791_676_800  # 2026-10-11T00:00:00Z

_KEY = RSAKey.generate_key(2048, parameters={"kid": "test-kid"})
_OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "test-kid"})


def _id_token(key: RSAKey = _KEY, **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "sub": "user-1",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "token_use": "id",
        "exp": NOW + 3600,
        "iat": NOW,
        "nonce": "nonce-1",
        "email": "yamada@example.com",
        "name": "山田",
        "cognito:groups": ["role:member", "group:sales"],
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode({"alg": "RS256", "kid": "test-kid"}, claims, key)


def _token_response(id_token: str | None = None, **extra: Any) -> httpx.Response:
    body = {
        "access_token": "access-token-1",
        "refresh_token": "refresh-token-1",
        "id_token": id_token or _id_token(),
        "token_type": "Bearer",
        "expires_in": 3600,
        **extra,
    }
    return httpx.Response(200, json=body)


class FakeCognito:
    """MockTransport の振る舞いを差し替えられる Cognito の代役。"""

    def __init__(self) -> None:
        self.token: Callable[[httpx.Request], httpx.Response] = (
            lambda request: _token_response()
        )
        self.revoke: Callable[[httpx.Request], httpx.Response] = (
            lambda request: httpx.Response(200)
        )
        self.jwks: Callable[[httpx.Request], httpx.Response] = (
            lambda request: httpx.Response(
                200, json={"keys": [_KEY.as_dict(private=False)]}
            )
        )
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/.well-known/jwks.json"):
            return self.jwks(request)
        if path == "/oauth2/token":
            return self.token(request)
        if path == "/oauth2/revoke":
            return self.revoke(request)
        return httpx.Response(404)

    def calls_to(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def cognito() -> FakeCognito:
    return FakeCognito()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def opened() -> list[str]:
    return []


@pytest.fixture
def service(cognito, clock, opened) -> OidcService:
    breaker = CircuitBreaker(clock=clock)
    breaker.add_open_listener(lambda: opened.append("opened"))
    return OidcService(
        user_pool_id=POOL_ID,
        client_id=CLIENT_ID,
        client_secret="test-client-secret",
        domain=DOMAIN,
        redirect_uri=REDIRECT,
        transport=httpx.MockTransport(cognito),
        breaker=breaker,
        wall_clock=lambda: NOW,
    )


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


class TestAuthorizationRequest:
    def test_url_carries_state_nonce_pkce_and_callback(self, service):
        auth = service.create_authorization_request()
        url = urlparse(auth.url)
        query = parse_qs(url.query)

        assert url.netloc == DOMAIN and url.path == "/oauth2/authorize"
        assert query["response_type"] == ["code"]
        assert query["client_id"] == [CLIENT_ID]
        assert query["redirect_uri"] == [REDIRECT]
        assert query["scope"] == ["openid email profile"]
        assert query["state"] == [auth.state]
        assert query["nonce"] == [auth.nonce]
        assert query["code_challenge_method"] == ["S256"]
        expected = (
            base64.urlsafe_b64encode(
                hashlib.sha256(auth.code_verifier.encode()).digest()
            )
            .decode()
            .rstrip("=")
        )
        assert query["code_challenge"] == [expected]
        assert len(auth.state) >= 32 and len(auth.nonce) >= 32


class TestExchange:
    def test_code_is_exchanged_and_id_token_verified(self, service, cognito):
        tokens = service.exchange_code("auth-code", "verifier-1", nonce="nonce-1")

        assert tokens.access_token == "access-token-1"
        assert tokens.refresh_token == "refresh-token-1"
        assert tokens.expires_in == 3600
        assert tokens.claims["sub"] == "user-1"
        assert tokens.claims["cognito:groups"] == ["role:member", "group:sales"]
        sent = next(r for r in cognito.requests if r.url.path == "/oauth2/token")
        form = _form(sent)
        assert form["grant_type"] == ["authorization_code"]
        assert form["code"] == ["auth-code"]
        assert form["code_verifier"] == ["verifier-1"]
        assert form["redirect_uri"] == [REDIRECT]
        assert sent.headers["authorization"].startswith("Basic ")

    @pytest.mark.parametrize(
        "id_token",
        [
            _id_token(key=_OTHER_KEY),
            _id_token(iss="https://evil.example.com"),
            _id_token(aud="another-client"),
            _id_token(exp=NOW - 1),
            _id_token(nonce="other-nonce"),
            _id_token(nonce=None),
            _id_token(token_use="access"),
        ],
        ids=["signature", "iss", "aud", "exp", "nonce", "nonce-missing", "use"],
    )
    def test_invalid_id_token_is_rejected(self, service, cognito, id_token):
        cognito.token = lambda request: _token_response(id_token)
        with raises_code(OidcError, ErrorCode.OIDC_TOKEN_INVALID):
            service.exchange_code("auth-code", "verifier-1", nonce="nonce-1")


class TestRefresh:
    def test_refresh_returns_new_tokens_and_claims(self, service, cognito):
        cognito.token = lambda request: _token_response(
            _id_token(nonce=None), refresh_token=None, access_token="access-token-2"
        )
        tokens = service.refresh("refresh-token-1")

        assert tokens.access_token == "access-token-2"
        # Cognito が更新トークンを回さなければ、前の更新トークンのまま
        assert tokens.refresh_token == "refresh-token-1"
        assert tokens.claims["cognito:groups"] == ["role:member", "group:sales"]
        sent = [r for r in cognito.requests if r.url.path == "/oauth2/token"][-1]
        form = _form(sent)
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["refresh-token-1"]

    def test_invalid_grant_is_permanent(self, service, cognito):
        cognito.token = lambda request: httpx.Response(
            400, json={"error": "invalid_grant"}
        )
        with raises_code(OidcError, ErrorCode.OIDC_GRANT_INVALID):
            service.refresh("revoked-refresh-token")

    @pytest.mark.parametrize(
        "behaviour",
        [
            "timeout",
            "connect",
            "server_error",
            "throttled",
            "jwks_down",
        ],
    )
    def test_unreachable_cognito_is_transient(self, service, cognito, behaviour):
        def raise_timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        def raise_connect(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable", request=request)

        if behaviour == "timeout":
            cognito.token = raise_timeout
        elif behaviour == "connect":
            cognito.token = raise_connect
        elif behaviour == "server_error":
            cognito.token = lambda request: httpx.Response(503, text="unavailable")
        elif behaviour == "throttled":
            cognito.token = lambda request: httpx.Response(
                429, json={"error": "too_many_requests"}
            )
        else:
            cognito.jwks = lambda request: httpx.Response(503)

        with raises_code(OidcError, ErrorCode.OIDC_UNAVAILABLE):
            service.refresh("refresh-token-1")

    def test_requests_wait_at_most_five_seconds(self, service):
        assert service.timeout_seconds == 5.0


class TestRevokeAndLogout:
    def test_revoke_sends_the_refresh_token(self, service, cognito):
        service.revoke("refresh-token-1")
        form = _form(cognito.requests[-1])
        assert cognito.requests[-1].url.path == "/oauth2/revoke"
        assert form["token"] == ["refresh-token-1"]

    def test_failed_revoke_raises(self, service, cognito):
        cognito.revoke = lambda request: httpx.Response(503)
        with pytest.raises(OidcError):
            service.revoke("refresh-token-1")

    def test_logout_url_returns_to_the_app_login(self, service):
        url = urlparse(service.logout_url("https://app.example.com/login"))
        query = parse_qs(url.query)
        assert url.netloc == DOMAIN and url.path == "/logout"
        assert query["client_id"] == [CLIENT_ID]
        assert query["logout_uri"] == ["https://app.example.com/login"]


class TestCircuitBreaker:
    def _fail_five_times(self, service, cognito) -> None:
        cognito.token = lambda request: httpx.Response(503)
        for _ in range(5):
            with pytest.raises(OidcError):
                service.refresh("refresh-token-1")

    def test_opens_after_five_consecutive_failures(self, service, cognito, opened):
        self._fail_five_times(service, cognito)
        calls = cognito.calls_to("/oauth2/token")

        with raises_code(OidcError, ErrorCode.OIDC_UNAVAILABLE):
            service.refresh("refresh-token-1")

        assert cognito.calls_to("/oauth2/token") == calls
        assert opened == ["opened"]

    def test_half_open_after_sixty_seconds_and_closes_on_success(
        self, service, cognito, clock
    ):
        self._fail_five_times(service, cognito)
        clock.now += 59
        with pytest.raises(OidcError):
            service.refresh("refresh-token-1")

        clock.now += 1
        cognito.token = lambda request: _token_response(_id_token(nonce=None))
        assert service.refresh("refresh-token-1").access_token == "access-token-1"
        # 閉に戻ったので、続く呼び出しも Cognito に届く
        service.refresh("refresh-token-1")
        assert cognito.calls_to("/oauth2/token") == 7

    def test_half_open_allows_one_trial_and_reopens_on_failure(self, clock, opened):
        breaker = CircuitBreaker(clock=clock)
        breaker.add_open_listener(lambda: opened.append("opened"))
        for _ in range(5):
            breaker.before_call()
            breaker.record_failure()
        clock.now += 60

        breaker.before_call()  # 半開の 1 回
        with raises_code(OidcError, ErrorCode.OIDC_UNAVAILABLE):
            breaker.before_call()  # 結果が出るまで他は呼ばない
        breaker.record_failure()
        with raises_code(OidcError, ErrorCode.OIDC_UNAVAILABLE):
            breaker.before_call()
        assert opened == ["opened", "opened"]

    def test_permanent_failures_do_not_count(self, service, cognito, opened):
        cognito.token = lambda request: httpx.Response(
            400, json={"error": "invalid_grant"}
        )
        for _ in range(6):
            with raises_code(OidcError, ErrorCode.OIDC_GRANT_INVALID):
                service.refresh("refresh-token-1")
        assert opened == []
        assert cognito.calls_to("/oauth2/token") == 6


def test_error_messages_do_not_contain_tokens(service, cognito):
    cognito.token = lambda request: httpx.Response(
        400, json={"error": "invalid_grant", "error_description": "secret-token-x"}
    )
    with pytest.raises(OidcError) as exc_info:
        service.refresh("secret-refresh-token")
    text = str(exc_info.value) + json.dumps(exc_info.value.context)
    assert "secret-refresh-token" not in text
    assert "secret-token-x" not in text


class ClosableTransport(httpx.MockTransport):
    """MockTransport that refuses requests after close (like a closed pool)."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__(handler)
        self.closed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.closed:
            raise httpx.ConnectError("transport closed")
        return super().handle_request(request)

    def close(self) -> None:
        self.closed = True


class TestSharedTransport:
    """The pooled transport outlives each short-lived client (concurrent users)."""

    def _service(self, transport: httpx.BaseTransport) -> OidcService:
        return OidcService(
            user_pool_id=POOL_ID,
            client_id=CLIENT_ID,
            client_secret="test-client-secret",
            domain=DOMAIN,
            redirect_uri=REDIRECT,
            transport=transport,
            wall_clock=lambda: NOW,
        )

    def test_finished_calls_do_not_close_the_shared_transport(self, cognito):
        transport = ClosableTransport(cognito)
        service = self._service(transport)

        service.create_authorization_request()
        service.exchange_code("code-1", "verifier-1", nonce="nonce-1")
        service.refresh("refresh-token-1")
        service.revoke("refresh-token-1")

        assert transport.closed is False
        service.refresh("refresh-token-1")  # まだ使える

    def test_a_call_finishing_mid_flight_does_not_break_another(self, cognito):
        transport = ClosableTransport(cognito)
        service = self._service(transport)

        def token_while_another_user_finishes(request: httpx.Request) -> httpx.Response:
            # 別の利用者の処理がこの要求の途中で終わる（そのクライアントが閉じられる）
            service.create_authorization_request()
            service.revoke("other-refresh-token")
            if transport.closed:
                raise httpx.ConnectError("connection closed by another client")
            return _token_response()

        cognito.token = token_while_another_user_finishes
        tokens = service.refresh("refresh-token-1")

        assert tokens.access_token == "access-token-1"

    def test_close_releases_the_transport(self, cognito):
        transport = ClosableTransport(cognito)
        service = self._service(transport)

        service.close()

        assert transport.closed is True
