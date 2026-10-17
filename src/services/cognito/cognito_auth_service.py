"""
Cognito admin API client for the closed-network login (``AUTH_MODE=admin_api``,
v1.0.0 U8, ``functional-spec.md`` "Cognito を呼ぶ層の約束").

Calls ``AdminInitiateAuth`` (``ADMIN_USER_PASSWORD_AUTH`` and
``REFRESH_TOKEN_AUTH``), ``AdminRespondToAuthChallenge``
(``NEW_PASSWORD_REQUIRED``) and ``RevokeToken`` with boto3, computes the
``SECRET_HASH``, and turns every Cognito / botocore failure into
:class:`CognitoAuthError` with one of the closed-network login codes. The
Manager never sees a boto3 exception.

Timeouts (NFR9.39): connect 2 s + read 3 s, one attempt only; this layer never
retries, and ``AdminRespondToAuthChallenge`` is never resent because the
Cognito session is single-use.

Secrets (NFR3.15): passwords are only passed through to boto3. Exception
messages and logs carry the operation and Cognito's error *code* only; Cognito's
message is read for the attempt-limit check and never copied, and the boto3
exception chain is cut (``from None``) so a traceback cannot show the request.

The ID token is returned as a string; its claims are checked by the Manager
(BR3.1). No JWKS is fetched.
"""

import base64
import hashlib
import hmac
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from src.models.errors import CodedError, ErrorCode

from .circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)

#: botocore settings: at most 2 + 3 seconds and a single attempt (NFR9.39).
CLIENT_CONFIG = Config(
    connect_timeout=2,
    read_timeout=3,
    retries={"total_max_attempts": 1},
)

#: Cognito's wording for its attempt limit; the only use of Cognito's message.
_ATTEMPTS_EXCEEDED = "password attempts exceeded"

#: Cognito error code -> login code, shared by sign-in and refresh.
_LOGIN_ERRORS: dict[str, ErrorCode] = {
    "NotAuthorizedException": ErrorCode.LOGIN_FAILED,
    "UserNotFoundException": ErrorCode.LOGIN_FAILED,
    "PasswordResetRequiredException": ErrorCode.LOGIN_FAILED,
    "UserNotConfirmedException": ErrorCode.LOGIN_FAILED,
    "TooManyRequestsException": ErrorCode.LOGIN_RATE_LIMITED,
    "LimitExceededException": ErrorCode.LOGIN_RATE_LIMITED,
}
#: Overrides for ``AdminRespondToAuthChallenge`` (BR2.6).
_RESPOND_ERRORS: dict[str, ErrorCode] = {
    **_LOGIN_ERRORS,
    "InvalidPasswordException": ErrorCode.PASSWORD_POLICY_NOT_MET,
    "NotAuthorizedException": ErrorCode.LOGIN_CHALLENGE_EXPIRED,
    "CodeMismatchException": ErrorCode.LOGIN_CHALLENGE_EXPIRED,
    "ExpiredCodeException": ErrorCode.LOGIN_CHALLENGE_EXPIRED,
}


