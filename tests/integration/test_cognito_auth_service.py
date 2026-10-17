"""CognitoAuthService（AUTH_MODE=admin_api の Cognito 管理 API）の結合テスト。

botocore の Stubber で cognito-idp を模す（本物の Cognito は呼ばない）。
functional-spec.md「Cognito を呼ぶ層の約束」、BR1.2、BR1.4、BR1.5、BR2.6、BR3.2、
BR3.3、NFR3.15、NFR4.22、NFR9.39、NFR9.44。
"""

import base64
import hashlib
import hmac
from typing import Any
from unittest.mock import Mock

import pytest
from botocore.exceptions import (
    EndpointConnectionError,
    ParamValidationError,
    ReadTimeoutError,
)
from botocore.stub import Stubber

from src.models.errors import CodedError, ErrorCode
from src.services.cognito.circuit_breaker import CircuitBreaker
from src.services.cognito.cognito_auth_service import (
    CLIENT_CONFIG,
    CognitoAuthError,
    CognitoAuthService,
    CognitoChallenge,
    CognitoTokens,
)
from tests.error_helpers import raises_code
from tests.log_helpers import SENTINEL_PASSWORD, assert_not_leaked

POOL_ID = "ap-northeast-1_TestPool"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret-0123456789"
EMAIL = "sentinel-user@example.com"
USERNAME = "0b5c0f2e-user-sub"
COGNITO_TEXT = "SENTINEL-COGNITO-MESSAGE"
SESSION = "cognito-session-0123456789abcdef"


