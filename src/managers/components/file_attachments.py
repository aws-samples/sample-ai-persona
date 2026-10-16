"""Attaching uploads to their parent and deciding who may read them (U5).

A Component (``.claude/rules/architecture.md``): ``FileManager`` (uploads and
delivery) and the parents' Managers (``DiscussionManager``,
``AgentDiscussionManager``, ``SurveyTemplateManager``) share one instance,
built in ``sharing_runtime.shared_access_parts``. Services are injected; this
module never reaches for ``service_factory``.

``attach_all`` (security-design.md §3, BR5.3):

1. Check the IDs (UUID shape, duplicates dropped). More than 100 ->
   ``FILE_ATTACH_LIMIT_EXCEEDED``; none -> nothing to do.
2. Read every file strongly; unless each is the user's, unexpired and
   unattached -> ``FILE_NOT_FOUND`` (nothing changes).
3. Remove every ``pending`` tag; a failure goes to step 7.
4. One transaction writes the parent and removes ``expires_at``; any failure
   goes to step 7.
5. Remove the tags again (another request's step 7 may have put one back
   between 3 and 4). A failure is logged (``retag_clear``); the attach stands.
6. Return the :class:`AttachResult` (the original expiries, for ``detach``).
7. ``_restore_tags``: re-read each file and put the tag back only while it is
   unattached; read again and take the tag off if it got attached meanwhile.
   Then ``FILE_OPERATION_FAILED``.

Delivery (BR5.4〜BR5.7): before attaching, only the uploader and only before
``expires_at``; after attaching, the parent type's registered reader decides.
U6 registers discussions (the discussion's readers, BR5.1). Until U7 registers
survey templates, a file attached to one is served to its uploader and to
``resource:read_all`` only (narrower than the parent's readers, so nothing leaks).
``delete_attached`` removes the files of a deleted parent (U6 BR1.5).

Logs: ``file.attach_rollback_failed`` (``file_id``, ``parent_type``, ``step``,
``error_type``; NFR8.15) and ``authz.denied`` for refused reads.
"""

import logging
import time
from typing import Callable, Mapping, Protocol, Sequence

from ...models.authz import AccessTarget, Principal, ResourceType
from ...models.errors import CodedError, ErrorCode
from ...models.uploaded_file import (
    PARENT_TYPES,
    PENDING_TAG,
    AttachResult,
    UploadedFile,
    is_uuid,
)
from ..shared.structured_log import log_event
from .access_policy import AccessPolicy

logger = logging.getLogger(__name__)

#: Most files one parent may attach at once (the transaction limit).
MAX_ATTACH_FILES = 100

#: Parent type -> the resource type its reader judges.
_PARENT_RESOURCE: Mapping[str, ResourceType] = {
    "discussion": ResourceType.DISCUSSION,
    "survey_template": ResourceType.SURVEY_TEMPLATE,
}

#: ``(principal, parent_id) -> may read the parent``; registered by U6 / U7.
ParentReader = Callable[[Principal, str], bool]


class FileMetadataStore(Protocol):
    """What this component needs from ``UploadedFileStore``."""

    def get(self, file_id: str) -> UploadedFile | None: ...

    def get_many(self, file_ids: Sequence[str]) -> dict[str, UploadedFile]: ...

    def transact_attach(
        self,
        owner_sub: str,
        file_ids: Sequence[str],
        parent_type: str,
        parent_id: str,
        now_epoch: int,
    ) -> None: ...

    def transact_detach(
        self, original_expires_at: Mapping[str, int], parent_type: str, parent_id: str
    ) -> None: ...

    def delete(self, file_id: str, parent_type: str, parent_id: str) -> None: ...


class ObjectTags(Protocol):
    """What this component needs from ``S3Service``."""

    def delete_object_tagging(self, s3_key: str) -> None: ...

    def put_object_tags(self, s3_key: str, tags: Mapping[str, str]) -> None: ...

    def delete_file(self, s3_path: str) -> bool: ...


def _not_found(message: str) -> CodedError:
    return CodedError(message, code=ErrorCode.FILE_NOT_FOUND)


