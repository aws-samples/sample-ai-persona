"""
Cognito OIDC communication (AUTH_MODE=oidc) and the per-task circuit breaker.

The OAuth 2.0 / OIDC protocol work is delegated to libraries instead of being
hand-written (NFR4.5): Authlib's ``OAuth2Client`` builds the authorization URL
with PKCE (S256), exchanges the code, refreshes and revokes tokens; joserfc
(Authlib's JOSE successor; ``authlib.jose`` is deprecated in Authlib 1.8)
verifies the ID token signature and ``iss`` / ``aud`` / ``exp`` / ``nonce``.

Endpoints are the fixed Cognito URLs derived from the user pool and the
Managed Login domain, so no discovery document is fetched. The JWKS is fetched
once and kept in the process; an unknown ``kid`` triggers one re-fetch.

Failure classification (``reliability-design.md``):

- transient: timeout, connection failure, HTTP 5xx, HTTP 429, JWKS fetch
  failure, or the breaker being open -> ``OIDC_UNAVAILABLE``;
- permanent: Cognito answered 4xx (``invalid_grant`` and similar) ->
  ``OIDC_GRANT_INVALID``; not counted by the breaker because Cognito is up;
- invalid ID token -> ``OIDC_TOKEN_INVALID``.

Every call waits at most 5 seconds and is never retried here: the caller
decides (the user retries the login; a refresh is retried on the next
request). This module never receives the HTTP request object and never puts
tokens, codes or Cognito's error descriptions into exception messages.
"""

import logging
import secrets
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

# Authlib ships no type information; the three imports below are untyped.
from authlib.common.security import generate_token  # type: ignore[import-untyped]
from authlib.integrations.base_client.errors import (  # type: ignore[import-untyped]
    OAuthError,
)
from authlib.integrations.httpx_client import (  # type: ignore[import-untyped]
    OAuth2Client,
)
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from src.models.errors import CodedError, ErrorCode

logger = logging.getLogger(__name__)

SCOPE = "openid email profile"
DEFAULT_TIMEOUT_SECONDS = 5.0
_ID_TOKEN_ALGORITHMS = ["RS256"]


class OidcError(CodedError):
    """A Cognito call failed. ``code`` tells transient from permanent."""

    code = ErrorCode.OIDC_UNAVAILABLE


class _TransientStatusError(Exception):
    """Raised from the response hook for 5xx / 429 before Authlib parses it."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Cognito answered HTTP {status_code}")
        self.status_code = status_code


def _raise_for_transient_status(response: httpx.Response) -> None:
    if response.status_code == 429 or response.status_code >= 500:
        raise _TransientStatusError(response.status_code)


@dataclass(frozen=True)
class AuthorizationRequest:
    """Where to send the browser, plus the values to keep until the callback."""

    url: str
    state: str
    nonce: str
    code_verifier: str


@dataclass(frozen=True)
class TokenSet:
    """Tokens from Cognito and the verified ID token claims.

    The ID token itself is not kept (only its claims are returned).
    """

    access_token: str
    refresh_token: str | None
    expires_in: int
    claims: dict[str, Any]


class CircuitBreaker:
    """Per-task breaker in front of the Cognito token endpoint.

    Closed: calls go through. Five consecutive transient failures open it for
    60 seconds, during which calls fail immediately. After that one trial call
    is allowed (half-open); its success closes the breaker, its failure opens
    it again. Other calls during the trial fail immediately. State is local to
    the process and resets on restart.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._clock = clock
        self._lock = Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False
        self._listeners: list[Callable[[], None]] = []

    def add_open_listener(self, listener: Callable[[], None]) -> None:
        """Call ``listener`` every time the breaker opens (for logging)."""
        self._listeners.append(listener)

    def before_call(self) -> None:
        """Admit or refuse a call.

        Raises:
            OidcError: ``OIDC_UNAVAILABLE`` while open or while a half-open
                trial is in flight.
        """
        with self._lock:
            if self._opened_at is None:
                return
            if self._trial_in_flight:
                raise OidcError("circuit half-open; trial call in flight")
            if self._clock() - self._opened_at < self._reset_timeout:
                raise OidcError("circuit open")
            self._trial_in_flight = True

    def record_success(self) -> None:
        """Close the breaker and reset the failure count."""
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._trial_in_flight = False

    def record_failure(self) -> None:
        """Count a transient failure; open (or reopen) when due."""
        with self._lock:
            reopen = self._trial_in_flight
            self._trial_in_flight = False
            self._failures += 1
            if not (reopen or self._failures >= self._threshold):
                return
            already_open = self._opened_at is not None and not reopen
            self._opened_at = self._clock()
            listeners = [] if already_open else list(self._listeners)
        for listener in listeners:
            listener()


