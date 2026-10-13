"""The department list, cached per task (ADR-009, contract C4).

Departments are the Cognito ``group:`` groups. Each task keeps one copy of the
list and refreshes it at most every five minutes, so requests do not call
Cognito (NFR9.14). The list is never used to decide who may read what; that
uses the user's own ``group_ids`` (BR2.11).

Refresh rules (BR2.11, NFR9.17, NFR9.19, NFR Design R-02):

- Younger than :data:`REFRESH_INTERVAL_SECONDS`: served as is.
- Older: one request refreshes it. The refresher is whoever takes the
  non-blocking lock; everyone else does not wait for it and gets the previous
  list -- or 503 when that list is older than :data:`MAX_STALENESS_SECONDS`.
- At start-up there is no previous list: other requests wait for the first
  refresh for at most :data:`INITIAL_WAIT_SECONDS`, then answer 503.
- A failed refresh serves the previous list up to one hour old (503 after
  that) and Cognito is not asked again for :data:`FAILURE_BACKOFF_SECONDS`.

The snapshot (list and fetch time) is replaced as one value, so readers never
see a list with another list's time. Failures and 503s write
``authz.departments_stale`` (``age_seconds``, ``served``) (NFR8.8).
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable, NoReturn, Protocol

from ...models.authz import Department
from ...models.errors import CodedError, LookupUnavailableError
from ..shared.structured_log import log_event

#: A list younger than this is served without asking Cognito.
REFRESH_INTERVAL_SECONDS = 300
#: After a failed refresh, Cognito is not asked again for this long.
FAILURE_BACKOFF_SECONDS = 60
#: The previous list is served for at most this long after it was fetched.
MAX_STALENESS_SECONDS = 3600
#: How long a request waits for the very first list at start-up.
INITIAL_WAIT_SECONDS = 5


class DepartmentSource(Protocol):
    """Where the departments come from (``CognitoGroupsService``)."""

    def list_departments(self) -> list[Department]: ...


@dataclass(frozen=True)
class _Snapshot:
    departments: tuple[Department, ...]
    fetched_at: float


class DepartmentDirectory:
    """Per-task cache of the department list."""

    def __init__(
        self,
        groups_service: DepartmentSource,
        *,
        clock: Callable[[], float] = time.monotonic,
        initial_wait_seconds: float = INITIAL_WAIT_SECONDS,
    ) -> None:
        """Create the directory.

        Args:
            groups_service: Reads the department groups (injected).
            clock: Monotonic clock for ages and the back-off.
            initial_wait_seconds: Start-up wait for the first list.
        """
        self._groups = groups_service
        self._clock = clock
        self._initial_wait = initial_wait_seconds
        self._snapshot: _Snapshot | None = None
        self._failed_at: float | None = None
        self._refresh_lock = threading.Lock()
        self._refreshed = threading.Event()

    # --- queries (contract C4) ---------------------------------------------------

    def list_all(self) -> list[Department]:
        """Every department, ordered by display name.

        Raises:
            LookupUnavailableError: No list younger than one hour is available.
        """
        return sorted(self._departments(), key=lambda d: (d.display_name, d.group_id))

    def get_many(self, group_ids: frozenset[str]) -> dict[str, Department]:
        """The departments among ``group_ids``; unknown IDs are left out."""
        return {d.group_id: d for d in self._departments() if d.group_id in group_ids}

    def exists(self, group_id: str) -> bool:
        """Whether ``group_id`` is a department now."""
        return any(d.group_id == group_id for d in self._departments())

    # --- cache -------------------------------------------------------------------

    def _departments(self) -> tuple[Department, ...]:
        now = self._clock()
        snapshot = self._snapshot
        if (
            snapshot is not None
            and now - snapshot.fetched_at < REFRESH_INTERVAL_SECONDS
        ):
            return snapshot.departments
        if self._in_backoff(now):
            return self._previous_or_fail(snapshot, now, failed=False)
        if self._refresh_lock.acquire(blocking=False):
            try:
                return self._refresh(snapshot)
            finally:
                self._refresh_lock.release()
        return self._while_refreshing(snapshot, now)

    def _in_backoff(self, now: float) -> bool:
        failed_at = self._failed_at
        return failed_at is not None and now - failed_at < FAILURE_BACKOFF_SECONDS

    def _refresh(self, previous: _Snapshot | None) -> tuple[Department, ...]:
        done = self._refreshed
        try:
            departments = self._groups.list_departments()
        except CodedError:
            now = self._clock()
            self._failed_at = now
            return self._previous_or_fail(previous, now, failed=True)
        else:
            snapshot = _Snapshot(tuple(departments), self._clock())
            self._snapshot = snapshot
            self._failed_at = None
            return snapshot.departments
        finally:
            # Waiters hold ``done``; later requests get a fresh event.
            self._refreshed = threading.Event()
            done.set()

    def _while_refreshing(
        self, snapshot: _Snapshot | None, now: float
    ) -> tuple[Department, ...]:
        if snapshot is not None:
            return self._previous_or_fail(snapshot, now, failed=False)
        event = self._refreshed
        # The first refresh may have finished between the lock and here.
        if self._snapshot is None and not self._in_backoff(self._clock()):
            event.wait(self._initial_wait)
        current = self._snapshot
        if current is None:
            self._unavailable(None, self._clock())
        return current.departments

    def _previous_or_fail(
        self, previous: _Snapshot | None, now: float, *, failed: bool
    ) -> tuple[Department, ...]:
        if previous is not None and now - previous.fetched_at <= MAX_STALENESS_SECONDS:
            if failed:
                log_event(
                    "authz.departments_stale",
                    age_seconds=round(now - previous.fetched_at, 1),
                    served="stale",
                )
            return previous.departments
        self._unavailable(previous, now)

    def _unavailable(self, previous: _Snapshot | None, now: float) -> NoReturn:
        log_event(
            "authz.departments_stale",
            age_seconds=(
                round(now - previous.fetched_at, 1) if previous is not None else None
            ),
            served="unavailable",
        )
        raise LookupUnavailableError("no department list younger than one hour")
