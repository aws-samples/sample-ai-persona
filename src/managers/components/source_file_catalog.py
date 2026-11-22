"""The owner of uploaded source files (advanced generation U1/U2, contract C4).

``SourceUploadManager`` creates, inspects, lists and deletes the files,
``AdvancedPersonaGenerationManager`` reads them for a generation and releases
their in-use marks, and ``PersonaManager`` asks whether the file of an
analysis record is still there. Managers cannot import each other, so the
rules on files live in this component (``.claude/rules/architecture.md``,
Component layer):

- Only the owner sees a file; someone else's file, a missing file and an
  expired file get the same NOT_FOUND code (NFR3.9, BR13.5, BR15.1).
- Whether a file is still alive is decided in one place, :meth:`_is_live`
  (U2 NFR5.29): not past ``expires_at``. A U1 record without ``expires_at``
  counts from its last use or its creation (``ADVANCED_SOURCE_RETENTION_DAYS``).
- A generation uses only the owner's live ``ready`` table (BR6.2, BR13.1).
- A user holds at most ``ADVANCED_UPLOAD_MAX_FILES`` live files: uploading
  ones younger than 24 hours, inspecting and ready ones (BR2.1, BR12.3). The
  count reads the eventually consistent GSI, so two starts at the same moment
  may both pass (accepted in U1, scalability-design.md §2).
- An inspection stuck past 15 minutes may start again (NFR5.19) and may be
  deleted (BR14.1).
- Deletion goes ``deleting`` -> every S3 version -> the record (BR14.1); a
  file in use is refused with ``SOURCE_FILE_IN_USE`` (BR14.2).
- Old U1 records get their ``expires_at`` written, or are deleted when past
  it, a few at a time while the list is read (NFR3.16).
- The in-use mark is set in the start transaction of the generation store and
  removed here by whoever ends the generation (BR6.4).

The ``SourceFileStore``, the ``S3Service`` and the caller's exception type are
given to the constructor; this component never reads ``service_factory``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Optional, Sequence, TypeVar

from ...config import config
from ...models.advanced_generation import SOURCE_FILES_MAX
from ...models.analysis import AnalysisRecord
from ...models.errors import CodedError, ErrorCode
from ...models.source_file import (
    DocumentMetadata,
    SourceAvailability,
    SourceAvailabilityState,
    SourceFile,
    SourceFileStatus,
    SourceKind,
    SourceMetadata,
)
from ...services.s3_service import S3ServiceError
from ...services.stores.source_file_store import (
    SourceFileStoreError,
    SourceFileUnavailable,
)
from ..shared.advanced_generation_log import log_advanced_event

if TYPE_CHECKING:
    from ...services.s3_service import S3Service
    from ...services.stores.source_file_store import SourceFileStore

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
#: U5 BR30.3: files that will be ready later (an error file never will).
_NOT_READY_YET = frozenset({SourceFileStatus.UPLOADING, SourceFileStatus.INSPECTING})
_HELD_STATES = frozenset(
    {SourceFileStatus.UPLOADING, SourceFileStatus.INSPECTING, SourceFileStatus.READY}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ReconcileOutcome:
    """What one pass over the old U1 records did (NFR3.16, never stored)."""

    filled: int = 0
    deleted: int = 0
    remaining: int = 0
    status: str = "done"
    error_code: Optional[str] = None


class SourceFileCatalog:
    """Reads and state changes of the user's source files."""

    def __init__(
        self,
        store: "SourceFileStore",
        *,
        s3: Optional["S3Service"] = None,
        error_type: type[CodedError] = CodedError,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        """
        Args:
            store: ``SourceFiles`` reads and conditional writes.
            s3: The upload bucket; needed only to delete files (U2).
            error_type: The caller's Manager exception; every refusal and
                every store failure is raised as this type.
            clock: Aware-UTC clock for the expiry checks (tests).
        """
        self._store = store
        self._s3 = s3
        self._error_type = error_type
        self._clock = clock or _utc_now

    # --- expiry (one place, NFR5.29) --------------------------------------------

    @staticmethod
    def _retention_days() -> int:
        return int(config.ADVANCED_SOURCE_RETENTION_DAYS)

    def _expired(self, source_file: SourceFile, now: datetime) -> bool:
        return source_file.effective_expires_at(self._retention_days()) <= now

    def _is_live(self, source_file: SourceFile, now: datetime) -> bool:
        """Not being deleted and not past its expiry (BR13.5)."""
        return source_file.is_live(now, self._retention_days())

    def new_expiry(self, now: Optional[datetime] = None) -> datetime:
        """``now`` + the retention: the expiry at creation and at each use (BR13.2)."""
        return (now or self._clock()) + timedelta(days=self._retention_days())

    def next_display_seq(self, owner_sub: str, filename: str) -> int:
        """The number of a new file of ``filename`` (highest live one + 1, BR11.3)."""
        now = self._clock()
        highest = self._call(
            lambda: self._store.max_display_seq(owner_sub, filename, now=now)
        )
        return int(highest) + 1

    # --- reads ------------------------------------------------------------------

    def get_owned(
        self, owner_sub: str, source_file_id: str, *, not_found_code: ErrorCode
    ) -> SourceFile:
        """The owner's unexpired file; anything else is ``not_found_code``.

        A ``deleting`` file is returned (its row says the deletion failed and
        can be retried, BR14.1); :meth:`ready_table` refuses it.
        """
        found = self._call(lambda: self._store.get_for_owner(source_file_id, owner_sub))
        if found is None or self._expired(found, self._clock()):
            raise self._error_type("source file not found", code=not_found_code)
        return found

    def ready_table(self, owner_sub: str, source_file_id: str) -> SourceFile:
        """The owner's live ready table for a generation (BR6.2, BR13.1, BR13.5).

        Raises:
            CodedError: ``GENERATION_SOURCE_NOT_FOUND`` when it is someone
                else's, missing, expired, not a table or not ready.
        """
        code = ErrorCode.GENERATION_SOURCE_NOT_FOUND
        found = self.get_owned(owner_sub, source_file_id, not_found_code=code)
        if (
            found.status is not SourceFileStatus.READY
            or found.kind is not SourceKind.TABLE
            or found.metadata is None
        ):
            raise self._error_type("source file is not a ready table", code=code)
        return found

    def ready_sources(
        self, owner_sub: str, source_file_ids: Sequence[str]
    ) -> list[SourceFile]:
        """The owner's live ready files for one generation (U4 BR23.1, NFR3.24).

        Documents alone (1〜10), or one table with documents; more than one
        table waits for U6.

        Raises:
            CodedError: ``UPLOAD_SOURCE_INVALID`` (nothing, more than ten, or
                two tables or more: ``reason=tables``),
                ``GENERATION_SOURCE_NOT_READY`` (U5: a file still uploading or
                being inspected), ``GENERATION_SOURCE_NOT_FOUND`` (any file
                someone else's, missing, expired, failed or being deleted).
        """
        ids = list(dict.fromkeys(i for i in source_file_ids if i))
        if not 1 <= len(ids) <= SOURCE_FILES_MAX:
            raise self._error_type(
                "a generation uses 1..10 files",
                code=ErrorCode.UPLOAD_SOURCE_INVALID,
                context={"reason": "count"},
            )
        code = ErrorCode.GENERATION_SOURCE_NOT_FOUND
        found = [self.get_owned(owner_sub, i, not_found_code=code) for i in ids]
        if any(s.status in _NOT_READY_YET for s in found):
            # U5 BR30.3: still uploading or being inspected; the user waits
            raise self._error_type(
                "source file is not ready yet",
                code=ErrorCode.GENERATION_SOURCE_NOT_READY,
            )
        for source in found:
            if source.status is not SourceFileStatus.READY or source.metadata is None:
                raise self._error_type("source file is not ready", code=code)
        if sum(1 for s in found if s.kind is SourceKind.TABLE) > 1:
            raise self._error_type(
                "one table per generation",
                code=ErrorCode.UPLOAD_SOURCE_INVALID,
                context={"reason": "tables"},
            )
        return found

    def list_owned(
        self, owner_sub: str, *, reconcile: bool = False
    ) -> list[SourceFile]:
        """The owner's files for the list, newest first (W2-1, BR13.1).

        Leaves out expired files and uploads abandoned for 24 hours; keeps
        ``deleting`` ones (shown as "削除できませんでした"). With ``reconcile``
        the same read also feeds :meth:`reconcile_legacy` (no extra read,
        performance-design.md NFR2.6); what it fills or deletes does not change
        what is shown, because expiry is judged the same way here.
        """
        now = self._clock()
        oldest = now - timedelta(seconds=config.ADVANCED_UPLOAD_PENDING_MAX_SECONDS)
        files = self._call(lambda: self._store.list_for_owner(owner_sub))
        if reconcile:
            self.reconcile_legacy(owner_sub, files)
        shown = [
            f
            for f in files
            if f.owner_sub == owner_sub
            and not self._expired(f, now)
            and not (f.status is SourceFileStatus.UPLOADING and f.created_at <= oldest)
        ]
        return sorted(shown, key=lambda f: f.created_at, reverse=True)

    def held_count(self, owner_sub: str, *, now: datetime) -> int:
        """Live files of the owner (abandoned uploads are not counted, NFR5.5)."""
        oldest = now - timedelta(seconds=config.ADVANCED_UPLOAD_PENDING_MAX_SECONDS)
        files = self._call(lambda: self._store.list_for_owner(owner_sub))
        return sum(
            1
            for f in files
            if f.status in _HELD_STATES
            and self._is_live(f, now)
            and not (f.status is SourceFileStatus.UPLOADING and f.created_at <= oldest)
        )

    def ensure_room(self, owner_sub: str, *, now: datetime) -> None:
        """Refuse a new upload when the owner already holds the maximum (BR2.1).

        Raises:
            CodedError: ``UPLOAD_TOO_MANY_FILES`` with ``max_files``.
        """
        limit = int(config.ADVANCED_UPLOAD_MAX_FILES)
        if self.held_count(owner_sub, now=now) >= limit:
            raise self._error_type(
                "too many source files",
                code=ErrorCode.UPLOAD_TOO_MANY_FILES,
                context={"max_files": limit},
            )

    def availability(
        self, owner_sub: str, record: AnalysisRecord
    ) -> tuple[SourceAvailability, ...]:
        """Whether each file of an analysis record is still there (BR14.3).

        ``available`` needs the owner's live file with the recorded checksum.
        A U1 record has no file IDs: one ``unknown`` per checksum. A failed
        read is ``unknown`` too (the record is still shown).
        """
        if not record.source_file_ids:
            return tuple(
                SourceAvailability("", "", SourceAvailabilityState.UNKNOWN)
                for _ in record.source_checksums or ("",)
            )
        now = self._clock()
        judged: list[SourceAvailability] = []
        for index, source_file_id in enumerate(record.source_file_ids):
            name = _at(record.source_display_names, index)
            checksum = _at(record.source_checksums, index)
            try:
                found = self._store.get_for_owner(source_file_id, owner_sub)
            except SourceFileStoreError as e:
                logger.warning("source file not readable (%s)", e.code.value)
                judged.append(
                    SourceAvailability(
                        source_file_id, name, SourceAvailabilityState.UNKNOWN
                    )
                )
                continue
            state = (
                SourceAvailabilityState.AVAILABLE
                if found is not None
                and self._is_live(found, now)
                and (found.checksum or "") == checksum
                else SourceAvailabilityState.DELETED
            )
            judged.append(SourceAvailability(source_file_id, name, state))
        return tuple(judged)

    # --- writes -----------------------------------------------------------------

    def create(self, source_file: SourceFile) -> None:
        """Write a new upload."""
        self._call(lambda: self._store.put_new(source_file))

    def record_url_refresh(
        self, owner_sub: str, source_file_id: str, *, now: datetime
    ) -> SourceFile:
        """Count one part URL refresh of the owner's uploading file (BR2.4).

        Raises:
            CodedError: ``UPLOAD_NOT_FOUND`` when someone else's, missing, no
                longer uploading, or out of refreshes.
        """
        return self._unavailable_as(
            ErrorCode.UPLOAD_NOT_FOUND,
            lambda: self._store.record_url_refresh(
                source_file_id,
                owner_sub,
                max_refreshes=int(config.ADVANCED_UPLOAD_URL_REFRESH_MAX),
                now=now,
            ),
        )

    def begin_inspection(
        self, owner_sub: str, source_file_id: str, *, now: datetime
    ) -> SourceFile:
        """``uploading -> inspecting`` once, or again after 15 minutes (NFR5.19).

        Raises:
            CodedError: ``UPLOAD_NOT_FOUND`` when someone else's, missing,
                older than 24 hours, already completed or being inspected.
        """
        return self._unavailable_as(
            ErrorCode.UPLOAD_NOT_FOUND,
            lambda: self._store.begin_inspection(
                source_file_id,
                owner_sub,
                now=now,
                stale_before=self._stale_before(now),
                created_after=now
                - timedelta(seconds=config.ADVANCED_UPLOAD_PENDING_MAX_SECONDS),
            ),
        )

    def inspection_in_progress(self, source_file: SourceFile, now: datetime) -> bool:
        """An ``inspecting`` file updated within the last 15 minutes (BR11.4)."""
        return (
            source_file.status is SourceFileStatus.INSPECTING
            and source_file.updated_at is not None
            and source_file.updated_at >= self._stale_before(now)
        )

    def mark_ready(
        self,
        source_file_id: str,
        metadata: SourceMetadata | DocumentMetadata,
        checksum: str,
        *,
        now: datetime,
    ) -> SourceFile:
        """Keep the inspection result and make the file ready (BR3.3).

        Raises:
            CodedError: ``UPLOAD_NOT_FOUND`` when the file is no longer
                inspecting.
        """
        return self._unavailable_as(
            ErrorCode.UPLOAD_NOT_FOUND,
            lambda: self._store.mark_ready(source_file_id, metadata, checksum, now=now),
        )

    def mark_error(
        self, source_file_id: str, error_code: ErrorCode, *, now: datetime
    ) -> bool:
        """Move the file to ``error`` with the code only (a failure is logged)."""
        try:
            return self._store.mark_error(source_file_id, error_code.value, now=now)
        except SourceFileStoreError as e:
            logger.warning("source file error state not written (%s)", e.code.value)
            return False

    def cancel_upload(self, owner_sub: str, source_file_id: str) -> bool:
        """Delete the owner's upload while it is still uploading (W2-6)."""
        return bool(
            self._call(lambda: self._store.delete_uploading(source_file_id, owner_sub))
        )

    def release(
        self, source_file_id: str, generation_id: str, *, now: datetime
    ) -> bool:
        """Remove the in-use mark of ``generation_id`` (BR6.4; never raises).

        ``now`` is kept for the callers; the end no longer stamps the last
        use (U2 BR13.3).
        """
        try:
            return self._store.clear_in_use(source_file_id, generation_id)
        except SourceFileStoreError as e:
            logger.warning("source file in-use mark not released (%s)", e.code.value)
            return False

    def fill_checksum(self, source_file_id: str, checksum: str) -> bool:
        """Write the checksum of a file whose record holds none (NFR3.19)."""
        try:
            return self._store.set_checksum_if_empty(source_file_id, checksum)
        except SourceFileStoreError as e:
            logger.warning("source file checksum not written (%s)", e.code.value)
            return False

    # --- deletion (BR14.1, BR14.2) ----------------------------------------------

    def delete_owned(self, owner_sub: str, source_file_id: str) -> int:
        """Delete the owner's file: ``deleting``, every S3 version, the record.

        A failed S3 deletion leaves the record ``deleting``; deleting it again
        continues from there. A record already gone counts as deleted.

        Returns:
            How many S3 versions and delete markers were removed.

        Raises:
            CodedError: ``UPLOAD_NOT_FOUND`` (someone else's, missing,
                expired, uploading, inspecting recently),
                ``SOURCE_FILE_IN_USE`` (a running generation holds it) or the
                S3 / store code (the record stays ``deleting``).
        """
        now = self._clock()
        self.get_owned(
            owner_sub, source_file_id, not_found_code=ErrorCode.UPLOAD_NOT_FOUND
        )
        deleting = self._begin_delete(owner_sub, source_file_id, now)
        return self._finish_delete(owner_sub, deleting)

    def _begin_delete(
        self, owner_sub: str, source_file_id: str, now: datetime
    ) -> SourceFile:
        try:
            return self._call(
                lambda: self._store.begin_delete(
                    source_file_id,
                    owner_sub,
                    now=now,
                    stale_before=self._stale_before(now),
                )
            )
        except SourceFileUnavailable:
            again = self._call(
                lambda: self._store.get_for_owner(source_file_id, owner_sub)
            )
            if again is not None and again.is_in_use(now):
                raise self._error_type(
                    "source file in use", code=ErrorCode.SOURCE_FILE_IN_USE
                ) from None
            raise self._error_type(
                "source file not deletable", code=ErrorCode.UPLOAD_NOT_FOUND
            ) from None

    def _finish_delete(self, owner_sub: str, deleting: SourceFile) -> int:
        if self._s3 is None:
            raise self._error_type(
                "no storage to delete from", code=ErrorCode.S3_OPERATION_FAILED
            )
        s3 = self._s3
        try:
            removed = s3.delete_all_versions(deleting.s3_key)
        except S3ServiceError as e:
            raise self._error_type(
                f"source file versions not deleted ({e.code.value})", code=e.code
            ) from e
        if not self._call(
            lambda: self._store.delete_record(deleting.source_file_id, owner_sub)
        ):
            logger.info("source file record already gone")
        return int(removed)

    # --- old U1 records (NFR3.16) -----------------------------------------------

    def reconcile_legacy(
        self, owner_sub: str, files: Sequence[SourceFile]
    ) -> ReconcileOutcome:
        """Give old records without ``expires_at`` one, or delete expired ones.

        Only the owner's records are touched, at most
        ``ADVANCED_SOURCE_RECONCILE_MAX_DELETES`` deletions and
        ``ADVANCED_SOURCE_RECONCILE_MAX_SECONDS`` per call; the rest waits for
        the next list. Never raises: a failure is logged with its code only.
        """
        legacy = [f for f in files if f.owner_sub == owner_sub and f.expires_at is None]
        if not legacy:
            return ReconcileOutcome()
        now = self._clock()
        started = time.monotonic()
        budget = float(config.ADVANCED_SOURCE_RECONCILE_MAX_SECONDS)
        max_deletes = int(config.ADVANCED_SOURCE_RECONCILE_MAX_DELETES)
        filled = deleted = done = 0
        error_code: Optional[str] = None
        for source_file in legacy:
            if time.monotonic() - started >= budget:
                break
            expiry = source_file.effective_expires_at(self._retention_days())
            removable = expiry <= now and not source_file.is_in_use(now)
            if removable and deleted >= max_deletes:
                break
            try:
                if removable and self._delete_legacy(owner_sub, source_file, now):
                    deleted += 1
                elif self._store.fill_expires_at(
                    source_file.source_file_id, owner_sub, expiry
                ):
                    filled += 1
            except CodedError as e:
                error_code = e.code.value
                logger.warning("old source file not reconciled (%s)", e.code.value)
            done += 1
        outcome = ReconcileOutcome(
            filled=filled,
            deleted=deleted,
            remaining=len(legacy) - done,
            status="failed" if error_code else "done",
            error_code=error_code,
        )
        log_advanced_event(
            "advanced_generation.sources_reconciled",
            filled=outcome.filled,
            deleted=outcome.deleted,
            remaining=outcome.remaining,
            status=outcome.status,
            error_code=outcome.error_code,
        )
        return outcome

    def _delete_legacy(
        self, owner_sub: str, source_file: SourceFile, now: datetime
    ) -> bool:
        """Delete one expired old record; ``False`` when its state refuses it."""
        try:
            deleting = self._store.begin_delete(
                source_file.source_file_id,
                owner_sub,
                now=now,
                stale_before=self._stale_before(now),
            )
        except SourceFileUnavailable:
            return False
        self._finish_delete(owner_sub, deleting)
        return True

    # --- plumbing ---------------------------------------------------------------

    @staticmethod
    def _stale_before(now: datetime) -> datetime:
        return now - timedelta(seconds=config.ADVANCED_UPLOAD_INSPECT_RETRY_SECONDS)

    def _unavailable_as(self, code: ErrorCode, write: Callable[[], _T]) -> _T:
        try:
            return self._call(write)
        except SourceFileUnavailable:
            raise self._error_type("source file condition failed", code=code) from None

    def _call(self, operation: Callable[[], _T]) -> _T:
        try:
            return operation()
        except SourceFileUnavailable:
            raise
        except SourceFileStoreError as e:
            raise self._error_type(
                f"source file store failed ({e.code.value})", code=e.code
            ) from e


def _at(values: Sequence[str], index: int) -> str:
    return values[index] if index < len(values) else ""
