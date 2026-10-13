"""The share dialog and saving the shares (WF1, WF2, U4).

The entry points under ``/share/{resource_type}/...`` are the same for every
shareable type. This Manager turns their values into ``ResourceSharing`` calls:

- the dialog content (choices, the state of each department, the notes),
- the change built from the picker's values (BR1.12; the browser's values are
  never trusted, ``security-design.md`` §2),
- the result of a save, with failures counted without names when the user
  may not read the target (BR2.4, NFR1.14).

The decisions themselves are ``ResourceSharing``'s (and ``AccessPolicy``'s).
"""

import logging
import re
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Sequence, TypeVar

from ..models.authz import AccessTarget, Principal, ResourceType
from ..models.errors import CodedError, ErrorCode
from ..models.sharing import BulkShareFailure, ShareDialogView, SharingTable
from ..services.service_factory import service_factory
from .components.resource_sharing import (
    MAX_TARGETS,
    VANISHED_LABEL,
    ResourceSharing,
    ResourceSharingError,
)
from .components.sharing_runtime import build_sharing_parts, shared_parts

logger = logging.getLogger(__name__)

__all__ = [
    "VANISHED_LABEL",
    "ShareSaveResult",
    "SharingManager",
    "SharingManagerError",
]

#: IDs are UUIDs (or similar); anything else is treated as a missing target.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

_T = TypeVar("_T")


class SharingManagerError(CodedError):
    """An unexpected failure while sharing (retrying can help)."""

    code = ErrorCode.SHARE_UPDATE_FAILED


@dataclass(frozen=True)
class ShareSaveResult:
    """The result of a save (BR7.3).

    Attributes:
        resource_type: Type of the targets.
        total: Distinct targets of the request.
        updated: Targets saved (including the ones that did not change).
        failed: Targets that could not be saved.
    """

    resource_type: ResourceType
    total: int
    updated: tuple[str, ...]
    failed: tuple[BulkShareFailure, ...] = ()

    @property
    def is_complete(self) -> bool:
        """Whether every target was saved."""
        return not self.failed

    @property
    def named_failures(self) -> tuple[BulkShareFailure, ...]:
        """Failures whose target the user may read (shown with the name)."""
        return tuple(f for f in self.failed if f.display_name)

    @property
    def unnamed_failure_count(self) -> int:
        """Failures shown only as a count (BR2.4)."""
        return sum(1 for f in self.failed if not f.display_name)

    @property
    def failed_ids(self) -> tuple[str, ...]:
        """IDs to select again when the list is reloaded (BR7.3)."""
        return tuple(f.resource_id for f in self.failed)


