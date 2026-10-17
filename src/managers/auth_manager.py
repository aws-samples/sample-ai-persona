"""
Login decisions (v1.0.0 U1): login, per-request session check, token refresh,
logout and the startup check.

Business rules implemented here (``functional-design/rules.md``):

- BR1.3 return path validation, BR1.4 callback verification, BR1.5 new
  session id stored as a hash only, BR1.6 roles / departments from groups;
- BR1.7 8-hour idle and BR1.8 7-day absolute expiry, BR1.10 activity write at
  most every 5 minutes, BR1.21 auth method must match the running AUTH_MODE;
- BR1.11 refresh 60 seconds before expiry with permanent / transient handling
  and a 15-minute grace, BR1.12 conditional writes where the first writer wins;
- BR1.13 logout, BR1.14 encrypted tokens, BR1.15 local-only login and the
  startup check, BR1.18 fail closed (503) when the Sessions table is down.

Every method is synchronous because it talks to DynamoDB / Cognito; the
middleware and routers run them in a worker thread. Service exceptions are
converted to :class:`~src.models.errors.AuthRequiredError` (401 / login),
:class:`~src.models.errors.LookupUnavailableError` (503) or this module's
:class:`LoginFailedError` / :class:`AuthConfigError` before they leave.
"""

import base64
import hashlib
import json
import logging
import secrets
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import TYPE_CHECKING, Any, TextIO
from urllib.parse import urlsplit

from src.config import Config, config
from src.models.authz import Principal, Role
from src.models.errors import (
    AuthRequiredError,
    CodedError,
    ErrorCode,
    ErrorKind,
    LookupUnavailableError,
)
from src.models.login_challenge import LoginChallenge, challenge_key
from src.models.login_session import AuthMethod, LoginSession, SessionStore
from src.models.password_policy import (
    MAX_PASSWORD_LENGTH,
    PasswordPolicy,
    PasswordPolicyConfigError,
)
from src.services.cognito.cognito_auth_service import (
    CognitoAuthError,
    CognitoChallenge,
    CognitoTokens,
)
from src.services.cognito.oidc_service import OidcError, TokenSet
from src.services.session_store_service import SessionStoreError

from .shared.structured_log import log_event
from .shared.token_cipher import TokenCipher, TokenDecryptError, TokenKeyConfigError

if TYPE_CHECKING:
    from src.services.cognito.cognito_auth_service import CognitoAuthService
    from src.services.cognito.oidc_service import OidcService

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = timedelta(hours=8)
ABSOLUTE_TIMEOUT = timedelta(days=7)
ACTIVITY_WRITE_INTERVAL = timedelta(minutes=5)
REFRESH_LEEWAY = timedelta(seconds=60)
REFRESH_FAILURE_GRACE = timedelta(minutes=15)
LOGIN_TRANSACTION_TTL = timedelta(minutes=10)

