"""The owner-name copy, cached per task (BR5.1〜BR5.3, U4).

Owner columns of the lists show the owner's name. Each task keeps one copy of
the user list (``UserDirectoryService``) and refreshes it in the background
(``performance-design.md`` §3), the same way ``DepartmentDirectory`` keeps the
departments:

- Younger than :data:`REFRESH_INTERVAL_SECONDS`: served as is.
- Older: the request that notices it starts one refresh on another thread and
  answers from the previous copy at once; no request waits for Cognito.
- No copy yet (start-up): the request answers "unavailable" (``-``).
- A failed refresh keeps the previous copy for up to
  :data:`MAX_STALENESS_SECONDS` and Cognito is not asked again for
  :data:`FAILURE_BACKOFF_SECONDS`. Failures write ``authz.owners_stale``
  (``age_seconds``, ``served``) (NFR8.13).

The copy is for display only (BR5.3): this class has no decision port and
nothing here is used to decide who may read what.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

from ...models.errors import CodedError
from ...models.sharing import OwnerSummary
from ..shared.structured_log import log_event

logger = logging.getLogger(__name__)

#: A copy younger than this is served without asking Cognito.
REFRESH_INTERVAL_SECONDS = 300
#: After a failed refresh, Cognito is not asked again for this long.
FAILURE_BACKOFF_SECONDS = 60
#: The previous copy is served for at most this long after it was fetched.
MAX_STALENESS_SECONDS = 3600
#: Label of an owner that is unknown, disabled, nameless or missing (BR5.2).
OWNER_UNKNOWN = "所有者不明"


class UserSource(Protocol):
    """Where the users come from (``UserDirectoryService``)."""

    def list_users(self) -> list[OwnerSummary]: ...


def _start_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="owner-directory-refresh", daemon=True).start()


@dataclass(frozen=True)
class _Snapshot:
    users: dict[str, OwnerSummary]
    fetched_at: float


class OwnerDirectory:
    """Per-task copy of ``sub -> owner name`` (display only)."""

    def __init__(
        self,
        users: UserSource,
        *,
        clock: Callable[[], float] = time.monotonic,
        run_in_background: Callable[[Callable[[], None]], None] = _start_thread,
    ) -> None:
        """Create the directory.

        Args:
            users: Reads the user list (injected).
            clock: Monotonic clock for ages and the back-off.
            run_in_background: Runs one refresh job; a daemon thread by
                default (tests run it synchronously or later).
        """
        self._users = users
        self._clock = clock
        self._run_in_background = run_in_background
        self._snapshot: _Snapshot | None = None
        self._failed_at: float | None = None
        self._refreshing = False
        self._lock = threading.Lock()

    # --- queries -------------------------------------------------------------------

    def labels(self, subs: Iterable[str]) -> dict[str, str] | None:
        """Owner labels of ``subs`` (BR5.2).

        Returns:
            ``sub -> name`` (``所有者不明`` for unknown, disabled or nameless
            users), or ``None`` when no copy younger than one hour exists
            (the owner column then shows ``-``).
        """
        self._refresh_if_due()
        snapshot = self._usable_snapshot()
        if snapshot is None:
            return None
        return {sub: _label_of(snapshot.users.get(sub)) for sub in subs}

    def label(self, sub: str | None) -> str | None:
        """The label of one owner; ``所有者不明`` when ``sub`` is missing."""
        if not sub:
            return OWNER_UNKNOWN
        found = self.labels([sub])
        return None if found is None else found[sub]

    # --- copy ----------------------------------------------------------------------

    def _usable_snapshot(self) -> _Snapshot | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        if self._clock() - snapshot.fetched_at > MAX_STALENESS_SECONDS:
            return None
        return snapshot

    def _refresh_if_due(self) -> None:
        now = self._clock()
        with self._lock:
            snapshot = self._snapshot
            if (
                snapshot is not None
                and now - snapshot.fetched_at < REFRESH_INTERVAL_SECONDS
            ):
                return
            failed_at = self._failed_at
            if failed_at is not None and now - failed_at < FAILURE_BACKOFF_SECONDS:
                return
            if self._refreshing:
                return
            self._refreshing = True
        try:
            self._run_in_background(self._refresh)
        except Exception:
            # The refresh never started: clear the flag (otherwise no refresh
            # would ever run again) and treat it as a failed refresh so the
            # back-off and ``authz.owners_stale`` apply (R-03).
            logger.exception("owner copy refresh could not start")
            self._record_failure()

    def _refresh(self) -> None:
        try:
            users = self._users.list_users()
        except CodedError:
            self._record_failure()
        except Exception:
            logger.exception("owner copy refresh failed unexpectedly")
            self._record_failure()
        else:
            snapshot = _Snapshot({u.sub: u for u in users}, self._clock())
            with self._lock:
                self._snapshot = snapshot
                self._failed_at = None
                self._refreshing = False

    def _record_failure(self) -> None:
        now = self._clock()
        with self._lock:
            self._failed_at = now
            self._refreshing = False
            previous = self._snapshot
        age = None if previous is None else round(now - previous.fetched_at, 1)
        served = (
            "stale"
            if age is not None and age <= MAX_STALENESS_SECONDS
            else "unavailable"
        )
        log_event("authz.owners_stale", age_seconds=age, served=served)


def _label_of(user: OwnerSummary | None) -> str:
    if user is None or not user.enabled or not user.display_name:
        return OWNER_UNKNOWN
    return user.display_name
