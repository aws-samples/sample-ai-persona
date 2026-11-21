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

Only the owner sees an upload; someone else's and a missing one are the same
``UPLOAD_NOT_FOUND`` (NFR3.9). URLs are returned, never logged or stored.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Sequence, TypeVar

from ..config import config
from ..models.authz import Principal
from ..models.errors import CodedError, ErrorCode
from ..models.source_file import (
    SourceFile,
    SourceFileStatus,
    SourceFormat,
    UploadSession,
    part_count_for,
)
from ..services.s3_service import S3ServiceError
from ..services.service_factory import service_factory
from .components.advanced_generation_policy import AdvancedGenerationPolicy
from .components.source_file_catalog import SourceFileCatalog
from .shared.advanced_generation_log import log_advanced_event
from .shared.source_inspector import SourceInspectionError, inspect_table_head

if TYPE_CHECKING:
    from ..services.s3_service import S3Service
    from ..services.stores.app_settings_store import AppSettingsStore
    from ..services.stores.source_file_store import SourceFileStore

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Extensions accepted for a table (BR2.1). Excel files are saved as CSV first.
_EXTENSIONS = {".csv": SourceFormat.CSV, ".tsv": SourceFormat.TSV}
#: Content types browsers send for CSV / TSV files (empty when unknown).
_CONTENT_TYPES = frozenset(
    {
        "",
        "text/csv",
        "text/tab-separated-values",
        "text/plain",
        "application/csv",
        "application/vnd.ms-excel",
        "application/octet-stream",
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
        raise SourceUploadError(
            "not a csv or tsv file",
            code=ErrorCode.UPLOAD_SOURCE_INVALID,
            context={"reason": "format"},
        )
    return found


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
        self._source_file_catalog = SourceFileCatalog(
            source_file_store or service_factory.get_source_file_store(),
            error_type=SourceUploadError,
        )
        self._s3 = s3_service or service_factory.get_s3_service()
        self._clock = clock or _utc_now

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
        _format_of(filename)
        if content_type.strip().lower() not in _CONTENT_TYPES:
            raise SourceUploadError(
                "unexpected content type",
                code=ErrorCode.UPLOAD_SOURCE_INVALID,
                context={"reason": "format"},
            )
        self._check_size(int(size_bytes))
        now = self._clock()
        self._source_file_catalog.ensure_room(principal.sub, now=now)
        part_size = int(config.ADVANCED_UPLOAD_PART_SIZE_BYTES)
        draft = SourceFile.create_new(
            owner_sub=principal.sub,
            filename=filename,
            size_bytes=int(size_bytes),
            part_count=part_count_for(int(size_bytes), part_size),
            now=now,
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
            The ready file with its metadata.

        Raises:
            SourceUploadError: ``UPLOAD_NOT_FOUND`` (someone else's, missing,
                already completed), ``UPLOAD_SOURCE_INVALID`` /
                ``UPLOAD_TOO_LARGE`` (the file went to ``error``), or an S3 /
                store code (the file stays inspecting and may be retried).
        """
        self._advanced_generation_policy.ensure_available(principal, SourceUploadError)
        now = self._clock()
        inspecting = self._source_file_catalog.begin_inspection(
            principal.sub, source_file_id, now=now
        )
        try:
            self._finish_parts(inspecting, list(parts))
            ready = self._inspect(inspecting, now)
        except SourceUploadError as e:
            if e.code in (ErrorCode.UPLOAD_SOURCE_INVALID, ErrorCode.UPLOAD_TOO_LARGE):
                self._source_file_catalog.mark_error(source_file_id, e.code, now=now)
                self._log_completed(inspecting, e.code)
            raise
        self._log_completed(ready, None)
        return ready

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
        limit = self._advanced_generation_policy.upload_limit_bytes()
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
        try:
            metadata = inspect_table_head(
                head,
                source_format=_format_of(source_file.filename),
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

    # --- read and cancel --------------------------------------------------------

    def get_upload(self, principal: Principal, source_file_id: str) -> SourceFile:
        """The owner's upload in any state (the row of the upload list)."""
        return self._source_file_catalog.get_owned(
            principal.sub, source_file_id, not_found_code=ErrorCode.UPLOAD_NOT_FOUND
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

    def _check_size(self, size_bytes: int) -> None:
        limit = self._advanced_generation_policy.upload_limit_bytes()
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
        urls = self._s3_call(
            lambda: self._s3.generate_upload_part_urls(
                self._s3.path_of(source_file.s3_key),
                upload_id,
                list(numbers),
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
        metadata = source_file.metadata
        log_advanced_event(
            "advanced_generation.upload_completed",
            source_file_id=source_file.source_file_id,
            size_bytes=source_file.size_bytes,
            status="error" if error_code else source_file.status.value,
            error_code=error_code.value if error_code else None,
            encoding=metadata.encoding.value if metadata else None,
            estimated_rows=metadata.estimated_rows if metadata else None,
        )