#: Login methods: ``oidc`` and ``local`` (U1), ``admin_api`` (U8, closed network).
SUPPORTED_MODES = frozenset(
    {AuthMethod.OIDC.value, AuthMethod.ADMIN_API.value, AuthMethod.LOCAL.value}
)
SIGNED_OUT_PATH = "/login?reason=signed_out"
#: Where an ``admin_api`` login goes when Cognito asks for a new password (BR2.1).
NEW_PASSWORD_PATH = "/auth/new-password"
#: Longest sign-in e-mail address accepted before calling Cognito (BR1.2).
MAX_EMAIL_LENGTH = 254
#: The ``reason`` values of ``auth.login_failed`` under ``admin_api`` (NFR8.28).
LOGIN_FAILURE_REASONS = frozenset(
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
_NEW_PASSWORD_REQUIRED = "NEW_PASSWORD_REQUIRED"

_ROLE_PREFIX = "role:"
_GROUP_PREFIX = "group:"
_OIDC_REQUIRED_SETTINGS = (
    "COGNITO_USER_POOL_ID",
    "COGNITO_CLIENT_ID",
    "COGNITO_CLIENT_SECRET",
    "COGNITO_DOMAIN",
    "TOKEN_ENCRYPTION_KEYS",
    "LOGIN_COOKIE_SIGNING_KEY",
    "SESSIONS_TABLE_NAME",
)
#: BR4.1: like ``oidc`` but without the Managed Login domain (plus the policy).
_ADMIN_API_REQUIRED_SETTINGS = tuple(
    name for name in _OIDC_REQUIRED_SETTINGS if name != "COGNITO_DOMAIN"
)


class AuthConfigError(CodedError):
    """Login settings are missing or wrong; the app must not start."""

    code = ErrorCode.AUTH_CONFIG_INVALID


class LoginFailedError(CodedError):
    """The Cognito callback could not be accepted (BR1.4).

    The Router shows the login-failed page (M5); the reason is only logged.
    """

    code = ErrorCode.AUTH_REQUIRED


class AdminLoginError(CodedError):
    """An ``admin_api`` login or first password change was not completed.

    ``code`` is one of the closed-network login codes (``LOGIN_FAILED``,
    ``LOGIN_RATE_LIMITED``, ``LOGIN_UNAVAILABLE``, ``PASSWORD_POLICY_NOT_MET``,
    ``PASSWORD_CONFIRMATION_MISMATCH``, ``LOGIN_CHALLENGE_EXPIRED``). The
    message never contains the e-mail address, a password or Cognito's text.
    """

    code = ErrorCode.LOGIN_FAILED


@dataclass(frozen=True)
class LocalUser:
    """The development user of ``AUTH_MODE=local``."""

    display_name: str | None
    email: str | None
    roles: frozenset[Role] = field(default_factory=frozenset)
    group_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class AuthSettings:
    """The settings the login decisions depend on."""

    mode: str
    app_base_url: str
    local_user: LocalUser
    #: ``admin_api`` only: the User Pool and app client the ID token must name
    #: (BR3.1), and the password rules of the pool (BR4.3).
    user_pool_id: str = ""
    client_id: str = ""
    password_policy: PasswordPolicy | None = None

    @property
    def issuer(self) -> str:
        """The ``iss`` of the pool's ID tokens."""
        region = self.user_pool_id.split("_", 1)[0]
        return f"https://cognito-idp.{region}.amazonaws.com/{self.user_pool_id}"

    @classmethod
    def from_config(cls, cfg: Config) -> "AuthSettings":
        """Build from :mod:`src.config` (unknown local roles are ignored).

        A malformed password policy is left ``None`` here; the startup check
        (:meth:`AuthManager.check_startup`) refuses it first.
        """
        roles = frozenset(
            Role(value)
            for value in _split(cfg.LOCAL_DEV_USER_ROLES)
            if value in {r.value for r in Role}
        )
        policy: PasswordPolicy | None = None
        if cfg.AUTH_MODE == AuthMethod.ADMIN_API.value:
            try:
                policy = PasswordPolicy.from_settings(cfg.password_policy_settings())
            except PasswordPolicyConfigError:
                policy = None
        return cls(
            mode=cfg.AUTH_MODE or "",
            app_base_url=cfg.APP_BASE_URL.rstrip("/"),
            local_user=LocalUser(
                display_name=cfg.LOCAL_DEV_USER_NAME or None,
                email=cfg.LOCAL_DEV_USER_EMAIL or None,
                roles=roles,
                group_ids=frozenset(_split(cfg.LOCAL_DEV_USER_GROUPS)),
            ),
            user_pool_id=cfg.COGNITO_USER_POOL_ID or "",
            client_id=cfg.COGNITO_CLIENT_ID or "",
            password_policy=policy,
        )


@dataclass(frozen=True)
class LoginStart:
    """Where ``/login`` sends the browser and what to keep until the callback.

    ``transaction`` is the LoginTransaction (state, nonce, PKCE verifier,
    next, created_at) for the signed cookie. ``authorization_url`` is
    ``None`` under ``AUTH_MODE=local``.
    """

    authorization_url: str | None
    transaction: dict[str, Any]
    next: str


@dataclass(frozen=True)
class LoginResult:
    """A completed login: the raw session id for the cookie, and where to go."""

    session_id: str
    next: str
    principal: Principal


@dataclass(frozen=True)
class NewPasswordStart:
    """Cognito asked for a first password change (``admin_api``, BR2.1).

    ``challenge_id`` is the raw random id for the signed ``login_tx`` cookie;
    only its hash is stored. ``location`` is the M3 URL.
    """

    challenge_id: str
    location: str = NEW_PASSWORD_PATH


def record_event(event: str, *, stream: TextIO | None = None, **fields: Any) -> None:
    """Write one structured log line for the web layer (access log, CSRF, timing).

    The presentation layer must not import ``src.managers.shared`` directly, so
    it goes through this Manager-level entry point to the single writer.
    """
    log_event(event, stream=stream, **fields)


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _hash_session_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _log_circuit_opened() -> None:
    log_event("auth.circuit_opened", reason="circuit_open")


@dataclass(frozen=True)
class _NewSession:
    """A just-written session and its raw id (for the cookie)."""

    session_id: str
    session: LoginSession


def _challenge_expired(message: str) -> AdminLoginError:
    return AdminLoginError(message, code=ErrorCode.LOGIN_CHALLENGE_EXPIRED)


def _no_required_attributes(parameters: Mapping[str, str]) -> bool:
    """Whether ``requiredAttributes`` is an empty JSON array (BR2.1)."""
    try:
        required = json.loads(parameters.get("requiredAttributes", "[]"))
    except ValueError:
        return False
    return isinstance(required, list) and not required


class _RefreshFailed(Exception):
    """A token refresh failed; ``transient`` selects the U1 handling (BR1.11)."""

    def __init__(self, transient: bool) -> None:
        super().__init__("token refresh failed")
        self.transient = transient


def _token_set(tokens: CognitoTokens, claims: dict[str, Any]) -> TokenSet:
    """Put admin API tokens and their checked claims into the shared shape."""
    return TokenSet(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        claims=claims,
    )


def _jwt_payload(token: str) -> dict[str, Any] | None:
    """The JSON payload of a JWT without checking its signature (BR3.1, Q2 A)."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


class AuthManager:
    """Login, session check, refresh and logout for one AUTH_MODE."""

    def __init__(
        self,
        *,
        settings: AuthSettings | None = None,
        session_store: SessionStore | None = None,
        oidc_service: "OidcService | None" = None,
        cognito_auth_service: "CognitoAuthService | None" = None,
        cipher: TokenCipher | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Create the manager; missing dependencies come from ``service_factory``.

        One Cognito client is held per mode: ``OidcService`` for ``oidc``,
        ``CognitoAuthService`` for ``admin_api``, none for ``local``.
        """
        from src.services.service_factory import service_factory

        self._settings = settings or AuthSettings.from_config(config)
        self._store: SessionStore = session_store or service_factory.get_session_store()
        self._clock = clock
        self._oidc: "OidcService | None" = None
        self._cognito: "CognitoAuthService | None" = None
        self._cipher: TokenCipher | None = None
        mode = self._settings.mode
        if mode == AuthMethod.OIDC.value:
            self._oidc = oidc_service or service_factory.get_oidc_service()
            self._oidc.add_circuit_open_listener(_log_circuit_opened)
        elif mode == AuthMethod.ADMIN_API.value:
            self._cognito = (
                cognito_auth_service or service_factory.get_cognito_auth_service()
            )
            self._cognito.add_circuit_open_listener(_log_circuit_opened)
        if self._oidc is not None or self._cognito is not None:
            self._cipher = cipher or TokenCipher(config.TOKEN_ENCRYPTION_KEYS or "")
        self._locks_guard = Lock()
        self._refresh_locks: dict[str, tuple[Lock, int]] = {}

    @property
    def mode(self) -> str:
        """The running AUTH_MODE."""
        return self._settings.mode

    @property
    def secure_cookies(self) -> bool:
        """Whether cookies get ``Secure`` (always outside ``local``; Q1)."""
        return self._settings.mode != AuthMethod.LOCAL.value

    # --- startup ---------------------------------------------------------------

    @staticmethod
    def check_startup(cfg: Config) -> None:
        """Refuse to start with an unusable login configuration (BR1.15, NFR4.7).

        Raises:
            AuthConfigError: AUTH_MODE unset / unknown, ``local`` inside ECS,
                or a missing / malformed ``oidc`` / ``admin_api`` setting
                (``admin_api`` also needs the five password policy settings,
                BR4.1).
        """
        mode = cfg.AUTH_MODE
        if mode not in SUPPORTED_MODES:
            logger.error("AUTH_MODE must be one of %s", sorted(SUPPORTED_MODES))
            raise AuthConfigError("AUTH_MODE is unset or not supported")
        if mode == AuthMethod.LOCAL.value:
            if cfg.RUNNING_IN_ECS:
                logger.error("AUTH_MODE=local is refused inside ECS")
                raise AuthConfigError("AUTH_MODE=local cannot run inside ECS")
            return
        required = (
            _ADMIN_API_REQUIRED_SETTINGS
            if mode == AuthMethod.ADMIN_API.value
            else _OIDC_REQUIRED_SETTINGS
        )
        missing = [name for name in required if not getattr(cfg, name)]
        if missing:
            logger.error("AUTH_MODE=%s requires settings: %s", mode, ", ".join(missing))
            raise AuthConfigError(f"missing {mode} settings: {', '.join(missing)}")
        if mode == AuthMethod.ADMIN_API.value:
            try:
                PasswordPolicy.from_settings(cfg.password_policy_settings())
            except PasswordPolicyConfigError as e:
                logger.error("password policy settings are invalid: %s", e)
                raise AuthConfigError("password policy settings are invalid") from e
        try:
            TokenCipher(cfg.TOKEN_ENCRYPTION_KEYS or "")
        except TokenKeyConfigError as e:
            logger.error("TOKEN_ENCRYPTION_KEYS is malformed")
            raise AuthConfigError("TOKEN_ENCRYPTION_KEYS is malformed") from e

    # --- login -----------------------------------------------------------------

    @staticmethod
    def sanitize_next(value: str | None) -> str:
        """Accept only an in-app relative path as the return target (BR1.3)."""
        if not value or not value.startswith("/") or value.startswith("//"):
            return "/"
        if "\\" in value or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            return "/"
        parts = urlsplit(value)
        if parts.scheme or parts.netloc:
            return "/"
        return value

    def begin_login(self, next_url: str | None) -> LoginStart:
        """Start a login: the Cognito URL and the LoginTransaction to keep."""
        target = self.sanitize_next(next_url)
        if self._oidc is None:
            return LoginStart(authorization_url=None, transaction={}, next=target)
        auth = self._oidc.create_authorization_request()
        transaction = {
            "state": auth.state,
            "nonce": auth.nonce,
            "code_verifier": auth.code_verifier,
            "next": target,
            "created_at": int(self._clock().timestamp()),
        }
        return LoginStart(
            authorization_url=auth.url, transaction=transaction, next=target
        )

    def complete_login(
        self,
        transaction: Mapping[str, Any] | None,
        *,
        state: str | None,
        code: str | None,
        error: str | None,
        previous_session_id: str | None,
    ) -> LoginResult:
        """Verify the Cognito callback and create a new session (BR1.4〜BR1.6).

        Raises:
            LoginFailedError: The callback cannot be accepted (nothing is stored).
            LookupUnavailableError: The Sessions table is unreachable.
        """
        reason = self._transaction_problem(transaction, state, code, error)
        if reason is not None or transaction is None or self._oidc is None:
            self._login_failed(reason or "not_configured")
            raise LoginFailedError(f"login callback rejected: {reason}")
        try:
            tokens = self._oidc.exchange_code(
                str(code), str(transaction["code_verifier"]), nonce=transaction["nonce"]
            )
        except OidcError as e:
            self._login_failed("exchange_failed", error_code=e.code.value)
            raise LoginFailedError("code exchange failed") from e
        session = self._create_session(tokens, previous_session_id)
        log_event(
            "auth.login_succeeded", sub=session.session.user_sub, auth_method=self.mode
        )
        return LoginResult(
            session_id=session.session_id,
            next=self.sanitize_next(str(transaction.get("next") or "/")),
            principal=session.session.to_principal(),
        )

    def login_local(
        self, next_url: str | None, previous_session_id: str | None
    ) -> LoginResult:
        """Sign in as the configured development user (``AUTH_MODE=local``)."""
        user = self._settings.local_user
        self._remove_previous(previous_session_id)
        session_id = self.start_local_session(
            display_name=user.display_name,
            email=user.email,
            roles=user.roles,
            group_ids=user.group_ids,
        )
        session = self._read(_hash_session_id(session_id))
        if session is None:
            raise AuthRequiredError("local session vanished right after login")
        return LoginResult(
            session_id=session_id,
            next=self.sanitize_next(next_url),
            principal=session.to_principal(),
        )

    def start_local_session(
        self,
        *,
        display_name: str | None,
        email: str | None,
        roles: Iterable[Role | str],
        group_ids: Iterable[str],
    ) -> str:
        """Create a token-less ``local`` session and return its raw id.

        Raises:
            AuthConfigError: Not running under ``AUTH_MODE=local``.
        """
        if self.mode != AuthMethod.LOCAL.value:
            raise AuthConfigError("local sessions exist only under AUTH_MODE=local")
        session_id = secrets.token_urlsafe(32)
        session = LoginSession.create_new(
            session_id_hash=_hash_session_id(session_id),
            user_sub=f"local:{email or display_name or 'dev'}",
            auth_method=AuthMethod.LOCAL,
            now=self._clock(),
            roles=frozenset(Role(r) for r in roles),
            group_ids=frozenset(group_ids),
            display_name=display_name,
            email=email,
        )
        self._put(session)
        log_event("auth.login_succeeded", sub=session.user_sub, auth_method=self.mode)
        return session_id

    # --- admin_api login (U8, closed network) -------------------------------------

    @property
    def password_policy(self) -> PasswordPolicy:
        """The password rules of the pool (``admin_api`` only, BR4.3).

        Raises:
            AuthConfigError: Not running under ``admin_api`` or no policy.
        """
        policy = self._settings.password_policy
        if self.mode != AuthMethod.ADMIN_API.value or policy is None:
            raise AuthConfigError("password policy exists only under admin_api")
        return policy

    def _admin_api(self) -> "CognitoAuthService":
        if self._cognito is None:
            raise AuthConfigError("this login exists only under AUTH_MODE=admin_api")
        return self._cognito

    def login_admin_api(
        self,
        *,
        email: str,
        password: str,
        next_url: str | None,
        previous_session_id: str | None,
    ) -> LoginResult | NewPasswordStart:
        """Sign in with e-mail and password through Cognito (WF1, BR1.2〜BR1.6).

        The password is only passed on to Cognito; it is never stored, logged
        or put into an exception (BR4.5).

        Returns:
            :class:`LoginResult` with a new session, or :class:`NewPasswordStart`
            when Cognito asks for a first password change (BR2.1).

        Raises:
            AdminLoginError: ``LOGIN_FAILED`` / ``LOGIN_RATE_LIMITED`` /
                ``LOGIN_UNAVAILABLE``; no session is created.
            LookupUnavailableError: The Sessions table is unreachable (503).
        """
        cognito = self._admin_api()
        target = self.sanitize_next(next_url)
        address = email.strip()
        if not address or len(address) > MAX_EMAIL_LENGTH:
            raise self._login_refused("invalid_input")
        if not password or len(password) > MAX_PASSWORD_LENGTH:
            raise self._login_refused("invalid_input")
        try:
            answer = cognito.initiate_auth(address, password)
        except CognitoAuthError as e:
            raise self._cognito_refused(e) from None
        if isinstance(answer, CognitoChallenge):
            return self._start_new_password(answer, target)
        return self._finish_login(answer, target, previous_session_id)

    def check_challenge(self, challenge_id: str | None) -> PasswordPolicy:
        """Confirm that M3 may be shown for this challenge (WF2 1, BR2.4).

        Returns:
            The password policy for the rule list.

        Raises:
            AdminLoginError: ``LOGIN_CHALLENGE_EXPIRED`` (the item is removed).
            LookupUnavailableError: The Sessions table is unreachable.
        """
        self._admin_api()
        self._load_challenge(challenge_id)
        return self.password_policy

    def complete_new_password(
        self,
        *,
        challenge_id: str | None,
        new_password: str,
        confirmation: str,
        previous_session_id: str | None,
    ) -> LoginResult:
        """Set the first password and sign in (WF2, BR2.4〜BR2.7).

        Order: read the challenge, check the rules and the confirmation
        (without touching the challenge), take the challenge out exactly once,
        then call Cognito once. Retryable answers write the challenge back.

        Raises:
            AdminLoginError: ``PASSWORD_POLICY_NOT_MET`` (``context["rules"]``
                lists the missed rule keys), ``PASSWORD_CONFIRMATION_MISMATCH``,
                ``LOGIN_CHALLENGE_EXPIRED``, ``LOGIN_RATE_LIMITED`` or
                ``LOGIN_UNAVAILABLE``.
            LookupUnavailableError: The Sessions table is unreachable.
        """
        cognito = self._admin_api()
        challenge, cognito_session = self._load_challenge(challenge_id)
        self._check_new_password(new_password, confirmation)
        claimed = self._claim(challenge.session_id_hash)
        try:
            answer = cognito.respond_new_password(
                claimed.username, new_password, cognito_session
            )
        except CognitoAuthError as e:
            raise self._new_password_refused(e, claimed) from None
        if isinstance(answer, CognitoChallenge):
            self._login_failed("unsupported_challenge")
            raise _challenge_expired("another challenge after the new password")
        try:
            return self._finish_login(
                answer, claimed.next, previous_session_id, first_login=True
            )
        except AdminLoginError:
            # The password was changed; the user signs in again from M2.
            raise _challenge_expired("ID token refused after the new password")

    def _start_new_password(
        self, challenge: CognitoChallenge, target: str
    ) -> NewPasswordStart:
        """Keep the Cognito session for M3 (BR2.1〜BR2.3) or refuse it (BR1.6)."""
        username = challenge.parameters.get("USER_ID_FOR_SRP", "")
        if (
            challenge.name != _NEW_PASSWORD_REQUIRED
            or not username
            or not _no_required_attributes(challenge.parameters)
        ):
            raise self._login_refused("unsupported_challenge")
        assert self._cipher is not None
        challenge_id = secrets.token_urlsafe(32)
        item = LoginChallenge.create_new(
            challenge_id=challenge_id,
            cognito_session_enc=self._cipher.encrypt(challenge.session),
            key_version=self._cipher.current_version,
            username=username,
            next=target,
            now=self._clock(),
        )
        try:
            self._store.put_challenge(item)
        except SessionStoreError as e:
            raise LookupUnavailableError("Sessions table unreachable") from e
        return NewPasswordStart(challenge_id=challenge_id)

    def _finish_login(
        self,
        tokens: CognitoTokens,
        target: str,
        previous_session_id: str | None,
        *,
        first_login: bool = False,
    ) -> LoginResult:
        """Check the ID token and create the session (BR1.3, BR3.1)."""
        claims = self._verified_claims(tokens.id_token)
        if claims is None:
            raise self._login_refused("token_invalid")
        created = self._create_session(_token_set(tokens, claims), previous_session_id)
        extra: dict[str, Any] = {"first_login": True} if first_login else {}
        log_event(
            "auth.login_succeeded",
            sub=created.session.user_sub,
            auth_method=self.mode,
            **extra,
        )
        return LoginResult(
            session_id=created.session_id,
            next=self.sanitize_next(target),
            principal=created.session.to_principal(),
        )

    def _verified_claims(self, id_token: str) -> dict[str, Any] | None:
        """The ID token claims when iss / aud / exp / token_use / sub match (BR3.1).

        The signature is not checked: the token comes from an AWS API call over
        TLS signed with IAM, not through the browser (Q2 A).
        """
        claims = _jwt_payload(id_token)
        if claims is None:
            return None
        sub = claims.get("sub")
        exp = claims.get("exp")
        if not (
            claims.get("iss") == self._settings.issuer
            and claims.get("aud") == self._settings.client_id
            and claims.get("token_use") == "id"
            and isinstance(exp, (int, float))
            and not isinstance(exp, bool)
            and exp > self._clock().timestamp()
            and isinstance(sub, str)
            and sub
            and claims.get("cognito:username") == sub
        ):
            return None
        return claims

    def _load_challenge(self, challenge_id: str | None) -> tuple[LoginChallenge, str]:
        """The valid challenge and its decrypted Cognito session (BR2.4)."""
        if not challenge_id:
            raise _challenge_expired("no challenge id")
        key = challenge_key(challenge_id)
        try:
            challenge = self._store.get_challenge(key)
        except SessionStoreError as e:
            raise LookupUnavailableError("Sessions table unreachable") from e
        if challenge is None:
            raise _challenge_expired("challenge not found")
        if challenge.is_expired(self._clock()):
            self._drop_challenge(key)
            raise _challenge_expired("challenge older than three minutes")
        assert self._cipher is not None
        try:
            return challenge, self._cipher.decrypt(challenge.cognito_session_enc)
        except TokenDecryptError:
            self._drop_challenge(key)
            raise _challenge_expired("challenge could not be decrypted") from None

    def _check_new_password(self, new_password: str, confirmation: str) -> None:
        """BR2.5: the rules first, then the confirmation; Cognito is not called."""
        missed = self.password_policy.violations(new_password)
        if missed:
            raise AdminLoginError(
                "new password misses policy rules",
                code=ErrorCode.PASSWORD_POLICY_NOT_MET,
                context={"rules": [rule.value for rule in missed]},
            )
        if not secrets.compare_digest(
            new_password.encode("utf-8"), confirmation.encode("utf-8")
        ):
            raise AdminLoginError(
                "confirmation differs",
                code=ErrorCode.PASSWORD_CONFIRMATION_MISMATCH,
                context={"field": "confirmation"},
            )

    def _claim(self, key: str) -> LoginChallenge:
        """Take the challenge out once (BR2.7); losing the race is expiry."""
        try:
            claimed = self._store.claim_challenge(key)
        except SessionStoreError as e:
            raise LookupUnavailableError("Sessions table unreachable") from e
        if claimed is None:
            raise _challenge_expired("challenge already used")
        return claimed

    def _new_password_refused(
        self, error: CognitoAuthError, claimed: LoginChallenge
    ) -> AdminLoginError:
        """Map Cognito's answer to M3 (BR2.6); retryable answers restore it."""
        code = error.code
        if code in (ErrorCode.LOGIN_CHALLENGE_EXPIRED, ErrorCode.LOGIN_FAILED):
            return _challenge_expired("Cognito refused the challenge session")
        self._restore(claimed)
        if code is ErrorCode.PASSWORD_POLICY_NOT_MET:
            return AdminLoginError(
                "Cognito refused the new password",
                code=code,
                context={"rules": []},
            )
        self._login_failed_for(error)
        return AdminLoginError("new password not set", code=code)

    def _restore(self, claimed: LoginChallenge) -> None:
        """Write the claimed challenge back unchanged (BR2.7, NFR9.45)."""
        try:
            self._store.restore_challenge(claimed)
        except SessionStoreError:
            # The user starts again from M2; the password was not changed.
            logger.warning("could not write the challenge back", exc_info=True)

    def _drop_challenge(self, key: str) -> None:
        try:
            self._store.claim_challenge(key)
        except SessionStoreError:
            logger.warning("could not delete an expired challenge", exc_info=True)

    def _login_refused(self, reason: str) -> AdminLoginError:
        """Log one ``auth.login_failed`` and build the ``LOGIN_FAILED`` error."""
        self._login_failed(reason)
        return AdminLoginError(f"login refused: {reason}", code=ErrorCode.LOGIN_FAILED)

    def _cognito_refused(self, error: CognitoAuthError) -> AdminLoginError:
        self._login_failed_for(error)
        return AdminLoginError("login not completed", code=error.code)

    def _login_failed_for(self, error: CognitoAuthError) -> None:
        if error.unexpected:
            self._login_failed("unexpected_error", error_type=error.error_type)
            return
        reasons = {
            ErrorCode.LOGIN_FAILED: "bad_credentials",
            ErrorCode.LOGIN_RATE_LIMITED: "rate_limited",
        }
        self._login_failed(reasons.get(error.code, "unavailable"))

    # --- per-request check -------------------------------------------------------

    def check_request(self, session_id: str | None, *, passive: bool) -> Principal:
        """Resolve the Principal for one request (WF2).

        Args:
            session_id: The raw ``session_id`` cookie value, if any.
            passive: SSE streams and polling do not count as activity (BR1.9).

        Raises:
            AuthRequiredError: Not signed in (``AUTH_REQUIRED``) or expired
                (``SESSION_EXPIRED``).
            LookupUnavailableError: The Sessions table / Cognito outage
                exceeded what the rules allow (503).
        """
        if not session_id:
            raise AuthRequiredError("no session cookie")
        session_hash = _hash_session_id(session_id)
        session = self._read(session_hash)
        if session is None:
            raise AuthRequiredError("session not found")
        if session.auth_method.value != self.mode:
            self._delete_quietly(session_hash)
            raise AuthRequiredError("session auth method differs from AUTH_MODE")
        now = self._clock()
        expired = self._expiry_reason(session, now)
        if expired is not None:
            self._delete_quietly(session_hash)
            log_event(
                "auth.session_expired",
                sub=session.user_sub,
                auth_method=self.mode,
                reason=expired,
            )
            raise AuthRequiredError(
                f"session expired: {expired}", code=ErrorCode.SESSION_EXPIRED
            )
        if self._needs_refresh(session, now):
            session = self._refresh(session, now)
        if not passive and now - session.last_accessed_at >= ACTIVITY_WRITE_INTERVAL:
            self._touch(session.touch(now))
        return session.to_principal()

    # --- logout ------------------------------------------------------------------

    def logout(self, session_id: str | None) -> str:
        """End the session here and at Cognito; return where to redirect (BR1.13).

        Raises:
            LookupUnavailableError: The session row could not be deleted, so the
                logout is not confirmed (503).
        """
        if session_id:
            self._end_session(_hash_session_id(session_id))
        if self._oidc is None:
            # local and admin_api go to the app's own login page (BR3.3).
            return SIGNED_OUT_PATH
        return self._oidc.logout_url(f"{self._settings.app_base_url}/login")

    def _end_session(self, session_hash: str) -> None:
        try:
            session = self._store.get(session_hash)
        except SessionStoreError:
            logger.warning("could not read the session at logout", exc_info=True)
            session = None
        if session is not None and session.refresh_token_enc and self._has_provider:
            self._revoke(session)
        # Logout must not report success while the old cookie still works, so a
        # delete failure surfaces as 503 (the user can sign out again).
        try:
            self._store.delete(session_hash)
        except SessionStoreError as e:
            raise LookupUnavailableError("Sessions table unreachable at logout") from e
        log_event(
            "auth.logout",
            sub=session.user_sub if session else None,
            auth_method=self.mode,
        )

    @property
    def _has_provider(self) -> bool:
        """Whether this mode talks to Cognito (``oidc`` / ``admin_api``)."""
        return self._oidc is not None or self._cognito is not None

    def _revoke(self, session: LoginSession) -> None:
        assert self._cipher is not None
        try:
            refresh_token = self._cipher.decrypt(session.refresh_token_enc or "")
            if self._cognito is not None:
                self._cognito.revoke(refresh_token)
            elif self._oidc is not None:
                self._oidc.revoke(refresh_token)
        except (OidcError, CognitoAuthError, TokenDecryptError):
            logger.warning("refresh token revocation failed", exc_info=True)
            log_event(
                "auth.logout",
                sub=session.user_sub,
                auth_method=self.mode,
                reason="revoke_failed",
            )

    # --- refresh -----------------------------------------------------------------

    def _needs_refresh(self, session: LoginSession, now: datetime) -> bool:
        if session.auth_method is AuthMethod.LOCAL or not self._has_provider:
            return False
        expires_at = session.access_expires_at
        return expires_at is None or expires_at - REFRESH_LEEWAY <= now

    def _refresh(self, session: LoginSession, now: datetime) -> LoginSession:
        """Refresh once per session in this task; later waiters reuse the result."""
        with self._refresh_slot(session.session_id_hash) as waited:
            if waited:
                current = self._read(session.session_id_hash)
                if current is None:
                    raise AuthRequiredError("session removed during refresh")
                if not self._needs_refresh(current, now):
                    return current
                session = current
            return self._refresh_now(session, now)

    @contextmanager
    def _refresh_slot(self, session_hash: str) -> Iterator[bool]:
        """Per-session lock. Yields whether this caller had to wait for it."""
        with self._locks_guard:
            lock, users = self._refresh_locks.get(session_hash, (Lock(), 0))
            self._refresh_locks[session_hash] = (lock, users + 1)
        waited = not lock.acquire(blocking=False)
        if waited:
            lock.acquire()
        try:
            yield waited
        finally:
            lock.release()
            with self._locks_guard:
                _, users = self._refresh_locks[session_hash]
                if users <= 1:
                    del self._refresh_locks[session_hash]
                else:
                    self._refresh_locks[session_hash] = (lock, users - 1)

    def _refresh_tokens(self, session: LoginSession, refresh_token: str) -> TokenSet:
        """Call the mode's Cognito client.

        Raises:
            _RefreshFailed: ``transient`` tells the 15-minute grace from the
                permanent failure that ends the session.
        """
        if self._cognito is not None:
            try:
                tokens = self._cognito.refresh(session.user_sub, refresh_token)
            except CognitoAuthError as e:
                raise _RefreshFailed(e.code.kind is ErrorKind.TRANSIENT) from None
            claims = self._verified_claims(tokens.id_token)
            if claims is None:
                raise _RefreshFailed(transient=False)
            return _token_set(tokens, claims)
        assert self._oidc is not None
        try:
            return self._oidc.refresh(refresh_token)
        except OidcError as e:
            raise _RefreshFailed(e.code.kind is ErrorKind.TRANSIENT) from None

    def _refresh_now(self, session: LoginSession, now: datetime) -> LoginSession:
        assert self._cipher is not None
        try:
            refresh_token = self._cipher.decrypt(session.refresh_token_enc or "")
        except TokenDecryptError as e:
            self._delete_quietly(session.session_id_hash)
            log_event(
                "auth.decrypt_failed", sub=session.user_sub, auth_method=self.mode
            )
            raise AuthRequiredError("stored token could not be decrypted") from e
        try:
            tokens = self._refresh_tokens(session, refresh_token)
        except _RefreshFailed as e:
            if e.transient:
                return self._on_transient_failure(session, now)
            return self._on_permanent_failure(session)
        roles, groups = self._roles_and_groups(tokens.claims)
        updated = session.update(
            roles=roles,
            group_ids=groups,
            display_name=tokens.claims.get("name"),
            email=tokens.claims.get("email"),
            access_token_enc=self._cipher.encrypt(tokens.access_token),
            refresh_token_enc=(
                self._cipher.encrypt(tokens.refresh_token)
                if tokens.refresh_token
                else None
            ),
            key_version=self._cipher.current_version,
            access_expires_at=now + timedelta(seconds=tokens.expires_in),
            refresh_failing_since=None,
        )
        return self._write_or_reread(updated, session.version)

    def _on_permanent_failure(self, session: LoginSession) -> LoginSession:
        log_event(
            "auth.refresh_failed",
            sub=session.user_sub,
            auth_method=self.mode,
            reason="permanent",
        )
        if self._delete(session.session_id_hash, expected_version=session.version):
            raise AuthRequiredError(
                "refresh rejected; session ended", code=ErrorCode.SESSION_EXPIRED
            )
        current = self._read(session.session_id_hash)
        if current is None:
            raise AuthRequiredError(
                "session ended by another task", code=ErrorCode.SESSION_EXPIRED
            )
        return current

    def _on_transient_failure(
        self, session: LoginSession, now: datetime
    ) -> LoginSession:
        log_event(
            "auth.refresh_failed",
            sub=session.user_sub,
            auth_method=self.mode,
            reason="transient",
        )
        failing_since = session.refresh_failing_since or now
        if now - failing_since > REFRESH_FAILURE_GRACE:
            raise LookupUnavailableError("token refresh has failed for too long")
        if session.refresh_failing_since is not None:
            return session
        marked = session.update(refresh_failing_since=now)
        return self._write_or_reread(marked, session.version)

    # --- helpers -------------------------------------------------------------------

    def _transaction_problem(
        self,
        transaction: Mapping[str, Any] | None,
        state: str | None,
        code: str | None,
        error: str | None,
    ) -> str | None:
        """Name the reason the callback must be rejected, or ``None``."""
        if not transaction or not all(
            isinstance(transaction.get(k), str) and transaction.get(k)
            for k in ("state", "nonce", "code_verifier")
        ):
            return "no_transaction"
        created_at = transaction.get("created_at")
        if not isinstance(created_at, int):
            return "no_transaction"
        age = self._clock() - datetime.fromtimestamp(created_at, timezone.utc)
        if age > LOGIN_TRANSACTION_TTL:
            return "transaction_expired"
        if error:
            return "cognito_error"
        if not state or not secrets.compare_digest(
            state.encode("utf-8"), str(transaction["state"]).encode("utf-8")
        ):
            return "state_mismatch"
        if not code:
            return "missing_code"
        return None

    def _login_failed(self, reason: str, **extra: Any) -> None:
        log_event("auth.login_failed", auth_method=self.mode, reason=reason, **extra)

    def _roles_and_groups(
        self, claims: Mapping[str, Any]
    ) -> tuple[frozenset[Role], frozenset[str]]:
        """Split Cognito groups into roles and departments (BR1.6)."""
        groups = claims.get("cognito:groups")
        roles: set[Role] = set()
        departments: set[str] = set()
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, str):
                continue
            if group.startswith(_ROLE_PREFIX):
                value = group[len(_ROLE_PREFIX) :]
                if value in {r.value for r in Role}:
                    roles.add(Role(value))
                else:
                    log_event("auth.unknown_role_group", group=group)
            elif group.startswith(_GROUP_PREFIX) and len(group) > len(_GROUP_PREFIX):
                departments.add(group[len(_GROUP_PREFIX) :])
        return frozenset(roles), frozenset(departments)

    def _create_session(
        self, tokens: TokenSet, previous_session_id: str | None
    ) -> "_NewSession":
        """New session from verified tokens; the browser's old one is dropped.

        Shared by ``oidc`` and ``admin_api`` (BR1.3, U1 BR1.5 / BR1.6 / BR1.14).
        """
        session_id = secrets.token_urlsafe(32)
        session = self._session_from_tokens(
            _hash_session_id(session_id), tokens, self._clock()
        )
        self._remove_previous(previous_session_id)
        self._put(session)
        return _NewSession(session_id=session_id, session=session)

    def _session_from_tokens(
        self, session_hash: str, tokens: TokenSet, now: datetime
    ) -> LoginSession:
        assert self._cipher is not None
        roles, groups = self._roles_and_groups(tokens.claims)
        return LoginSession.create_new(
            session_id_hash=session_hash,
            user_sub=str(tokens.claims["sub"]),
            auth_method=AuthMethod(self.mode),
            now=now,
            roles=roles,
            group_ids=groups,
            display_name=tokens.claims.get("name"),
            email=tokens.claims.get("email"),
            access_token_enc=self._cipher.encrypt(tokens.access_token),
            refresh_token_enc=(
                self._cipher.encrypt(tokens.refresh_token)
                if tokens.refresh_token
                else None
            ),
            key_version=self._cipher.current_version,
            access_expires_at=now + timedelta(seconds=tokens.expires_in),
        )

    @staticmethod
    def _expiry_reason(session: LoginSession, now: datetime) -> str | None:
        if now - session.created_at > ABSOLUTE_TIMEOUT:
            return "absolute_timeout"
        if now - session.last_accessed_at > IDLE_TIMEOUT:
            return "idle_timeout"
        return None

    def _remove_previous(self, previous_session_id: str | None) -> None:
        """Drop the session the browser had before this login (R-08 (4))."""
        if previous_session_id:
            self._delete_quietly(_hash_session_id(previous_session_id))

    def _read(self, session_hash: str) -> LoginSession | None:
        try:
            return self._store.get(session_hash)
        except SessionStoreError as e:
            raise LookupUnavailableError("Sessions table unreachable") from e

    def _put(self, session: LoginSession) -> None:
        try:
            self._store.put(session)
        except SessionStoreError as e:
            raise LookupUnavailableError("Sessions table unreachable") from e

    def _delete(self, session_hash: str, *, expected_version: int) -> bool:
        try:
            return self._store.delete(session_hash, expected_version=expected_version)
        except SessionStoreError as e:
            raise LookupUnavailableError("Sessions table unreachable") from e

    def _delete_quietly(self, session_hash: str) -> None:
        try:
            self._store.delete(session_hash)
        except SessionStoreError:
            logger.warning("could not delete a session", exc_info=True)

    def _touch(self, session: LoginSession) -> None:
        """Activity write; a failure only loses the timestamp, so it is logged."""
        try:
            self._store.touch(session)
        except SessionStoreError:
            logger.warning("could not record session activity", exc_info=True)

    def _write_or_reread(self, session: LoginSession, expected: int) -> LoginSession:
        """Versioned write; when another task won, continue with its value."""
        try:
            written = self._store.update_versioned(session, expected_version=expected)
        except SessionStoreError as e:
            raise LookupUnavailableError("Sessions table unreachable") from e
        if written:
            return session
        current = self._read(session.session_id_hash)
        if current is None:
            raise AuthRequiredError("session removed by another task")
        return current
