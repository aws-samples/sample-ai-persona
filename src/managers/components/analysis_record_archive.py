"""The analysis record of an advanced generation in S3 (U1, BR9.1〜BR9.4, C7).

``AdvancedPersonaGenerationManager`` deposits and reads the record of a
finished generation; ``PersonaManager`` copies it when a candidate is saved
and removes the copy when the persona is deleted. Managers cannot import each
other, so the record lives in this component
(``.claude/rules/architecture.md``, Component layer).

- The record is ``analysis-records/<owner_sub>/<record_id>.json``. A pending
  record (record ID = generation ID) carries the tag ``pending=true``; the
  bucket lifecycle removes it (BR9.3).
- A pending record is not readable once 24 hours have passed since the
  generation finished (``GENERATION_EXPIRED``, AC10.2.3).
- Saving copies it, without tags, to the saved persona's ID (BR9.4). One copy
  belongs to one persona; deleting the persona removes that copy only.
- The body never holds a presigned URL (BR6.2): a deposit that would is
  refused.

The ``S3Service`` and the caller's exception type are given to the
constructor; this component never reads ``service_factory``. Authorization
is the caller's: the owner is part of every key.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, Optional

from ...config import config
from ...models.analysis import AnalysisRecord
from ...models.errors import CodedError, ErrorCode
from ...services.s3_service import S3ServiceError

if TYPE_CHECKING:
    from ...services.s3_service import S3Service

logger = logging.getLogger(__name__)

#: Key prefix of analysis records (the lifecycle rule filters on it).
RECORD_PREFIX = "analysis-records"
#: Tag of a record no persona has been saved from yet (contract C7).
PENDING_TAGS = {"pending": "true"}
#: Query parameters that only a presigned URL carries.
_PRESIGNED_MARKERS = ("X-Amz-Signature", "X-Amz-Credential", "Signature=")


def record_key(owner_sub: str, record_id: str) -> str:
    """``analysis-records/<owner_sub>/<record_id>.json`` (contract C7)."""
    return f"{RECORD_PREFIX}/{owner_sub}/{record_id}.json"


class AnalysisRecordArchive:
    """Deposit, read, copy and remove analysis records."""

    def __init__(
        self,
        s3_service: "S3Service",
        *,
        error_type: type[CodedError] = CodedError,
    ) -> None:
        """
        Args:
            s3_service: The upload bucket.
            error_type: The caller's Manager exception; refusals and S3
                failures are raised as this type.
        """
        self._s3 = s3_service
        self._error_type = error_type

    def deposit(self, record: AnalysisRecord) -> str:
        """Write a pending record (BR9.1).

        Returns:
            The key written.

        Raises:
            CodedError: ``GENERATION_OPERATION_FAILED`` when the body holds a
                presigned URL, the key is taken or S3 failed.
        """
        body = json.dumps(record.to_dict(), ensure_ascii=False)
        if any(marker in body for marker in _PRESIGNED_MARKERS):
            raise self._error_type(
                "analysis record holds a presigned URL",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            )
        key = record_key(record.owner_sub, record.record_id)
        self._s3_call(
            "deposit",
            lambda: self._s3.put_new_object(
                key, body.encode("utf-8"), "application/json", tags=PENDING_TAGS
            ),
        )
        return key

    def read_pending(
        self,
        owner_sub: str,
        record_id: str,
        *,
        finished_at: Optional[datetime],
        now: datetime,
    ) -> AnalysisRecord:
        """The record of a finished generation, within 24 hours (BR9.3).

        Raises:
            CodedError: ``GENERATION_EXPIRED`` past the window (or when the
                generation has not finished); ``GENERATION_NOT_FOUND`` when
                missing or someone else's.
        """
        window = timedelta(seconds=config.ADVANCED_GENERATION_RESULT_READABLE_SECONDS)
        if finished_at is None or now > finished_at + window:
            raise self._error_type(
                "analysis record expired", code=ErrorCode.GENERATION_EXPIRED
            )
        record = self._read(owner_sub, record_id)
        if record is None or record.owner_sub != owner_sub:
            raise self._error_type(
                "analysis record not found", code=ErrorCode.GENERATION_NOT_FOUND
            )
        return record

    def commit_copy(self, owner_sub: str, record_id: str, persona_id: str) -> str:
        """Copy the pending record to the saved persona's ID, without tags (BR9.4).

        Returns:
            The persona's record ID (``persona_id``).

        Raises:
            CodedError: ``GENERATION_OPERATION_FAILED`` when the copy failed.
        """
        self._s3_call(
            "copy",
            lambda: self._s3.copy_object(
                record_key(owner_sub, record_id), record_key(owner_sub, persona_id)
            ),
        )
        return persona_id

    def discard_copy(self, owner_sub: str, persona_id: str) -> None:
        """Remove the copy of one persona (other copies stay, BR9.4).

        Raises:
            CodedError: ``GENERATION_OPERATION_FAILED`` when S3 failed.
        """
        key = record_key(owner_sub, persona_id)
        self._s3_call("discard", lambda: self._s3.delete_file(self._s3.path_of(key)))

    def read_committed(
        self, owner_sub: str, persona_id: str
    ) -> Optional[AnalysisRecord]:
        """The record of a saved persona, or ``None`` when it is gone.

        Raises:
            CodedError: ``GENERATION_OPERATION_FAILED`` when S3 failed.
        """
        return self._read(owner_sub, persona_id)

    # --- plumbing ---------------------------------------------------------------

    def _read(self, owner_sub: str, record_id: str) -> Optional[AnalysisRecord]:
        path = self._s3.path_of(record_key(owner_sub, record_id))
        try:
            raw = self._s3.download_file(path)
        except S3ServiceError as e:
            if e.code is ErrorCode.S3_OBJECT_NOT_FOUND:
                return None
            raise self._error_type(
                "analysis record read failed",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e
        try:
            return AnalysisRecord.from_dict(json.loads(raw.decode("utf-8")))
        except (ValueError, KeyError, TypeError):
            logger.warning("analysis record unreadable")
            return None

    def _s3_call(self, step: str, operation: Callable[[], object]) -> None:
        try:
            operation()
        except CodedError as e:
            logger.warning("analysis record %s failed (%s)", step, e.code.value)
            raise self._error_type(
                f"analysis record {step} failed",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e