class SharingManager:
    """The share dialog and the share saves for every registered type."""

    def __init__(self, resource_sharing: ResourceSharing | None = None) -> None:
        """Create the manager.

        Args:
            resource_sharing: The sharing component. The per-task instance
                shared with the other Managers when omitted.
        """
        if resource_sharing is None:
            resource_sharing = shared_parts(
                lambda: build_sharing_parts(
                    service_factory.get_resource_share_store(),
                    service_factory.get_department_source(),
                    service_factory.get_user_source(),
                )
            ).sharing
        self._resource_sharing = resource_sharing

    # --- the type -------------------------------------------------------------------

    def resource_type_of(self, value: str) -> ResourceType:
        """The registered type named ``value``.

        Raises:
            CodedError: ``HTTP_ERROR`` (404) for unknown or unregistered types.
        """
        for table in self._resource_sharing.tables():
            if table.resource_type.value == value:
                return table.resource_type
        raise CodedError("resource type is not shareable", code=ErrorCode.HTTP_ERROR)

    def _table(self, value: str) -> SharingTable:
        resource_type = self.resource_type_of(value)
        return next(
            t
            for t in self._resource_sharing.tables()
            if t.resource_type is resource_type
        )

    # --- the dialog -----------------------------------------------------------------

    def dialog(
        self, principal: Principal, resource_type: str, resource_id: str
    ) -> ShareDialogView:
        """The dialog for one target (WF1). 404 when unreadable, 403 when
        readable only (BR1.1)."""
        table = self._table(resource_type)
        self._require_valid_id(table, resource_id)
        return self._guard(
            lambda: self._resource_sharing.dialog_state(
                principal, table.resource_type, [resource_id], bulk=False
            )
        )

    def bulk_dialog(
        self, principal: Principal, resource_type: str, resource_ids: Sequence[str]
    ) -> ShareDialogView:
        """The dialog for a selection (WF2, BR7.1).

        Targets that cannot be managed, and malformed IDs, are only counted.
        """
        table = self._table(resource_type)
        ids = self._checked_ids(resource_ids)
        valid = [i for i in ids if _ID_PATTERN.match(i)]
        if not valid:
            raise CodedError("no readable target", code=table.not_found)
        view = self._guard(
            lambda: self._resource_sharing.dialog_state(
                principal, table.resource_type, valid, bulk=True
            )
        )
        invalid = len(ids) - len(valid)
        return replace(view, unmanageable_count=view.unmanageable_count + invalid)

    # --- saving ---------------------------------------------------------------------

    def save(
        self,
        principal: Principal,
        resource_type: str,
        resource_id: str,
        *,
        selected: Iterable[str],
        unchanged: Iterable[str],
        initial: Iterable[str],
    ) -> ShareSaveResult:
        """Save the shares of one target (WF1, BR1).

        Raises:
            CodedError: The type's NOT_FOUND, sharing validation / capacity
                codes, ``LOOKUP_UNAVAILABLE`` or ``SHARE_UPDATE_FAILED``.
            AccessDeniedError: Readable but not manageable.
        """
        table = self._table(resource_type)
        self._require_valid_id(table, resource_id)

        def run() -> ShareSaveResult:
            change = self._resource_sharing.build_change(
                principal, selected=selected, unchanged=unchanged, initial=initial
            )
            self._resource_sharing.update_shares(
                principal, AccessTarget(table.resource_type, resource_id), change
            )
            return ShareSaveResult(table.resource_type, 1, (resource_id,))

        return self._guard(run)

    def bulk_save(
        self,
        principal: Principal,
        resource_type: str,
        resource_ids: Sequence[str],
        *,
        selected: Iterable[str],
        unchanged: Iterable[str],
        initial: Iterable[str],
    ) -> ShareSaveResult:
        """Save the shares of a selection (WF2, BR2).

        The counts and the scope are checked once; then each target is saved
        and the failures are reported (malformed IDs fail as missing).
        """
        table = self._table(resource_type)
        ids = self._checked_ids(resource_ids)
        valid = [i for i in ids if _ID_PATTERN.match(i)]

        def run() -> ShareSaveResult:
            change = self._resource_sharing.build_change(
                principal, selected=selected, unchanged=unchanged, initial=initial
            )
            updated: list[str] = []
            failures: dict[str, BulkShareFailure] = {}
            if valid:
                result = self._resource_sharing.bulk_update_shares(
                    principal,
                    [AccessTarget(table.resource_type, i) for i in valid],
                    change,
                )
                updated = list(result.updated)
                failures = {f.resource_id: f for f in result.failed}
            for resource_id in ids:
                if not _ID_PATTERN.match(resource_id):
                    failures[resource_id] = BulkShareFailure(
                        resource_id, table.not_found
                    )
            ordered = tuple(failures[i] for i in ids if i in failures)
            return ShareSaveResult(
                table.resource_type, len(ids), tuple(updated), ordered
            )

        return self._guard(run)

    # --- helpers --------------------------------------------------------------------

    @staticmethod
    def _require_valid_id(table: SharingTable, resource_id: str) -> None:
        if not _ID_PATTERN.match(resource_id or ""):
            raise CodedError("malformed resource id", code=table.not_found)

    @staticmethod
    def _checked_ids(resource_ids: Sequence[str]) -> list[str]:
        ids = list(dict.fromkeys(i.strip() for i in resource_ids if i and i.strip()))
        if not ids:
            raise ResourceSharingError("no target", code=ErrorCode.SHARE_NO_TARGET)
        if len(ids) > MAX_TARGETS:
            raise ResourceSharingError(
                f"{len(ids)} targets exceed {MAX_TARGETS}",
                code=ErrorCode.SHARE_TOO_MANY_TARGETS,
                context={"max_targets": MAX_TARGETS},
            )
        return ids

    @staticmethod
    def _guard(run: Callable[[], _T]) -> _T:
        """Coded errors pass through; anything else becomes a Manager error."""
        try:
            return run()
        except CodedError:
            raise
        except Exception as e:
            logger.error("Unexpected error while sharing", exc_info=True)
            raise SharingManagerError(f"sharing failed ({type(e).__name__})") from e
