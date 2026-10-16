"""The per-process cache of survey result CSVs (U7, NFR2.22, BR2.5).

A pure helper with no decision in it, so it lives in ``shared/``: both
``SurveyAnalysisManager`` (reads) and ``SurveyExecutionManager`` (evicts on
delete) use the one instance of the process (:func:`shared_result_cache`).
Callers only touch it after the authorization check (BR2.4, BR2.5); the key is
the survey ID and the bytes are the same for every reader.

Limits (``performance-design.md`` §2): 20 entries, 256MB in total, 32MB per
entry (larger CSVs are not kept), 300 seconds. When a limit is reached the
least recently used entry goes first. One lock guards every operation; two
threads may still read the same CSV from S3 at once (no single-flight), which
is correct, only slower.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

#: Most entries kept.
MAX_ENTRIES = 20
#: Most bytes kept in total.
MAX_TOTAL_BYTES = 256 * 1024 * 1024
#: Largest CSV kept.
MAX_ENTRY_BYTES = 32 * 1024 * 1024
#: How long an entry is served.
TTL_SECONDS = 300


@dataclass(frozen=True)
class _Entry:
    data: bytes
    stored_at: float


class ResultCsvCache:
    """A bounded, thread-safe LRU of result CSVs keyed by survey ID."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_ENTRIES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        max_entry_bytes: int = MAX_ENTRY_BYTES,
        ttl_seconds: float = TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a cache (tests pass smaller limits and a clock)."""
        self._max_entries = max_entries
        self._max_total = max_total_bytes
        self._max_entry = max_entry_bytes
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._total = 0
        self._lock = threading.Lock()

    def get(self, survey_id: str) -> bytes | None:
        """The CSV of ``survey_id`` when kept and fresh; it becomes most recent."""
        with self._lock:
            entry = self._entries.get(survey_id)
            if entry is None:
                return None
            if self._clock() - entry.stored_at >= self._ttl:
                self._drop(survey_id)
                return None
            self._entries.move_to_end(survey_id)
            return entry.data

    def put(self, survey_id: str, data: bytes) -> None:
        """Keep ``data`` unless it is over the single-entry limit."""
        with self._lock:
            if survey_id in self._entries:
                self._drop(survey_id)
            if len(data) > self._max_entry:
                return
            self._entries[survey_id] = _Entry(data, self._clock())
            self._total += len(data)
            while self._entries and (
                len(self._entries) > self._max_entries or self._total > self._max_total
            ):
                oldest = next(iter(self._entries))
                self._drop(oldest)

    def evict(self, survey_id: str) -> None:
        """Forget ``survey_id`` (its survey was deleted, BR1.8)."""
        with self._lock:
            if survey_id in self._entries:
                self._drop(survey_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def total_bytes(self) -> int:
        """Bytes kept now."""
        with self._lock:
            return self._total

    def _drop(self, survey_id: str) -> None:
        entry = self._entries.pop(survey_id)
        self._total -= len(entry.data)


_cache: ResultCsvCache | None = None
_cache_lock = threading.Lock()


def shared_result_cache() -> ResultCsvCache:
    """The one cache of this process (built on first use)."""
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = ResultCsvCache()
    return _cache


def reset_result_cache() -> None:
    """Forget the cache (tests isolate their state with this)."""
    global _cache
    with _cache_lock:
        _cache = None
