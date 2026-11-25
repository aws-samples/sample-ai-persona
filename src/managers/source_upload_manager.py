"""Uploads of tables for the advanced generation (beta) U1 (W2, W3).

The browser sends the file straight to S3 in 16MB parts with presigned
``UploadPart`` URLs (BR2.3, BR2.6); this Manager only answers the start,
part URL refresh, completion, status and cancel requests:

- Start (BR2.1〜BR2.3): CSV / TSV only, at most the environment's limit (the
  exact limit is accepted), at most ten live files. Nothing is issued and
  nothing is written to S3 when a check fails.
- Refresh (BR2.4): new URLs for part numbers of the owner's uploading file,
  24 times at most per upload.
- Completion (BR2.7, BR3.1〜BR3.3, NFR5.19, NFR5.20): takes effect once (or
  again after a 15-minute stuck inspection); the sizes S3 holds must add up to
  the declared size and stay within the limit, otherwise the upload is
  aborted and the file goes to ``error``. Then the first bytes only are read
  and inspected, the checksum is read and the file becomes ``ready``.
- Cancel (W2-6): drops the parts and the record of an uploading file.
- U2: a number for files of the same name and a 7-day expiry at the start
  (BR11.3, BR13.2); a second completion returns the current row (BR11.4);
  one error code per fix of the table (BR12.1); the list of the owner's live
  files with the repair of old records (W2-1, NFR3.16); and the deletion of
  every S3 version and the record (W2-6, BR14.1, BR14.2).

Only the owner sees an upload; someone else's, a missing and an expired one
are the same ``UPLOAD_NOT_FOUND`` (NFR3.9, BR15.1). URLs are returned, never
logged or stored; logs carry IDs, states, codes and counts only (BR15.3).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Sequence, TypeVar

from ..config import config
from ..models.authz import Principal
from ..models.errors import CodedError, ErrorCode, ErrorKind
from ..models.source_file import (
    DocumentMetadata,
    SourceFile,
    SourceFileListItem,
    SourceFileStatus,
    SourceFormat,
    SourceKind,
    SourceMetadata,
    UploadSession,
    part_count_for,
    part_sizes_for,
)
from ..services.s3_service import S3ServiceError
from ..services.service_factory import service_factory
from .components.advanced_generation_policy import AdvancedGenerationPolicy
from .components.source_file_catalog import SourceFileCatalog
from .shared.advanced_generation_log import log_advanced_event
from .shared.source_inspector import (
    SourceInspectionError,
    inspect_document,
    inspect_table_head,
)

if TYPE_CHECKING:
    from ..services.s3_service import S3Service
    from ..services.stores.app_settings_store import AppSettingsStore
    from ..services.stores.source_file_store import SourceFileStore

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Extensions accepted: tables (BR2.1) and documents (U4 BR21.1). Excel files
#: are saved as CSV first; an old Word file as .docx (BR21.2).
_EXTENSIONS = {
    f".{fmt.value}": fmt
    for fmt in (
        SourceFormat.CSV,
        SourceFormat.TSV,
        SourceFormat.PDF,
        SourceFormat.DOCX,
        SourceFormat.TXT,
        SourceFormat.MD,
    )
}
_LEGACY_WORD = ".doc"
#: Content types browsers send for these files (empty when unknown).
_CONTENT_TYPES = frozenset(
    {
        "",
        "text/csv",
        "text/tab-separated-values",
        "text/plain",
        "application/csv",
        "application/vnd.ms-excel",
        "application/octet-stream",
        # U4: documents
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/markdown",
        "text/x-markdown",
    }
)


class SourceUploadError(CodedError):
    """A refused or failed upload request (the router turns the code into words)."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _size_context(limit: int) -> dict[str, object]:
    """The limit for the wording (``{max_size_mb}``) and for the screen."""
    return {"max_bytes": limit, "max_size_mb": limit // 1_000_000}


def _format_of(filename: str) -> SourceFormat:
    extension = os.path.splitext(filename.strip().lower())[1]
    found = _EXTENSIONS.get(extension)
    if found is None:
        reason = "legacy_word" if extension == _LEGACY_WORD else "format"
        raise SourceUploadError(
            "not an accepted file format",
            code=ErrorCode.UPLOAD_SOURCE_INVALID,
            context={"reason": reason},
        )
    return found


def _limit_for(source_format: SourceFormat, table_limit: int) -> int:
    """1GB for a table (BR2.2), 100MB for a document (BR21.1)."""
    if source_format.kind is SourceKind.DOCUMENT:
        return int(config.ADVANCED_DOCUMENT_MAX_BYTES)
    return table_limit


class SourceUploadManager:
    """Start, refresh, complete, read and cancel the user's table uploads."""

    def __init__(
        self,
        settings_store: Optional["AppSettingsStore"] = None,
        source_file_store: Optional["SourceFileStore"] = None,
        s3_service: Optional["S3Service"] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        """
        Args:
            settings_store: ``AppSettings`` (optional, the factory's when omitted).
            source_file_store: ``SourceFiles`` (optional, the factory's).
            s3_service: The upload bucket (optional, the factory's).
            clock: Aware-UTC clock (tests).
        """
        self._advanced_generation_policy = AdvancedGenerationPolicy(
            settings_store or service_factory.get_app_settings_store()
        )
        self._s3 = s3_service or service_factory.get_s3_service()
        self._clock = clock or _utc_now
        self._source_file_catalog = SourceFileCatalog(
            source_file_store or service_factory.get_source_file_store(),
            s3=self._s3,
            error_type=SourceUploadError,
            clock=self._clock,
        )

    # --- start and part URLs ----------------------------------------------------

    def start_upload(
        self,
        principal: Principal,
        *,
        filename: str,
        size_bytes: int,
        content_type: str,
    ) -> UploadSession:
        """Start a multipart upload and issue every part URL (BR2.1〜BR2.3).

        Raises:
            AccessDeniedError: The user lacks ``ai:execute``.
            SourceUploadError: ``ADVANCED_GENERATION_UNAVAILABLE``,
                ``UPLOAD_SOURCE_INVALID``, ``UPLOAD_TOO_LARGE`` (with
                ``max_bytes``), ``UPLOAD_TOO_MANY_FILES`` or an S3 / store code.
        """
        self._advanced_generation_policy.ensure_available(principal, SourceUploadError)
        source_format = _format_of(filename)
        if content_type.strip().lower() not in _CONTENT_TYPES:
            raise SourceUploadError(
                "unexpected content type",
                code=ErrorCode.UPLOAD_SOURCE_INVALID,
                context={"reason": "format"},
            )
        self._check_size(int(size_bytes), source_format)
        now = self._clock()
        # BR21.3: tables and documents count together
        self._source_file_catalog.ensure_room(principal.sub, now=now)
        part_size = int(config.ADVANCED_UPLOAD_PART_SIZE_BYTES)
        draft = SourceFile.create_new(
            owner_sub=principal.sub,
            filename=filename,
            size_bytes=int(size_bytes),
            part_count=part_count_for(int(size_bytes), part_size),
            now=now,
            kind=source_format.kind,
        )
        # BR11.3, BR13.2: the number is fixed now and the 7 days start now
        draft = draft.update(
            display_seq=self._source_file_catalog.next_display_seq(
                principal.sub, draft.filename
            ),
            expires_at=self._source_file_catalog.new_expiry(now),
        )
        upload_id = self._s3_call(
            lambda: self._s3.create_multipart_upload(
                draft.s3_key, content_type or "application/octet-stream"
            )
        )
        source_file = draft.update(multipart_upload_id=upload_id)
        try:
            self._source_file_catalog.create(source_file)
        except CodedError:
            self._abort_quietly(source_file)
            raise
        return self._session(source_file, range(1, source_file.part_count + 1), now)

    def refresh_part_urls(
        self, principal: Principal, source_file_id: str, part_numbers: Sequence[int]
    ) -> UploadSession:
        """New URLs for parts not sent yet (BR2.4, FD-Q7).

        Raises:
            SourceUploadError: ``UPLOAD_NOT_FOUND`` (someone else's, missing or
                no longer uploading), ``UPLOAD_SOURCE_INVALID`` (a number out of
                range, or the refresh limit with ``max_refreshes``).
        """
        self._advanced_generation_policy.ensure_available(principal, SourceUploadError)
        found = self._uploading(principal, source_file_id)
        numbers = [int(n) for n in part_numbers]
        if (
            not numbers
            or len(set(numbers)) != len(numbers)
            or any(not 1 <= n <= found.part_count for n in numbers)
        ):
            raise SourceUploadError(
                "part number out of range",
                code=ErrorCode.UPLOAD_SOURCE_INVALID,
                context={"reason": "part_number"},
            )
        limit = int(config.ADVANCED_UPLOAD_URL_REFRESH_MAX)
        if found.url_refresh_count >= limit:
            raise SourceUploadError(
                "part url refreshes used up",
                code=ErrorCode.UPLOAD_SOURCE_INVALID,
                context={"max_refreshes": limit},
            )
        now = self._clock()
        refreshed = self._source_file_catalog.record_url_refresh(
            principal.sub, source_file_id, now=now
        )
        return self._session(refreshed, numbers, now)

    # --- completion -------------------------------------------------------------

    def complete_upload(
        self,
        principal: Principal,
        source_file_id: str,
        parts: Iterable[tuple[int, str]],
    ) -> SourceFile:
        """Complete the upload once and inspect its head (BR2.7, BR3.1〜BR3.3).

        Args:
            principal: The uploader.
            source_file_id: The upload.
            parts: ``(part_number, etag)`` pairs the browser collected.

        Returns:
            The ready file with its metadata. A second completion (another
            tab, U2 BR11.4) gets the row as it is now, without inspecting
            again: ``inspecting`` (the first one is still at it), ``ready``
            or ``error``.

        Raises:
            SourceUploadError: ``UPLOAD_NOT_FOUND`` (someone else's, missing,
                expired, still uploading after 24 hours), a VALIDATION /
                CAPACITY code such as ``UPLOAD_SOURCE_INVALID``,
                ``UPLOAD_TOO_LARGE`` or a ``SOURCE_*`` inspection code (the
                file went to ``error``), or an S3 / store code (the file stays
                inspecting and may be retried after 15 minutes).
        """
        self._advanced_generation_policy.ensure_available(principal, SourceUploadError)
        now = self._clock()
        try:
            inspecting = self._source_file_catalog.begin_inspection(
                principal.sub, source_file_id, now=now
            )
        except SourceUploadError as e:
            if e.code is not ErrorCode.UPLOAD_NOT_FOUND:
                raise
            return self._completed_before(principal, source_file_id, now)
        try:
            self._finish_parts(inspecting, list(parts))
            ready = self._inspect(inspecting, now)
        except SourceUploadError as e:
            if e.code.kind in (ErrorKind.VALIDATION, ErrorKind.CAPACITY):
                self._source_file_catalog.mark_error(source_file_id, e.code, now=now)
                self._log_completed(inspecting, e.code)
            raise
        self._log_completed(ready, None)
        return ready

    def _completed_before(
        self, principal: Principal, source_file_id: str, now: datetime
    ) -> SourceFile:
        """The row of an upload another request completed (BR11.4, NFR5.31)."""
        found = self._source_file_catalog.get_owned(
            principal.sub, source_file_id, not_found_code=ErrorCode.UPLOAD_NOT_FOUND
        )
        if found.status in (
            SourceFileStatus.READY,
            SourceFileStatus.ERROR,
        ) or self._source_file_catalog.inspection_in_progress(found, now):
            return found
        raise SourceUploadError(
            "upload not completable", code=ErrorCode.UPLOAD_NOT_FOUND
        )

    def _finish_parts(
        self, source_file: SourceFile, parts: list[tuple[int, str]]
    ) -> None:
        """Check the sizes S3 holds, then complete or abort (BR2.7)."""
        upload_id = source_file.multipart_upload_id
        if not upload_id:
            return
        try:
            held = self._s3.list_parts(source_file.s3_key, upload_id)
        except S3ServiceError as e:
            if e.code is ErrorCode.S3_OBJECT_NOT_FOUND:
                return  # completed by an earlier attempt that did not finish
            raise SourceUploadError("list parts failed", code=e.code) from e
        total = sum(p.size_bytes for p in held)
        limit = _limit_for(
            _format_of(source_file.filename),
            self._advanced_generation_policy.upload_limit_bytes(),
        )
        refusal: Optional[ErrorCode] = None
        if total > limit:
            refusal = ErrorCode.UPLOAD_TOO_LARGE
        elif total != source_file.size_bytes or sorted(n for n, _ in parts) != [
            p.part_number for p in held
        ]:
            refusal = ErrorCode.UPLOAD_SOURCE_INVALID
        if refusal is not None:
            self._abort_quietly(source_file)
            context = (
                _size_context(limit) if refusal is ErrorCode.UPLOAD_TOO_LARGE else {}
            )
            raise SourceUploadError(
                "uploaded parts do not match", code=refusal, context=context
            )
        self._s3_call(
            lambda: self._s3.complete_multipart_upload(
                source_file.s3_key, upload_id, parts
            )
        )

    def _inspect(self, source_file: SourceFile, now: datetime) -> SourceFile:
        head = self._s3_call(
            lambda: self._s3.read_range(
                source_file.s3_key, int(config.ADVANCED_INSPECT_BYTES)
            )
        )
        source_format = _format_of(source_file.filename)
        metadata: SourceMetadata | DocumentMetadata
        try:
            if source_format.kind is SourceKind.DOCUMENT:
                metadata = self._inspect_document(source_file, source_format, head, now)
            else:
                metadata = inspect_table_head(
                    head,
                    source_format=source_format,
                    total_size=source_file.size_bytes,
                    now=now,
                )
        except SourceInspectionError as e:
            raise SourceUploadError(
                "inspection failed", code=e.code, context=dict(e.context)
            ) from None
        checksum = self._s3_call(lambda: self._s3.get_checksum(source_file.s3_key))
        return self._source_file_catalog.mark_ready(
            source_file.source_file_id, metadata, checksum or "", now=now
        )

    def _inspect_document(
        self,
        source_file: SourceFile,
        source_format: SourceFormat,
        head: bytes,
        now: datetime,
    ) -> DocumentMetadata:
        """Head and tail of a document (U4 BR22.1〜BR22.3, NFR3.25)."""
        size = source_file.size_bytes
        tail = (
            head
            if len(head) >= size
            else self._s3_call(
                lambda: self._s3.read_tail(
                    source_file.s3_key, int(config.ADVANCED_INSPECT_TAIL_BYTES)
                )
            )
        )
        chars_per_mb = (
            config.ADVANCED_DOCUMENT_CHARS_PER_MB_PDF
            if source_format is SourceFormat.PDF
            else config.ADVANCED_DOCUMENT_CHARS_PER_MB_DOCX
        )
        return inspect_document(
            head,
            tail,
            source_format=source_format,
            total_size=size,
            now=now,
            chars_per_mb=int(chars_per_mb),
        )

    # --- read and cancel --------------------------------------------------------

    def get_upload(self, principal: Principal, source_file_id: str) -> SourceFile:
        """The owner's unexpired upload in any state (the row of the upload list)."""
        return self._source_file_catalog.get_owned(
            principal.sub, source_file_id, not_found_code=ErrorCode.UPLOAD_NOT_FOUND
        )

    def list_uploads(self, principal: Principal) -> list[SourceFile]:
        """The owner's files for the list, newest first (W2-1, BR13.1, BR15.1).

        Expired files and uploads abandoned for 24 hours are left out;
        ``deleting`` ones stay (their deletion can be retried). Old U1
        records are given their expiry, or deleted once past it, a few at a
        time (NFR3.16); that never stops the list.

        Raises:
            SourceUploadError: ``ADVANCED_GENERATION_UNAVAILABLE`` or a store
                code.
        """
        self._advanced_generation_policy.ensure_available(principal, SourceUploadError)
        return self._source_file_catalog.list_owned(principal.sub, reconcile=True)

    def list_item(self, source_file: SourceFile) -> SourceFileListItem:
        """The list row of ``source_file`` now (in use, selectable, expiry date)."""
        return SourceFileListItem.of(
            source_file,
            now=self._clock(),
            retention_days=int(config.ADVANCED_SOURCE_RETENTION_DAYS),
        )

    def inspection_stuck(self, source_file: SourceFile) -> bool:
        """An ``inspecting`` file untouched for 15 minutes (it may be deleted, BR14.1)."""
        return (
            source_file.status is SourceFileStatus.INSPECTING
            and not self._source_file_catalog.inspection_in_progress(
                source_file, self._clock()
            )
        )

    def delete_source(self, principal: Principal, source_file_id: str) -> None:
        """Delete the owner's file: every S3 version and the record (W2-6, BR14.1).

        Deleting stays possible when the feature is closed meanwhile, so the
        user can always remove their data. A failed S3 deletion leaves the
        row "削除できませんでした"; deleting again continues.

        Raises:
            SourceUploadError: ``UPLOAD_NOT_FOUND`` (someone else's, missing,
                expired, uploading or inspecting), ``SOURCE_FILE_IN_USE``
                (BR14.2) or an S3 / store code.
        """
        try:
            versions = self._source_file_catalog.delete_owned(
                principal.sub, source_file_id
            )
        except SourceUploadError as e:
            if e.code is not ErrorCode.UPLOAD_NOT_FOUND:
                log_advanced_event(
                    "advanced_generation.source_deleted",
                    source_file_id=source_file_id,
                    status="in_use"
                    if e.code is ErrorCode.SOURCE_FILE_IN_USE
                    else "failed",
                    error_code=e.code.value,
                )
            raise
        log_advanced_event(
            "advanced_generation.source_deleted",
            source_file_id=source_file_id,
            status="deleted",
            versions=versions,
        )

    def cancel_upload(self, principal: Principal, source_file_id: str) -> None:
        """Drop the parts and the record of the owner's uploading file (W2-6).

        Cancelling stays possible when the feature is closed meanwhile, so that
        no parts are left behind.

        Raises:
            SourceUploadError: ``UPLOAD_NOT_FOUND`` when someone else's,
                missing or no longer uploading.
        """
        found = self._uploading(principal, source_file_id)
        self._abort_quietly(found)
        if not self._source_file_catalog.cancel_upload(principal.sub, source_file_id):
            raise SourceUploadError(
                "upload not cancellable", code=ErrorCode.UPLOAD_NOT_FOUND
            )

    # --- helpers ----------------------------------------------------------------

    def _check_size(self, size_bytes: int, source_format: SourceFormat) -> None:
        limit = _limit_for(
            source_format, self._advanced_generation_policy.upload_limit_bytes()
        )
        if size_bytes < 1:
            raise SourceUploadError(
                "empty file",
                code=ErrorCode.UPLOAD_SOURCE_INVALID,
                context={"reason": "empty"},
            )
        if size_bytes > limit:
            raise SourceUploadError(
                "file over the limit",
                code=ErrorCode.UPLOAD_TOO_LARGE,
                context=_size_context(limit),
            )

    def _uploading(self, principal: Principal, source_file_id: str) -> SourceFile:
        found = self._source_file_catalog.get_owned(
            principal.sub, source_file_id, not_found_code=ErrorCode.UPLOAD_NOT_FOUND
        )
        if (
            found.status is not SourceFileStatus.UPLOADING
            or not found.multipart_upload_id
        ):
            raise SourceUploadError("not uploading", code=ErrorCode.UPLOAD_NOT_FOUND)
        return found

    def _session(
        self, source_file: SourceFile, numbers: Iterable[int], now: datetime
    ) -> UploadSession:
        expiration = int(config.ADVANCED_UPLOAD_URL_EXPIRATION_SECONDS)
        upload_id = source_file.multipart_upload_id or ""
        # The signature carries each part's length, so S3 refuses a larger body.
        expected = part_sizes_for(
            source_file.size_bytes, int(config.ADVANCED_UPLOAD_PART_SIZE_BYTES)
        )
        part_sizes = {n: expected[n] for n in numbers}
        urls = self._s3_call(
            lambda: self._s3.generate_upload_part_urls(
                self._s3.path_of(source_file.s3_key),
                upload_id,
                part_sizes,
                expiration=expiration,
            )
        )
        return UploadSession(
            source_file_id=source_file.source_file_id,
            part_size_bytes=int(config.ADVANCED_UPLOAD_PART_SIZE_BYTES),
            part_count=source_file.part_count,
            part_urls=tuple(urls),
            urls_expire_at=now + timedelta(seconds=expiration),
        )

    def _abort_quietly(self, source_file: SourceFile) -> None:
        """Abort the multipart upload; a failure is left to the 1-day lifecycle."""
        if not source_file.multipart_upload_id:
            return
        try:
            self._s3.abort_multipart_upload(
                source_file.s3_key, source_file.multipart_upload_id
            )
        except S3ServiceError as e:
            logger.warning("multipart upload not aborted (%s)", e.code.value)

    @staticmethod
    def _s3_call(operation: Callable[[], _T]) -> _T:
        try:
            return operation()
        except S3ServiceError as e:
            raise SourceUploadError("s3 operation failed", code=e.code) from e

    @staticmethod
    def _log_completed(
        source_file: SourceFile, error_code: Optional[ErrorCode]
    ) -> None:
        table = source_file.table_metadata
        document = source_file.document_metadata
        encoding = table.encoding if table else document.encoding if document else None
        log_advanced_event(
            "advanced_generation.upload_completed",
            source_file_id=source_file.source_file_id,
            kind=source_file.kind.value,
            size_bytes=source_file.size_bytes,
            status="error" if error_code else source_file.status.value,
            error_code=error_code.value if error_code else None,
            encoding=encoding.value if encoding else None,
            estimated_rows=table.estimated_rows if table else None,
            estimated_chars=document.estimated_chars if document else None,
        )