class CognitoAuthError(CodedError):
    """A Cognito admin API call failed. ``code`` decides how the Manager reacts.

    Attributes:
        unexpected: True for the catch-all (an exception or answer not in the
            table); the Manager logs it as ``unexpected_error``.
        error_type: Name of the Cognito / botocore error for the log, never
            its message.
    """

    code = ErrorCode.LOGIN_UNAVAILABLE

    def __init__(
        self,
        message: str = "",
        *,
        code: ErrorCode | None = None,
        unexpected: bool = False,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.unexpected = unexpected
        self.error_type = error_type


@dataclass(frozen=True)
class CognitoTokens:
    """Tokens returned by Cognito (the ID token is not verified here)."""

    id_token: str
    access_token: str
    refresh_token: str | None
    expires_in: int


@dataclass(frozen=True)
class CognitoChallenge:
    """A challenge Cognito asked for instead of returning tokens."""

    name: str
    session: str
    parameters: dict[str, str] = field(default_factory=dict)


AuthResult = CognitoTokens | CognitoChallenge


class CognitoAuthService:
    """boto3 ``cognito-idp`` client for one confidential app client."""

    def __init__(
        self,
        *,
        user_pool_id: str,
        client_id: str,
        client_secret: str,
        client: Any = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        """Create the client.

        Args:
            user_pool_id: ``<region>_<id>``; the region part selects the endpoint.
            client_id: App client id.
            client_secret: App client secret (``SECRET_HASH`` and ``RevokeToken``).
            client: Optional pre-built ``cognito-idp`` client (tests).
            breaker: Circuit breaker; a fresh one when omitted.
        """
        self.user_pool_id = user_pool_id
        self._client_id = client_id
        self._client_secret = client_secret
        region = user_pool_id.split("_", 1)[0]
        self.client = client or boto3.client(
            "cognito-idp", region_name=region, config=CLIENT_CONFIG
        )
        self.breaker = breaker or CircuitBreaker()

    @staticmethod
    def secret_hash_for(username: str, *, client_id: str, client_secret: str) -> str:
        """Cognito's ``SECRET_HASH``: Base64(HMAC-SHA256(secret, username + id))."""
        digest = hmac.new(
            client_secret.encode("utf-8"),
            (username + client_id).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def _secret_hash(self, username: str) -> str:
        return self.secret_hash_for(
            username, client_id=self._client_id, client_secret=self._client_secret
        )

    def add_circuit_open_listener(self, listener: Callable[[], None]) -> None:
        """Be told when the circuit breaker opens (the Manager logs it)."""
        self.breaker.add_open_listener(listener)

    # --- public API ------------------------------------------------------------

    def initiate_auth(self, email: str, password: str) -> AuthResult:
        """Sign in with ``ADMIN_USER_PASSWORD_AUTH`` (BR1.2).

        Raises:
            CognitoAuthError: ``LOGIN_FAILED`` / ``LOGIN_RATE_LIMITED`` /
                ``LOGIN_UNAVAILABLE``.
        """
        response = self._guarded(
            "sign-in",
            _LOGIN_ERRORS,
            lambda: self.client.admin_initiate_auth(
                UserPoolId=self.user_pool_id,
                ClientId=self._client_id,
                AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                AuthParameters={
                    "USERNAME": email,
                    "PASSWORD": password,
                    "SECRET_HASH": self._secret_hash(email),
                },
            ),
        )
        return self._result("sign-in", response, previous_refresh_token=None)

    def respond_new_password(
        self, username: str, new_password: str, session: str
    ) -> AuthResult:
        """Answer ``NEW_PASSWORD_REQUIRED`` once; never resent (BR2.6, NFR9.39).

        Raises:
            CognitoAuthError: ``PASSWORD_POLICY_NOT_MET`` /
                ``LOGIN_CHALLENGE_EXPIRED`` / ``LOGIN_FAILED`` /
                ``LOGIN_RATE_LIMITED`` / ``LOGIN_UNAVAILABLE``.
        """
        response = self._guarded(
            "new-password",
            _RESPOND_ERRORS,
            lambda: self.client.admin_respond_to_auth_challenge(
                UserPoolId=self.user_pool_id,
                ClientId=self._client_id,
                ChallengeName="NEW_PASSWORD_REQUIRED",
                ChallengeResponses={
                    "USERNAME": username,
                    "NEW_PASSWORD": new_password,
                    "SECRET_HASH": self._secret_hash(username),
                },
                Session=session,
            ),
        )
        return self._result("new-password", response, previous_refresh_token=None)

    def refresh(self, username: str, refresh_token: str) -> CognitoTokens:
        """Refresh with ``REFRESH_TOKEN_AUTH``; keeps the given refresh token (BR3.2).

        Raises:
            CognitoAuthError: ``LOGIN_FAILED`` (permanent) or a TRANSIENT code.
        """
        response = self._guarded(
            "refresh",
            _LOGIN_ERRORS,
            lambda: self.client.admin_initiate_auth(
                UserPoolId=self.user_pool_id,
                ClientId=self._client_id,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={
                    "REFRESH_TOKEN": refresh_token,
                    "SECRET_HASH": self._secret_hash(username),
                },
            ),
        )
        result = self._result("refresh", response, previous_refresh_token=refresh_token)
        if not isinstance(result, CognitoTokens):
            raise CognitoAuthError(
                "refresh answered with a challenge",
                unexpected=True,
                error_type="UnexpectedChallenge",
            )
        return result

    def revoke(self, refresh_token: str) -> None:
        """Revoke the refresh token (logout, BR3.3). Not behind the breaker.

        Raises:
            CognitoAuthError: The revocation did not succeed.
        """
        try:
            self.client.revoke_token(
                Token=refresh_token,
                ClientId=self._client_id,
                ClientSecret=self._client_secret,
            )
        except ClientError as e:
            error_code = _error_code_of(e)
            logger.warning("Cognito revoke failed: %s", error_code)
            raise CognitoAuthError(
                f"revoke failed: {error_code}", error_type=error_code
            ) from None
        except BotoCoreError as e:
            logger.warning("Cognito revoke unreachable: %s", type(e).__name__)
            raise CognitoAuthError(
                f"revoke unreachable: {type(e).__name__}",
                error_type=type(e).__name__,
            ) from None

    # --- internals ---------------------------------------------------------------

    def _guarded(
        self,
        operation: str,
        table: Mapping[str, ErrorCode],
        call: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Run one call behind the breaker and map failures to codes."""
        try:
            self.breaker.before_call()
        except CircuitOpenError:
            raise CognitoAuthError(f"{operation} refused: circuit open") from None
        try:
            response = call()
        except ClientError as e:
            raise self._client_failure(operation, table, e) from None
        except ParamValidationError:
            logger.warning("Cognito %s request was invalid", operation)
            raise CognitoAuthError(
                f"{operation} request invalid",
                unexpected=True,
                error_type="ParamValidationError",
            ) from None
        except BotoCoreError as e:
            self.breaker.record_failure()
            logger.warning("Cognito %s unreachable: %s", operation, type(e).__name__)
            raise CognitoAuthError(
                f"{operation} unreachable: {type(e).__name__}"
            ) from None
        self.breaker.record_success()
        return response

    def _client_failure(
        self, operation: str, table: Mapping[str, ErrorCode], error: ClientError
    ) -> CognitoAuthError:
        error_code = _error_code_of(error)
        status = int(
            error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
        )
        logger.warning("Cognito %s failed: %s", operation, error_code)
        if status >= 500 or error_code == "InternalErrorException":
            self.breaker.record_failure()
            return CognitoAuthError(f"{operation} failed: {error_code}")
        # Cognito is up and answered: not counted by the breaker.
        self.breaker.record_success()
        if error_code == "NotAuthorizedException" and _attempts_exceeded(error):
            code: ErrorCode | None = ErrorCode.LOGIN_RATE_LIMITED
        else:
            code = table.get(error_code)
        if code is None:
            return CognitoAuthError(
                f"{operation} failed: {error_code}",
                unexpected=True,
                error_type=error_code or "ClientError",
            )
        return CognitoAuthError(f"{operation} failed: {error_code}", code=code)

    def _result(
        self,
        operation: str,
        response: Mapping[str, Any],
        *,
        previous_refresh_token: str | None,
    ) -> AuthResult:
        """Tokens or a challenge; any other shape is ``LOGIN_UNAVAILABLE``."""
        result = _tokens_of(response, previous_refresh_token) or _challenge_of(response)
        if result is not None:
            return result
        logger.warning("Cognito %s answered in an unexpected shape", operation)
        raise CognitoAuthError(
            f"{operation} answered in an unexpected shape",
            unexpected=True,
            error_type="UnexpectedResponse",
        )


def _non_empty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _tokens_of(
    response: Mapping[str, Any], previous_refresh_token: str | None
) -> CognitoTokens | None:
    """The ``AuthenticationResult``, or ``None`` when it is absent or incomplete."""
    tokens = response.get("AuthenticationResult")
    if not isinstance(tokens, Mapping):
        return None
    id_token = _non_empty_str(tokens.get("IdToken"))
    access_token = _non_empty_str(tokens.get("AccessToken"))
    expires_in = tokens.get("ExpiresIn")
    if id_token is None or access_token is None or not isinstance(expires_in, int):
        return None
    return CognitoTokens(
        id_token=id_token,
        access_token=access_token,
        refresh_token=_non_empty_str(tokens.get("RefreshToken"))
        or previous_refresh_token,
        expires_in=expires_in,
    )


def _challenge_of(response: Mapping[str, Any]) -> CognitoChallenge | None:
    """The challenge, or ``None`` when the name or the session is missing."""
    name = _non_empty_str(response.get("ChallengeName"))
    session = _non_empty_str(response.get("Session"))
    if name is None or session is None:
        return None
    parameters = response.get("ChallengeParameters") or {}
    return CognitoChallenge(
        name=name,
        session=session,
        parameters={str(k): str(v) for k, v in dict(parameters).items()},
    )


def _error_code_of(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _attempts_exceeded(error: ClientError) -> bool:
    """Whether ``NotAuthorizedException`` is Cognito's attempt limit (BR1.5)."""
    message = str(error.response.get("Error", {}).get("Message", ""))
    return _ATTEMPTS_EXCEEDED in message.lower()