class FileAttachments:
    """Attach / detach uploads and decide who may read them."""

    def __init__(
        self,
        store: FileMetadataStore,
        tags: ObjectTags | None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the component.

        Args:
            store: Upload metadata (``UploadedFileStore``).
            tags: Object tags (``S3Service``); ``None`` when files are stored
                locally (no lifecycle, so no tags).
            clock: Epoch-seconds clock (tests pass a fake one).
        """
        self._store = store
        self._tags = tags
        self._clock = clock
        self._readers: dict[str, ParentReader] = {}

    # --- registration -----------------------------------------------------------

    def register_parent(self, parent_type: str, reader: ParentReader) -> None:
        """Register how readers of one parent type are decided (U6 / U7).

        Raises:
            ValueError: Unknown parent type, or registered twice (start-up error).
        """
        if parent_type not in PARENT_TYPES:
            raise ValueError(f"unknown parent type {parent_type!r}")
        if parent_type in self._readers:
            raise ValueError(f"reader for {parent_type} registered twice")
        self._readers[parent_type] = reader

    # --- attach / detach ----------------------------------------------------------

    def attach_all(
        self,
        principal: Principal,
        file_ids: Sequence[str],
        parent_type: str,
        parent_id: str,
    ) -> AttachResult:
        """Attach the user's uploads to a parent, all or nothing (BR5.3).

        Raises:
            CodedError: ``FILE_ATTACH_LIMIT_EXCEEDED`` (more than 100),
                ``FILE_NOT_FOUND`` (a file is not the user's, expired,
                attached or missing), ``FILE_OPERATION_FAILED`` (tags or the
                transaction failed; nothing stays attached).
        """
        ids = list(dict.fromkeys(file_ids))
        if len(ids) > MAX_ATTACH_FILES:
            raise CodedError(
                f"{len(ids)} files attached at once",
                code=ErrorCode.FILE_ATTACH_LIMIT_EXCEEDED,
                context={"max_files": MAX_ATTACH_FILES},
            )
        if not ids:
            return AttachResult.empty(parent_type, parent_id)
        now = int(self._clock())
        found = self._read_many(ids) if all(is_uuid(i) for i in ids) else {}
        usable = [
            f
            for i in ids
            if (f := found.get(i)) is not None and self.is_attachable(principal, f, now)
        ]
        if len(usable) != len(ids):
            self._deny(principal)
            raise _not_found("a file is not attachable by this user")

        cleared: list[UploadedFile] = []
        try:
            for f in usable:
                self._clear_tag(f)
                cleared.append(f)
            self._store.transact_attach(principal.sub, ids, parent_type, parent_id, now)
        except Exception as e:
            logger.warning("Attaching %d files failed: %s", len(ids), type(e).__name__)
            self._restore_tags([f.id for f in usable], parent_type)
            raise CodedError(
                f"attach failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

        for f in usable:
            try:
                self._clear_tag(f)
            except Exception as e:
                self._rollback_failed(f.id, parent_type, "retag_clear", e)
        return AttachResult(
            parent_type=parent_type,
            parent_id=parent_id,
            original_expires_at={f.id: int(f.expires_at or now) for f in usable},
        )

    def detach(self, result: AttachResult) -> None:
        """Undo an attach after the parent failed to save (never raises).

        The files go back to unattached with their original expiry, and the
        ``pending`` tag is put back once they are read as unattached.
        """
        if not result.file_ids:
            return
        try:
            self._store.transact_detach(
                result.original_expires_at, result.parent_type, result.parent_id
            )
        except Exception as e:
            for file_id in result.file_ids:
                self._rollback_failed(file_id, result.parent_type, "detach", e)
            return
        self._restore_tags(list(result.file_ids), result.parent_type)

    def delete_attached(
        self, parent_type: str, parent_id: str, file_ids: Sequence[str]
    ) -> list[str]:
        """Delete the files attached to a deleted parent (U6 BR1.5 (3)).

        Only files still attached to ``parent_id`` are touched: the S3 object
        first, then the metadata (conditioned on the parent). Files without an
        S3 object (local development) lose only their metadata.

        Returns:
            The IDs that could not be deleted (the caller retries and logs).
        """
        failed: list[str] = []
        for file_id in dict.fromkeys(file_ids):
            try:
                uploaded = self._store.get(file_id) if is_uuid(file_id) else None
                if uploaded is None or (uploaded.parent_type, uploaded.parent_id) != (
                    parent_type,
                    parent_id,
                ):
                    continue
                if self._tags is not None and self._tagged(uploaded):
                    self._tags.delete_file(uploaded.file_path)
                self._store.delete(file_id, parent_type, parent_id)
            except Exception as e:
                logger.warning("Deleting an attached file failed: %s", type(e).__name__)
                failed.append(file_id)
        return failed

    def _restore_tags(self, file_ids: Sequence[str], parent_type: str) -> None:
        """Step 7: put ``pending`` back on files that are still unattached."""
        for file_id in file_ids:
            try:
                current = self._store.get(file_id)
                if current is None or current.is_attached or not self._tagged(current):
                    continue
                self._put_tag(current)
                again = self._store.get(file_id)
                if again is not None and again.is_attached:
                    self._clear_tag(again)
            except Exception as e:
                self._rollback_failed(file_id, parent_type, "restore_tag", e)

    # --- reading ------------------------------------------------------------------

    def can_read_file(self, principal: Principal, file_id: str) -> bool:
        """Whether the user may read or be served the file (BR5.4〜BR5.7)."""
        try:
            self.readable_file(principal, file_id)
        except CodedError as e:
            if e.code is ErrorCode.FILE_NOT_FOUND:
                return False
            raise
        return True

    def readable_file(self, principal: Principal, file_id: str) -> UploadedFile:
        """The file's metadata when the user may read it.

        Raises:
            CodedError: ``FILE_NOT_FOUND`` when unreadable, expired, missing
                or not a file ID (the same answer, NFR1.14).
        """
        uploaded = self._read_one(file_id) if is_uuid(file_id) else None
        if uploaded is not None and self._may_read(principal, uploaded):
            return uploaded
        self._deny(principal)
        raise _not_found("file is not readable by this user")

    def attachable_file(self, principal: Principal, file_id: str) -> UploadedFile:
        """The file's metadata when the user may still attach it (R-02).

        A discussion's start reads its documents with this, the same rule as
        :meth:`attach_all` at the save (functional-spec.md WF6 step 5): the
        user's own, unexpired, not yet attached upload. A file attached to
        another parent is refused before the discussion runs.

        Raises:
            CodedError: ``FILE_NOT_FOUND`` when not attachable (the same answer
                as a missing file, NFR1.14).
        """
        uploaded = self._read_one(file_id) if is_uuid(file_id) else None
        if uploaded is not None and self.is_attachable(
            principal, uploaded, int(self._clock())
        ):
            return uploaded
        self._deny(principal)
        raise _not_found("file is not attachable by this user")

    @staticmethod
    def is_attachable(principal: Principal, uploaded: UploadedFile, now: int) -> bool:
        """The one rule for attaching and for a discussion's start (BR5.3, BR5.4).

        The user's own upload, not attached to any parent and not expired at
        ``now`` (epoch seconds; ``expires_at`` itself is expired).
        """
        return AccessPolicy.is_owner(
            principal, uploaded.owner_sub
        ) and uploaded.usable_before_attach(principal.sub, now)

    def _may_read(self, principal: Principal, uploaded: UploadedFile) -> bool:
        if not uploaded.is_attached:
            return self.is_attachable(principal, uploaded, int(self._clock()))
        parent_type = str(uploaded.parent_type)
        parent_id = str(uploaded.parent_id)
        reader = self._readers.get(parent_type)
        if reader is not None:
            return bool(reader(principal, parent_id))
        # Not registered yet: the uploader and resource:read_all only (BR5.7)
        target = AccessTarget(
            resource_type=_PARENT_RESOURCE[parent_type],
            resource_id=parent_id,
            owner_sub=uploaded.owner_sub,
        )
        return AccessPolicy.can_read(principal, target)

    # --- plumbing -----------------------------------------------------------------

    def _read_many(self, ids: Sequence[str]) -> dict[str, UploadedFile]:
        try:
            return self._store.get_many(ids)
        except Exception as e:
            logger.warning("Reading files failed: %s", type(e).__name__)
            raise CodedError(
                f"file read failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    def _read_one(self, file_id: str) -> UploadedFile | None:
        try:
            return self._store.get(file_id)
        except Exception as e:
            logger.warning("Reading a file failed: %s", type(e).__name__)
            raise CodedError(
                f"file read failed ({type(e).__name__})",
                code=ErrorCode.FILE_OPERATION_FAILED,
            ) from e

    def _tagged(self, uploaded: UploadedFile) -> bool:
        """Whether the file's object lives in S3 (only those carry tags)."""
        return self._tags is not None and uploaded.file_path.startswith("s3://")

    def _clear_tag(self, uploaded: UploadedFile) -> None:
        if self._tags is not None and self._tagged(uploaded):
            self._tags.delete_object_tagging(uploaded.s3_key)

    def _put_tag(self, uploaded: UploadedFile) -> None:
        if self._tags is not None and self._tagged(uploaded):
            key, value = PENDING_TAG
            self._tags.put_object_tags(uploaded.s3_key, {key: value})

    @staticmethod
    def _deny(principal: Principal) -> None:
        log_event(
            "authz.denied", reason="not_found", resource_type="file", sub=principal.sub
        )

    @staticmethod
    def _rollback_failed(
        file_id: str, parent_type: str, step: str, error: BaseException
    ) -> None:
        log_event(
            "file.attach_rollback_failed",
            level="ERROR",
            file_id=file_id,
            parent_type=parent_type,
            step=step,
            error_type=type(error).__name__,
        )