def _secret_hash(username: str) -> str:
    digest = hmac.new(
        CLIENT_SECRET.encode(), (username + CLIENT_ID).encode(), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode()


def _auth_result(**extra: Any) -> dict[str, Any]:
    result = {
        "IdToken": "id-token-1",
        "AccessToken": "access-token-1",
        "RefreshToken": "refresh-token-1",
        "ExpiresIn": 3600,
        "TokenType": "Bearer",
    }
    result.update(extra)
    return {"AuthenticationResult": result}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def opened() -> list[str]:
    return []


@pytest.fixture
def service(monkeypatch, opened) -> CognitoAuthService:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    svc = CognitoAuthService(
        user_pool_id=POOL_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        breaker=CircuitBreaker(clock=Clock()),
    )
    svc.add_circuit_open_listener(lambda: opened.append("opened"))
    return svc


@pytest.fixture
def stub(service):
    with Stubber(service.client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def _login_params(password: str = SENTINEL_PASSWORD) -> dict[str, Any]:
    return {
        "UserPoolId": POOL_ID,
        "ClientId": CLIENT_ID,
        "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
        "AuthParameters": {
            "USERNAME": EMAIL,
            "PASSWORD": password,
            "SECRET_HASH": _secret_hash(EMAIL),
        },
    }


def _error(
    stub: Stubber,
    operation: str,
    code: str,
    message: str = COGNITO_TEXT,
    status: int = 400,
) -> None:
    stub.add_client_error(
        operation,
        service_error_code=code,
        service_message=message,
        http_status_code=status,
    )


class TestClientSettings:
    def test_one_call_is_at_most_five_seconds_and_never_retried(self, service):
        cfg = service.client.meta.config
        assert cfg.connect_timeout == 2
        assert cfg.read_timeout == 3
        assert cfg.retries["total_max_attempts"] == 1
        assert CLIENT_CONFIG.connect_timeout + CLIENT_CONFIG.read_timeout <= 5

    def test_region_comes_from_the_user_pool_id(self, service):
        assert service.client.meta.region_name == "ap-northeast-1"

    def test_read_timeout_is_one_call_and_unavailable(self, service):
        broken = Mock()
        broken.admin_initiate_auth.side_effect = ReadTimeoutError(
            endpoint_url="https://cognito-idp.invalid"
        )
        service.client = broken
        with raises_code(CognitoAuthError, ErrorCode.LOGIN_UNAVAILABLE):
            service.initiate_auth(EMAIL, SENTINEL_PASSWORD)
        assert broken.admin_initiate_auth.call_count == 1


class TestInitiateAuth:
    def test_tokens(self, service, stub):
        stub.add_response("admin_initiate_auth", _auth_result(), _login_params())
        result = service.initiate_auth(EMAIL, SENTINEL_PASSWORD)
        assert result == CognitoTokens(
            id_token="id-token-1",
            access_token="access-token-1",
            refresh_token="refresh-token-1",
            expires_in=3600,
        )

    def test_new_password_challenge(self, service, stub):
        stub.add_response(
            "admin_initiate_auth",
            {
                "ChallengeName": "NEW_PASSWORD_REQUIRED",
                "Session": SESSION,
                "ChallengeParameters": {
                    "USER_ID_FOR_SRP": USERNAME,
                    "requiredAttributes": "[]",
                    "userAttributes": "{}",
                },
            },
            _login_params(),
        )
        result = service.initiate_auth(EMAIL, SENTINEL_PASSWORD)
        assert isinstance(result, CognitoChallenge)
        assert result.name == "NEW_PASSWORD_REQUIRED"
        assert result.session == SESSION
        assert result.parameters["USER_ID_FOR_SRP"] == USERNAME

    def test_secret_hash_is_hmac_sha256_of_email_and_client_id(self):
        expected = _secret_hash(EMAIL)
        assert (
            CognitoAuthService.secret_hash_for(
                EMAIL, client_id=CLIENT_ID, client_secret=CLIENT_SECRET
            )
            == expected
        )


class TestRespondNewPassword:
    def test_sends_username_new_password_secret_hash_and_session(self, service, stub):
        stub.add_response(
            "admin_respond_to_auth_challenge",
            _auth_result(),
            {
                "UserPoolId": POOL_ID,
                "ClientId": CLIENT_ID,
                "ChallengeName": "NEW_PASSWORD_REQUIRED",
                "ChallengeResponses": {
                    "USERNAME": USERNAME,
                    "NEW_PASSWORD": SENTINEL_PASSWORD,
                    "SECRET_HASH": _secret_hash(USERNAME),
                },
                "Session": SESSION,
            },
        )
        result = service.respond_new_password(USERNAME, SENTINEL_PASSWORD, SESSION)
        assert isinstance(result, CognitoTokens)


class TestRefresh:
    def test_uses_refresh_flow_and_keeps_the_refresh_token(self, service, stub):
        response = _auth_result()
        del response["AuthenticationResult"]["RefreshToken"]
        stub.add_response(
            "admin_initiate_auth",
            response,
            {
                "UserPoolId": POOL_ID,
                "ClientId": CLIENT_ID,
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "AuthParameters": {
                    "REFRESH_TOKEN": "refresh-token-1",
                    "SECRET_HASH": _secret_hash(USERNAME),
                },
            },
        )
        tokens = service.refresh(USERNAME, "refresh-token-1")
        assert tokens.refresh_token == "refresh-token-1"
        assert tokens.access_token == "access-token-1"

    def test_a_challenge_on_refresh_is_unexpected(self, service, stub):
        stub.add_response(
            "admin_initiate_auth",
            {"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": SESSION},
        )
        with raises_code(CognitoAuthError, ErrorCode.LOGIN_UNAVAILABLE):
            service.refresh(USERNAME, "refresh-token-1")


class TestRevoke:
    def test_revokes_with_client_id_and_secret(self, service, stub):
        stub.add_response(
            "revoke_token",
            {},
            {
                "Token": "refresh-token-1",
                "ClientId": CLIENT_ID,
                "ClientSecret": CLIENT_SECRET,
            },
        )
        service.revoke("refresh-token-1")

    def test_failure_is_a_coded_error(self, service, stub):
        _error(stub, "revoke_token", "UnauthorizedException")
        with pytest.raises(CognitoAuthError):
            service.revoke("refresh-token-1")


_LOGIN_TABLE = [
    (
        "NotAuthorizedException",
        "Incorrect username or password.",
        ErrorCode.LOGIN_FAILED,
    ),
    ("UserNotFoundException", COGNITO_TEXT, ErrorCode.LOGIN_FAILED),
    ("PasswordResetRequiredException", COGNITO_TEXT, ErrorCode.LOGIN_FAILED),
    ("UserNotConfirmedException", COGNITO_TEXT, ErrorCode.LOGIN_FAILED),
    (
        "NotAuthorizedException",
        "Password attempts exceeded",
        ErrorCode.LOGIN_RATE_LIMITED,
    ),
    ("TooManyRequestsException", COGNITO_TEXT, ErrorCode.LOGIN_RATE_LIMITED),
    ("LimitExceededException", COGNITO_TEXT, ErrorCode.LOGIN_RATE_LIMITED),
    ("InvalidParameterException", COGNITO_TEXT, ErrorCode.LOGIN_UNAVAILABLE),
    ("ResourceNotFoundException", COGNITO_TEXT, ErrorCode.LOGIN_UNAVAILABLE),
    ("ForbiddenException", COGNITO_TEXT, ErrorCode.LOGIN_UNAVAILABLE),
    (
        "InvalidUserPoolConfigurationException",
        COGNITO_TEXT,
        ErrorCode.LOGIN_UNAVAILABLE,
    ),
]


class TestErrorTable:
    @pytest.mark.parametrize(("aws_code", "message", "code"), _LOGIN_TABLE)
    def test_login_errors(self, service, stub, aws_code, message, code):
        _error(stub, "admin_initiate_auth", aws_code, message)
        with raises_code(CognitoAuthError, code):
            service.initiate_auth(EMAIL, SENTINEL_PASSWORD)

    @pytest.mark.parametrize(("aws_code", "message", "code"), _LOGIN_TABLE)
    def test_refresh_uses_the_same_table(self, service, stub, aws_code, message, code):
        _error(stub, "admin_initiate_auth", aws_code, message)
        with raises_code(CognitoAuthError, code):
            service.refresh(USERNAME, "refresh-token-1")

    def test_internal_error_is_unavailable(self, service, stub):
        _error(stub, "admin_initiate_auth", "InternalErrorException", status=500)
        with raises_code(CognitoAuthError, ErrorCode.LOGIN_UNAVAILABLE):
            service.initiate_auth(EMAIL, SENTINEL_PASSWORD)

    def test_connection_failure_is_unavailable(self, service):
        broken = Mock()
        broken.admin_initiate_auth.side_effect = EndpointConnectionError(
            endpoint_url="https://cognito-idp.invalid"
        )
        service.client = broken
        with raises_code(CognitoAuthError, ErrorCode.LOGIN_UNAVAILABLE):
            service.initiate_auth(EMAIL, SENTINEL_PASSWORD)

    def test_input_validation_failure_is_unavailable(self, service):
        broken = Mock()
        broken.admin_initiate_auth.side_effect = ParamValidationError(report="bad")
        service.client = broken
        with raises_code(CognitoAuthError, ErrorCode.LOGIN_UNAVAILABLE):
            service.initiate_auth(EMAIL, SENTINEL_PASSWORD)

    @pytest.mark.parametrize(
        "response",
        [
            {},
            {"AuthenticationResult": {"AccessToken": "a", "ExpiresIn": 1}},
            {"ChallengeName": "NEW_PASSWORD_REQUIRED"},
        ],
    )
    def test_unexpected_response_shape_is_unavailable(self, service, stub, response):
        stub.add_response("admin_initiate_auth", response)
        with raises_code(CognitoAuthError, ErrorCode.LOGIN_UNAVAILABLE):
            service.initiate_auth(EMAIL, SENTINEL_PASSWORD)

    @pytest.mark.parametrize(
        ("aws_code", "message", "code"),
        [
            (
                "InvalidPasswordException",
                COGNITO_TEXT,
                ErrorCode.PASSWORD_POLICY_NOT_MET,
            ),
            (
                "NotAuthorizedException",
                "Invalid session",
                ErrorCode.LOGIN_CHALLENGE_EXPIRED,
            ),
            ("CodeMismatchException", COGNITO_TEXT, ErrorCode.LOGIN_CHALLENGE_EXPIRED),
            ("ExpiredCodeException", COGNITO_TEXT, ErrorCode.LOGIN_CHALLENGE_EXPIRED),
            ("TooManyRequestsException", COGNITO_TEXT, ErrorCode.LOGIN_RATE_LIMITED),
            ("InvalidParameterException", COGNITO_TEXT, ErrorCode.LOGIN_UNAVAILABLE),
        ],
    )
    def test_respond_errors(self, service, stub, aws_code, message, code):
        _error(stub, "admin_respond_to_auth_challenge", aws_code, message)
        with raises_code(CognitoAuthError, code):
            service.respond_new_password(USERNAME, SENTINEL_PASSWORD, SESSION)

    def test_unexpected_errors_name_their_type_for_the_log(self, service, stub):
        _error(stub, "admin_initiate_auth", "InvalidParameterException")
        with pytest.raises(CognitoAuthError) as info:
            service.initiate_auth(EMAIL, SENTINEL_PASSWORD)
        assert info.value.unexpected is True
        assert info.value.error_type == "InvalidParameterException"

    def test_answered_failures_are_not_unexpected(self, service, stub):
        _error(stub, "admin_initiate_auth", "NotAuthorizedException")
        with pytest.raises(CognitoAuthError) as info:
            service.initiate_auth(EMAIL, SENTINEL_PASSWORD)
        assert info.value.unexpected is False


class TestNoLeak:
    @pytest.mark.parametrize(
        "aws_code", ["NotAuthorizedException", "InternalErrorException"]
    )
    def test_error_never_carries_email_password_or_cognito_text(
        self, service, stub, caplog, aws_code
    ):
        _error(stub, "admin_initiate_auth", aws_code, f"{COGNITO_TEXT} {EMAIL}")
        with pytest.raises(CognitoAuthError) as info:
            service.initiate_auth(EMAIL, SENTINEL_PASSWORD)
        error = info.value
        assert isinstance(error, CodedError)
        assert error.__cause__ is None and error.__suppress_context__
        for secret in (SENTINEL_PASSWORD, EMAIL, COGNITO_TEXT):
            assert_not_leaked(secret, error, caplog)


class TestCircuitBreaker:
    def _timeout_client(self) -> Mock:
        broken = Mock()
        broken.admin_initiate_auth.side_effect = EndpointConnectionError(
            endpoint_url="https://cognito-idp.invalid"
        )
        return broken

    def test_five_unreachable_calls_open_it_and_stop_calling(self, service, opened):
        service.client = self._timeout_client()
        for _ in range(5):
            with pytest.raises(CognitoAuthError):
                service.initiate_auth(EMAIL, SENTINEL_PASSWORD)
        with raises_code(CognitoAuthError, ErrorCode.LOGIN_UNAVAILABLE):
            service.initiate_auth(EMAIL, SENTINEL_PASSWORD)
        assert service.client.admin_initiate_auth.call_count == 5
        assert opened == ["opened"]

    def test_answered_failures_do_not_count(self, service, stub, opened):
        for _ in range(6):
            _error(stub, "admin_initiate_auth", "NotAuthorizedException")
            with raises_code(CognitoAuthError, ErrorCode.LOGIN_FAILED):
                service.initiate_auth(EMAIL, SENTINEL_PASSWORD)
        assert opened == []