class OidcService:
    """Cognito Managed Login client for one app client (confidential)."""

    def __init__(
        self,
        *,
        user_pool_id: str,
        client_id: str,
        client_secret: str,
        domain: str,
        redirect_uri: str,
        transport: httpx.BaseTransport | None = None,
        breaker: CircuitBreaker | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the client.

        Args:
            user_pool_id: ``<region>_<id>``; the region part selects the issuer.
            client_id: App client id (also the ID token audience).
            client_secret: App client secret (from Secrets Manager via ECS).
            domain: Managed Login domain host, without scheme.
            redirect_uri: ``APP_BASE_URL + /auth/callback``.
            transport: httpx transport (tests inject ``MockTransport``).
            breaker: Circuit breaker; a fresh one when omitted.
            timeout: Upper bound for every Cognito call, in seconds.
            wall_clock: Epoch-seconds clock used for ``exp`` checks.
        """
        region = user_pool_id.split("_", 1)[0]
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        self._client_id = client_id
        self._client_secret = client_secret
        self._base = f"https://{domain}"
        self._redirect_uri = redirect_uri
        self._transport = transport or httpx.HTTPTransport()
        self.breaker = breaker or CircuitBreaker()
        self.timeout_seconds = timeout
        self._wall_clock = wall_clock
        self._jwks: KeySet | None = None
        self._jwks_lock = Lock()

    # --- client construction -------------------------------------------------

    def _oauth_client(self) -> OAuth2Client:
        """A short-lived Authlib client sharing the pooled transport.

        Authlib keeps the last token on the client instance, so one instance
        per call keeps concurrent requests from sharing that state.
        """
        return OAuth2Client(
            client_id=self._client_id,
            client_secret=self._client_secret,
            token_endpoint_auth_method="client_secret_basic",
            revocation_endpoint_auth_method="client_secret_basic",
            scope=SCOPE,
            redirect_uri=self._redirect_uri,
            code_challenge_method="S256",
            transport=self._transport,
            timeout=self.timeout_seconds,
            event_hooks={"response": [_raise_for_transient_status]},
        )

    # --- public API ------------------------------------------------------------

    def create_authorization_request(self) -> AuthorizationRequest:
        """Build the Managed Login URL with state, nonce and PKCE (S256)."""
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = generate_token(64)
        with self._oauth_client() as client:
            url, _ = client.create_authorization_url(
                f"{self._base}/oauth2/authorize",
                state=state,
                code_verifier=code_verifier,
                nonce=nonce,
            )
        return AuthorizationRequest(
            url=url, state=state, nonce=nonce, code_verifier=code_verifier
        )

    def exchange_code(self, code: str, code_verifier: str, *, nonce: str) -> TokenSet:
        """Exchange the authorization code and verify the ID token.

        Raises:
            OidcError: ``OIDC_UNAVAILABLE`` / ``OIDC_GRANT_INVALID`` /
                ``OIDC_TOKEN_INVALID``.
        """

        def call(client: OAuth2Client) -> dict[str, Any]:
            token: dict[str, Any] = client.fetch_token(
                f"{self._base}/oauth2/token",
                grant_type="authorization_code",
                code=code,
                code_verifier=code_verifier,
            )
            return token

        token = self._guarded("exchange", call)
        claims = self._verify_id_token(token.get("id_token"), nonce=nonce)
        return self._token_set(token, claims, previous_refresh_token=None)

    def refresh(self, refresh_token: str) -> TokenSet:
        """Get new tokens with the refresh token and verify the new ID token.

        Raises:
            OidcError: ``OIDC_UNAVAILABLE`` (retry later) or
                ``OIDC_GRANT_INVALID`` (the session must end).
        """

        def call(client: OAuth2Client) -> dict[str, Any]:
            token: dict[str, Any] = client.refresh_token(
                f"{self._base}/oauth2/token", refresh_token=refresh_token
            )
            return token

        token = self._guarded("refresh", call)
        claims = self._verify_id_token(token.get("id_token"), nonce=None)
        return self._token_set(token, claims, previous_refresh_token=refresh_token)

    def revoke(self, refresh_token: str) -> None:
        """Revoke the refresh token (logout). Not behind the breaker.

        Raises:
            OidcError: The revocation did not succeed.
        """
        try:
            with self._oauth_client() as client:
                response = client.revoke_token(
                    f"{self._base}/oauth2/revoke",
                    token=refresh_token,
                    token_type_hint="refresh_token",
                )
        except (_TransientStatusError, httpx.HTTPError) as e:
            raise OidcError(f"revoke failed: {type(e).__name__}") from e
        if response.status_code != 200:
            raise OidcError(
                f"revoke answered HTTP {response.status_code}",
                code=ErrorCode.OIDC_GRANT_INVALID,
            )

    def add_circuit_open_listener(self, listener: Callable[[], None]) -> None:
        """Be told when the circuit breaker opens (the Manager logs it)."""
        self.breaker.add_open_listener(listener)

    def logout_url(self, logout_uri: str) -> str:
        """Managed Login logout URL that returns to ``logout_uri``."""
        query = urlencode({"client_id": self._client_id, "logout_uri": logout_uri})
        return f"{self._base}/logout?{query}"

    # --- internals ---------------------------------------------------------------

    def _guarded(
        self, operation: str, call: Callable[[OAuth2Client], dict[str, Any]]
    ) -> dict[str, Any]:
        """Run a token-endpoint call behind the breaker and classify failures."""
        self.breaker.before_call()
        try:
            with self._oauth_client() as client:
                token = call(client)
        except OAuthError as e:
            # Cognito is up and said no: permanent, not counted.
            self.breaker.record_success()
            raise OidcError(
                f"{operation} rejected by Cognito: {e.error}",
                code=ErrorCode.OIDC_GRANT_INVALID,
            ) from e
        except (_TransientStatusError, httpx.HTTPError, ValueError) as e:
            self.breaker.record_failure()
            raise OidcError(f"{operation} failed: {type(e).__name__}") from e
        self.breaker.record_success()
        return token

    def _token_set(
        self,
        token: dict[str, Any],
        claims: dict[str, Any],
        *,
        previous_refresh_token: str | None,
    ) -> TokenSet:
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OidcError(
                "token response has no access_token",
                code=ErrorCode.OIDC_TOKEN_INVALID,
            )
        refresh_token = token.get("refresh_token") or previous_refresh_token
        return TokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=int(token.get("expires_in") or 0),
            claims=claims,
        )

    def _key_set(self, *, refresh: bool) -> KeySet:
        """Return the cached JWKS, fetching it when missing or asked to."""
        with self._jwks_lock:
            if self._jwks is not None and not refresh:
                return self._jwks
        self.breaker.before_call()
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self.timeout_seconds,
                event_hooks={"response": [_raise_for_transient_status]},
            ) as client:
                response = client.get(f"{self.issuer}/.well-known/jwks.json")
                response.raise_for_status()
                key_set = KeySet.import_key_set(response.json())
        except (_TransientStatusError, httpx.HTTPError, ValueError, JoseError) as e:
            self.breaker.record_failure()
            raise OidcError(f"JWKS fetch failed: {type(e).__name__}") from e
        self.breaker.record_success()
        with self._jwks_lock:
            self._jwks = key_set
        return key_set

    def _verify_id_token(self, id_token: Any, *, nonce: str | None) -> dict[str, Any]:
        """Verify signature, issuer, audience, expiry, token use and nonce."""
        if not isinstance(id_token, str) or not id_token:
            raise OidcError(
                "token response has no id_token", code=ErrorCode.OIDC_TOKEN_INVALID
            )
        decoded = None
        for refresh in (False, True):
            key_set = self._key_set(refresh=refresh)
            try:
                decoded = jwt.decode(id_token, key_set, algorithms=_ID_TOKEN_ALGORITHMS)
                break
            except JoseError as e:
                if refresh:
                    raise OidcError(
                        f"ID token signature rejected: {type(e).__name__}",
                        code=ErrorCode.OIDC_TOKEN_INVALID,
                    ) from e
        assert decoded is not None
        options: dict[str, Any] = {
            "iss": {"essential": True, "value": self.issuer},
            "aud": {"essential": True, "value": self._client_id},
            "exp": {"essential": True},
            "sub": {"essential": True},
            "token_use": {"essential": True, "value": "id"},
        }
        if nonce is not None:
            options["nonce"] = {"essential": True, "value": nonce}
        registry = jwt.JWTClaimsRegistry(now=int(self._wall_clock()), **options)
        try:
            registry.validate(decoded.claims)
        except JoseError as e:
            raise OidcError(
                f"ID token claims rejected: {type(e).__name__}",
                code=ErrorCode.OIDC_TOKEN_INVALID,
            ) from e
        return dict(decoded.claims)
