"""
Per-task circuit breaker in front of Cognito (U1 ``reliability-design.md``,
U8 NFR Design Q1 A, NFR9.44).

Moved out of ``oidc_service.py`` so that :class:`OidcService` and
:class:`CognitoAuthService` share it. It raises the neutral
:class:`CircuitOpenError` instead of a coded error; each Service turns it into
its own code (``OIDC_UNAVAILABLE`` / ``LOGIN_UNAVAILABLE``). This module imports
no other Service.
"""

import time
from threading import Lock
from typing import Callable

#: Consecutive transient failures that open the breaker.
FAILURE_THRESHOLD = 5
#: Seconds the breaker stays open before one trial call is allowed.
RESET_TIMEOUT_SECONDS = 60.0


class CircuitOpenError(Exception):
    """The breaker refused the call (open, or a half-open trial is in flight).

    Deliberately not a ``CodedError``: the calling Service chooses the code.
    """


class CircuitBreaker:
    """Per-task breaker in front of a Cognito endpoint.

    Closed: calls go through. Five consecutive transient failures open it for
    60 seconds, during which calls fail immediately. After that one trial call
    is allowed (half-open); its success closes the breaker, its failure opens
    it again. Other calls during the trial fail immediately. State is local to
    the process and resets on restart (NFR9.43).
    """

    def __init__(
        self,
        *,
        failure_threshold: int = FAILURE_THRESHOLD,
        reset_timeout: float = RESET_TIMEOUT_SECONDS,
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
            CircuitOpenError: While open or while a half-open trial is in flight.
        """
        with self._lock:
            if self._opened_at is None:
                return
            if self._trial_in_flight:
                raise CircuitOpenError("circuit half-open; trial call in flight")
            if self._clock() - self._opened_at < self._reset_timeout:
                raise CircuitOpenError("circuit open")
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
