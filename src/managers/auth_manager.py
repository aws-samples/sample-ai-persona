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

import hashlib
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
from src.models.login_session import AuthMethod, LoginSession, SessionStore
from src.services.oidc_service import OidcError, TokenSet
from src.services.session_store_service import SessionStoreError

from .shared.structured_log import log_event
from .shared.token_cipher import TokenCipher, TokenDecryptError, TokenKeyConfigError

if TYPE_CHECKING:
    from src.services.oidc_service import OidcService

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = timedelta(hours=8)
ABSOLUTE_TIMEOUT = timedelta(days=7)
ACTIVITY_WRITE_INTERVAL = timedelta(minutes=5)
REFRESH_LEEWAY = timedelta(seconds=60)
REFRESH_FAILURE_GRACE = timedelta(minutes=15)
LOGIN_TRANSACTION_TTL = timedelta(minutes=10)

#: Login methods implemented in U1 (``admin_api`` arrives with U8).
SUPPORTED_MODES = frozenset({AuthMethod.OIDC.value, AuthMethod.LOCAL.value})
SIGNED_OUT_PATH = "/login?reason=signed_out"

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


class AuthConfigError(CodedError):
    """Login settings are missing or wrong; the app must not start."""

    code = ErrorCode.AUTH_CONFIG_INVALID


class LoginFailedError(CodedError):
    """The Cognito callback could not be accepted (BR1.4).

    The Router shows the login-failed page (M5); the reason is only logged.
    """

    code = ErrorCode.AUTH_REQUIRED


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

    @classmethod
    def from_config(cls, cfg: Config) -> "AuthSettings":
        """Build from :mod:`src.config` (unknown local roles are ignored)."""
        roles = frozenset(
            Role(value)
            for value in _split(cfg.LOCAL_DEV_USER_ROLES)
            if value in {r.value for r in Role}
        )
        return cls(
            mode=cfg.AUTH_MODE or "",
            app_base_url=cfg.APP_BASE_URL.rstrip("/"),
            local_user=LocalUser(
                display_name=cfg.LOCAL_DEV_USER_NAME or None,
                email=cfg.LOCAL_DEV_USER_EMAIL or None,
                roles=roles,
                group_ids=frozenset(_split(cfg.LOCAL_DEV_USER_GROUPS)),
            ),
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


class AuthManager:
    """Login, session check, refresh and logout for one AUTH_MODE."""

    def __init__(
        self,
        *,
        settings: AuthSettings | None = None,
        session_store: SessionStore | None = None,
        oidc_service: "OidcService | None" = None,
        cipher: TokenCipher | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Create the manager; missing dependencies come from ``service_factory``."""
        from src.services.service_factory import service_factory

        self._settings = settings or AuthSettings.from_config(config)
        self._store: SessionStore = session_store or service_factory.get_session_store()
        self._clock = clock
        self._oidc: "OidcService | None" = None
        self._cipher: TokenCipher | None = None
        if self._settings.mode == AuthMethod.OIDC.value:
            self._oidc = oidc_service or service_factory.get_oidc_service()
            self._cipher = cipher or TokenCipher(config.TOKEN_ENCRYPTION_KEYS or "")
            self._oidc.add_circuit_open_listener(
                lambda: log_event("auth.circuit_opened", reason="circuit_open")
            )
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
            AuthConfigError: AUTH_MODE unset / unknown / ``admin_api``,
                ``local`` inside ECS, or a missing / malformed ``oidc`` setting.
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
        missing = [name for name in _OIDC_REQUIRED_SETTINGS if not getattr(cfg, name)]
        if missing:
            logger.error("AUTH_MODE=oidc requires settings: %s", ", ".join(missing))
            raise AuthConfigError(f"missing oidc settings: {', '.join(missing)}")
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
        now = self._clock()
        session_id = secrets.token_urlsafe(32)
        session = self._session_from_tokens(_hash_session_id(session_id), tokens, now)
        self._remove_previous(previous_session_id)
        self._put(session)
        log_event("auth.login_succeeded", sub=session.user_sub, auth_method=self.mode)
        return LoginResult(
            session_id=session_id,
            next=self.sanitize_next(str(transaction.get("next") or "/")),
            principal=session.to_principal(),
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
        """End the session here and at Cognito; return where to redirect (BR1.13)."""
        if session_id:
            self._end_session(_hash_session_id(session_id))
        if self._oidc is None:
            return SIGNED_OUT_PATH
        return self._oidc.logout_url(f"{self._settings.app_base_url}/login")

    def _end_session(self, session_hash: str) -> None:
        try:
            session = self._store.get(session_hash)
        except SessionStoreError:
            logger.warning("could not read the session at logout", exc_info=True)
            session = None
        if session is not None and session.refresh_token_enc and self._oidc:
            self._revoke(session)
        self._delete_quietly(session_hash)
        log_event(
            "auth.logout",
            sub=session.user_sub if session else None,
            auth_method=self.mode,
        )

    def _revoke(self, session: LoginSession) -> None:
        assert self._oidc is not None and self._cipher is not None
        try:
            self._oidc.revoke(self._cipher.decrypt(session.refresh_token_enc or ""))
        except (OidcError, TokenDecryptError):
            logger.warning("refresh token revocation failed", exc_info=True)
            log_event(
                "auth.logout",
                sub=session.user_sub,
                auth_method=self.mode,
                reason="revoke_failed",
            )

    # --- refresh -----------------------------------------------------------------

    def _needs_refresh(self, session: LoginSession, now: datetime) -> bool:
        if session.auth_method is AuthMethod.LOCAL or self._oidc is None:
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

    def _refresh_now(self, session: LoginSession, now: datetime) -> LoginSession:
        assert self._oidc is not None and self._cipher is not None
        try:
            refresh_token = self._cipher.decrypt(session.refresh_token_enc or "")
        except TokenDecryptError as e:
            self._delete_quietly(session.session_id_hash)
            log_event(
                "auth.decrypt_failed", sub=session.user_sub, auth_method=self.mode
            )
            raise AuthRequiredError("stored token could not be decrypted") from e
        try:
            tokens = self._oidc.refresh(refresh_token)
        except OidcError as e:
            if e.code.kind is ErrorKind.TRANSIENT:
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
