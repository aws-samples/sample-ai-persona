"""The owner of uploaded source files (advanced generation U1, contract C4).

``SourceUploadManager`` creates and inspects the files and
``AdvancedPersonaGenerationManager`` reads them for a generation and releases
their in-use marks. Managers cannot import each other, so the rules on files
live in this component (``.claude/rules/architecture.md``, Component layer):

- Only the owner sees a file; someone else's file and a missing file get the
  same NOT_FOUND code (NFR3.9).
- A generation uses only the owner's ``ready`` table (BR6.2).
- A user holds at most ``ADVANCED_UPLOAD_MAX_FILES`` live files: uploading
  ones younger than 24 hours, inspecting and ready ones (BR2.1, NFR5.5). The
  count reads the eventually consistent GSI, so two starts at the same moment
  may both pass (accepted in U1, scalability-design.md §2).
- An inspection stuck past 15 minutes may start again (NFR5.19).
- The in-use mark is set in the start transaction of the generation store and
  removed here by whoever ends the generation (BR6.4).

The ``SourceFileStore`` and the caller's exception type are given to the
constructor; this component never reads ``service_factory``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, TypeVar

from ...config import config
from ...models.errors import CodedError, ErrorCode
from ...models.source_file import (
    SourceFile,
    SourceFileStatus,
    SourceKind,
    SourceMetadata,
)
from ...services.stores.source_file_store import (
    SourceFileStoreError,
    SourceFileUnavailable,
)

if TYPE_CHECKING:
    from ...services.stores.source_file_store import SourceFileStore

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_HELD_STATES = frozenset(
    {SourceFileStatus.UPLOADING, SourceFileStatus.INSPECTING, SourceFileStatus.READY}
)


class SourceFileCatalog:
    """Reads and state changes of the user's source files."""

    def __init__(
        self,
        store: "SourceFileStore",
        *,
        error_type: type[CodedError] = CodedError,
    ) -> None:
        """
        Args:
            store: ``SourceFiles`` reads and conditional writes.
            error_type: The caller's Manager exception; every refusal and
                every store failure is raised as this type.
        """
        self._store = store
        self._error_type = error_type

    # --- reads ------------------------------------------------------------------

    def get_owned(
        self, owner_sub: str, source_file_id: str, *, not_found_code: ErrorCode
    ) -> SourceFile:
        """The owner's file; anything else is ``not_found_code`` (NFR3.9)."""
        found = self._call(lambda: self._store.get_for_owner(source_file_id, owner_sub))
        if found is None:
            raise self._error_type("source file not found", code=not_found_code)
        return found

    def ready_table(self, owner_sub: str, source_file_id: str) -> SourceFile:
        """The owner's ready table for a generation (BR6.2).

        Raises:
            CodedError: ``GENERATION_SOURCE_NOT_FOUND`` when it is someone
                else's, missing, not a table or not ready.
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

    def held_count(self, owner_sub: str, *, now: datetime) -> int:
        """Live files of the owner (abandoned uploads are not counted, NFR5.5)."""
        oldest = now - timedelta(seconds=config.ADVANCED_UPLOAD_PENDING_MAX_SECONDS)
        files = self._call(lambda: self._store.list_for_owner(owner_sub))
        return sum(
            1
            for f in files
            if f.status in _HELD_STATES
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
                stale_before=now
                - timedelta(seconds=config.ADVANCED_UPLOAD_INSPECT_RETRY_SECONDS),
                created_after=now
                - timedelta(seconds=config.ADVANCED_UPLOAD_PENDING_MAX_SECONDS),
            ),
        )

    def mark_ready(
        self,
        source_file_id: str,
        metadata: SourceMetadata,
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
        """Remove the in-use mark of ``generation_id`` (BR6.4; never raises)."""
        try:
            return self._store.clear_in_use(source_file_id, generation_id, now=now)
        except SourceFileStoreError as e:
            logger.warning("source file in-use mark not released (%s)", e.code.value)
            return False

    # --- plumbing ---------------------------------------------------------------

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
