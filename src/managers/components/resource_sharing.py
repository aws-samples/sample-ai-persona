"""Department sharing and the tabbed lists (ADR-003, contract C7, U4).

``ResourceSharing`` decides *when* and *on what* the authorization checks run;
the checks themselves are ``AccessPolicy``'s (``security-design.md`` §1). It
is shared by every Manager whose resources can be shared (personas in U4,
discussions and surveys in U6 / U7), so it lives in ``components/`` (ADR-003,
ADR-011). DynamoDB is reached only through the injected store
(``ResourceShareStore``); the departments come from ``DepartmentDirectory``.

Rules (``functional-design/rules.md``):

- BR1.1 / BR1.9: every write attempt re-reads the body with a strongly
  consistent read and calls ``ensure_manageable`` on it (404 when unreadable,
  403 when readable only). Condition failures and conflicts are retried up to
  three attempts in total; the third failure is ``SHARE_UPDATE_FAILED``.
- BR1.2 / BR1.4 / BR1.5: added and removed departments must be in the user's
  choices (``shareable_groups``); only ``share:any_group`` may remove a
  department that no longer exists. Nothing is written otherwise.
- BR1.3: shares outside the choices stay: ``new = (current - remove) | add``.
- BR1.6 / BR1.7 / BR1.8 / BR1.10 / BR1.11: one conditional transaction, the
  column is removed when empty, nothing is written when nothing changes,
  index items copy ``created_at`` and ``owner_sub``, at most 99 departments
  and at most 99 changes.
- BR1.12: ``remove = (initial & removable) - selected - unchanged``.
- BR2: bulk shares check the counts and the scope once, then write each
  target and report failures (no name for unreadable targets).
- BR3: ``all`` needs ``resource:read_all`` (otherwise ``mine``); ``mine``
  queries ``OwnerIndex``; ``shared`` merges the user's departments, drops
  duplicates and the user's own; only ``all`` scans; cursors carry positions
  only and are validated (bad ones restart at the first page).
- BR4: deletes the body and its index items in one transaction conditioned
  on the shares and the cleanup columns, and returns the deleted body.

Logs: ``sharing.changed``, ``sharing.retry``, ``sharing.update_failed``
(``observability-design.md``). They carry IDs, never names.
"""

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from ...config import config
from ...models.authz import (
    AccessTarget,
    Department,
    Permission,
    Principal,
    ResourceType,
    permissions_of,
)
from ...models.errors import CodedError, ErrorCode, ErrorKind, LookupUnavailableError
from ...models.sharing import (
    BulkShareFailure,
    BulkShareResult,
    ListPage,
    ListTab,
    ResourceShareEntry,
    ShareChange,
    ShareDialogView,
    SharingTable,
    parse_share_key,
)
from ...services.resource_share_store import (
    ResourceShareStoreError,
    ShareConditionFailed,
    ShareStartKeyInvalid,
    ShareTransactionConflict,
    decode_position,
    encode_position,
)
from ..shared.structured_log import log_event
from .access_policy import AccessPolicy

#: One page of a tab (BR3.5).
PAGE_SIZE = 20
#: Most targets of one bulk share (BR2.1).
MAX_TARGETS = 100
#: Most departments one resource can be shared with, and most changes in one
#: write (BR1.11, NFR9.25).
MAX_GROUPS = 99
#: Write attempts before ``SHARE_UPDATE_FAILED`` (BR1.9).
MAX_ATTEMPTS = 3
#: Waits before retrying after a ``TransactionConflict`` (jittered).
CONFLICT_BACKOFF_SECONDS: tuple[float, ...] = (0.05, 0.1)
#: Newest items a search reads (BR3.8).
SEARCH_LIMIT = 500
#: How long the ``all`` count is kept per task (BR3.4, NFR2.4).
ALL_COUNT_TTL_SECONDS = 60
#: Label of a department that no longer exists.
VANISHED_LABEL = "（存在しない部署）"

_ID = "id"
_OWNER = "owner_sub"
_SHARED = "shared_group_ids"
_CREATED = "created_at"
_MINE_KEYS = frozenset({_ID, _OWNER, _CREATED})


class ResourceSharingError(CodedError):
    """A sharing operation failed (scope, limits, or repeated conflicts)."""

    code = ErrorCode.SHARE_UPDATE_FAILED


class ShareStore(Protocol):
    """What ``ResourceSharing`` needs from ``ResourceShareStore``."""

    def get_item(
        self, table: str, resource_id: str, attributes: Sequence[str]
    ) -> dict[str, Any] | None: ...

    def write_shares(
        self,
        table: str,
        resource_type: ResourceType,
        resource_id: str,
        *,
        expected: frozenset[str],
        new: frozenset[str],
        created_at: str,
        owner_sub: str,
    ) -> None: ...

    def delete_with_shares(
        self,
        table: str,
        resource_type: ResourceType,
        resource_id: str,
        *,
        expected: frozenset[str],
        created_at: str,
        cleanup: Mapping[str, Any],
    ) -> None: ...

    def query_owner(
        self,
        table: str,
        owner_sub: str,
        *,
        limit: int,
        start_key: Mapping[str, Any] | None = None,
    ) -> tuple[list[str], dict[str, Any] | None]: ...

    def count_owner(self, table: str, owner_sub: str) -> int: ...

    def query_group(
        self, group_id: str, resource_type: ResourceType
    ) -> list[ResourceShareEntry]: ...

    def scan_page(
        self,
        table: str,
        *,
        limit: int,
        attributes: Sequence[str],
        start_key: Mapping[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]: ...

    def scan_count(self, table: str) -> int: ...


class DepartmentListing(Protocol):
    """What ``ResourceSharing`` needs from ``DepartmentDirectory`` (C4)."""

    def list_all(self) -> list[Department]: ...

    def get_many(self, group_ids: frozenset[str]) -> dict[str, Department]: ...


def default_sharing_tables() -> list[SharingTable]:
    """The one place the shareable types are registered.

    U4 registers personas. U6 / U7 add discussions, survey templates and
    surveys here in the same change that gives their tables ``owner_sub`` and
    ``OwnerIndex``.
    """
    return [
        SharingTable(
            resource_type=ResourceType.PERSONA,
            table=config.table_name("Personas"),
            name_attribute="name",
            not_found=ErrorCode.PERSONA_NOT_FOUND,
            delete_failed=ErrorCode.PERSONA_OPERATION_FAILED,
            cleanup_attributes=("avatar_path",),
        )
    ]


@dataclass(frozen=True)
class _Count:
    value: int
    at: float


class ResourceSharing:
    """Sharing decisions and tab queries for every registered type."""

    def __init__(
        self,
        store: ShareStore,
        departments: DepartmentListing,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        """Create the component.

        Args:
            store: Reads and writes bodies and ``ResourceShares`` (injected).
            departments: The department list (injected).
            clock: Monotonic clock for the ``all`` count cache.
            sleep: Sleep for the conflict back-off (tests).
            jitter: 0-1 jitter of the conflict back-off (tests).
        """
        self._store = store
        self._departments = departments
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._tables: dict[ResourceType, SharingTable] = {}
        self._all_counts: dict[ResourceType, _Count] = {}
        self._lock = threading.Lock()

    # --- registration ----------------------------------------------------------------

    def register(self, table: SharingTable) -> None:
        """Register one shareable type.

        Raises:
            ValueError: The type is already registered (start-up error).
        """
        if table.resource_type in self._tables:
            raise ValueError(f"{table.resource_type.value} is registered twice")
        self._tables[table.resource_type] = table

    def is_registered(self, resource_type: ResourceType) -> bool:
        """Whether ``resource_type`` can be shared."""
        return resource_type in self._tables

    def tables(self) -> tuple[SharingTable, ...]:
        """Every registration (the rebuild script reads it)."""
        return tuple(self._tables.values())

    def _table(self, resource_type: ResourceType) -> SharingTable:
        table = self._tables.get(resource_type)
        if table is None:
            raise ValueError(f"{resource_type.value} is not registered for sharing")
        return table

    # --- reading bodies ----------------------------------------------------------------

    def _attributes(self, table: SharingTable) -> list[str]:
        return [
            _ID,
            _OWNER,
            _SHARED,
            _CREATED,
            table.name_attribute,
            *table.cleanup_attributes,
        ]

    def target_of(
        self,
        resource_type: ResourceType,
        body: Mapping[str, Any] | None,
        resource_id: str = "",
    ) -> AccessTarget:
        """Build the decision input from a body (``exists=False`` when ``None``)."""
        table = self._table(resource_type)
        if body is None:
            return AccessTarget.missing(resource_type, resource_id)
        name = body.get(table.name_attribute)
        return AccessTarget(
            resource_type=resource_type,
            resource_id=str(body.get(_ID, resource_id)),
            owner_sub=str(body[_OWNER]) if body.get(_OWNER) else None,
            shared_group_ids=frozenset(body.get(_SHARED) or ()),
            display_name=str(name) if name else None,
        )

    def _read(self, table: SharingTable, resource_id: str) -> dict[str, Any] | None:
        try:
            return self._store.get_item(
                table.table, resource_id, self._attributes(table)
            )
        except ResourceShareStoreError as e:
            raise LookupUnavailableError("could not read the shared resource") from e

    def can_read_body(
        self, principal: Principal, resource_type: ResourceType, body: Mapping[str, Any]
    ) -> bool:
        """``can_read`` on a body the caller read itself (BR3.7)."""
        return AccessPolicy.can_read(principal, self.target_of(resource_type, body))

    def can_manage_body(
        self, principal: Principal, resource_type: ResourceType, body: Mapping[str, Any]
    ) -> bool:
        """``can_manage`` on a body the caller read itself (BR6.4)."""
        return AccessPolicy.can_manage(principal, self.target_of(resource_type, body))

    # --- choices and the dialog ----------------------------------------------------------

    def _can_share_any(self, principal: Principal) -> bool:
        return bool(principal.sub) and Permission.SHARE_ANY_GROUP in permissions_of(
            principal.roles
        )

    def _choices(self, principal: Principal) -> tuple[list[Department], frozenset[str]]:
        """The user's choices (in directory order) and every existing department."""
        existing = self._departments.list_all()
        ids = frozenset(d.group_id for d in existing)
        allowed = AccessPolicy.shareable_groups(principal, ids)
        return [d for d in existing if d.group_id in allowed], ids

    def build_change(
        self,
        principal: Principal,
        *,
        selected: Iterable[str],
        unchanged: Iterable[str],
        initial: Iterable[str],
    ) -> ShareChange:
        """Turn the picker's values into a change (BR1.12, security-design.md §2).

        Args:
            selected: ``{name}`` (checked departments).
            unchanged: ``{name}_unchanged`` (mixed departments).
            initial: ``{name}_initial`` (checked or mixed when opened). It
                comes from the browser and is only used to narrow ``remove``.

        Raises:
            ResourceSharingError: ``SHARE_NO_DEPARTMENT`` without choices,
                ``SHARE_SCOPE_INVALID`` for out-of-scope values.
            LookupUnavailableError: The department list is unavailable.
        """
        choices, existing = self._choices(principal)
        if not choices:
            raise ResourceSharingError(
                "the user has no department to share with",
                code=ErrorCode.SHARE_NO_DEPARTMENT,
            )
        choice_ids = frozenset(d.group_id for d in choices)
        add = frozenset(selected)
        keep = frozenset(unchanged)
        initial_ids = frozenset(initial)
        removable = choice_ids
        if self._can_share_any(principal):
            removable = removable | (initial_ids | keep) - existing
        if not add <= choice_ids or not keep <= removable:
            raise ResourceSharingError(
                "a department outside the choices was sent",
                code=ErrorCode.SHARE_SCOPE_INVALID,
            )
        return ShareChange(add=add, remove=(initial_ids & removable) - add - keep)

    def dialog_state(
        self,
        principal: Principal,
        resource_type: ResourceType,
        resource_ids: Sequence[str],
        *,
        bulk: bool,
    ) -> ShareDialogView:
        """What the share dialog shows when it opens (WF1, WF2, BR7.1).

        A single dialog requires ``ensure_manageable`` (404 / 403). A bulk one
        counts the targets that cannot be managed without naming them
        (NFR1.14).

        Raises:
            CodedError: The type's NOT_FOUND, ``SHARE_NO_TARGET`` or
                ``SHARE_TOO_MANY_TARGETS``.
            AccessDeniedError: A single target readable but not manageable.
            LookupUnavailableError: Bodies or departments unavailable.
        """
        table = self._table(resource_type)
        ids = self._dedupe(resource_ids)
        if bulk:
            self._check_target_count(ids)
        elif len(ids) != 1:
            raise ValueError("a single dialog needs exactly one id")
        targets: list[AccessTarget] = []
        unmanageable = 0
        for resource_id in ids:
            target = self.target_of(
                resource_type, self._read(table, resource_id), resource_id
            )
            if not bulk:
                AccessPolicy.ensure_manageable(principal, target, table.not_found)
            if AccessPolicy.can_manage(principal, target):
                targets.append(target)
            else:
                unmanageable += 1
        choices, existing = self._choices(principal)
        choice_ids = frozenset(d.group_id for d in choices)
        shared_union = frozenset().union(*(t.shared_group_ids for t in targets))
        vanished = (
            tuple(sorted(shared_union - existing))
            if self._can_share_any(principal)
            else ()
        )
        shown = choice_ids | frozenset(vanished)
        mixed: dict[str, tuple[int, int]] | None = None
        if bulk:
            mixed = {
                g: (sum(1 for t in targets if g in t.shared_group_ids), len(targets))
                for g in sorted(shown)
                if any(g in t.shared_group_ids for t in targets)
            }
        selected = frozenset(
            g
            for g in shown
            if targets and all(g in t.shared_group_ids for t in targets)
        )
        return ShareDialogView(
            resource_type=resource_type,
            target_ids=tuple(t.resource_id for t in targets),
            target_label=self._label(targets),
            choices=tuple(choices),
            vanished=vanished,
            selected=selected,
            mixed=mixed,
            unmanageable_count=unmanageable,
            show_scope_note=not self._can_share_any(principal),
            show_persona_notice=resource_type is ResourceType.PERSONA,
            no_department=not choices,
        )

    @staticmethod
    def _label(targets: Sequence[AccessTarget]) -> str:
        if not targets:
            return ""
        first = targets[0].display_name or ""
        rest = len(targets) - 1
        return f"{first} ほか {rest} 件" if rest else first

    def department_names(self, group_ids: Iterable[str]) -> list[str]:
        """Display names of departments, in the given order (display only).

        Departments that no longer exist read ``（存在しない部署）``; when the
        directory is unavailable the IDs are shown as they are.
        """
        ids = list(dict.fromkeys(group_ids))
        try:
            known = self._departments.get_many(frozenset(ids))
        except CodedError:
            return ids
        return [known[g].display_name if g in known else VANISHED_LABEL for g in ids]

    # --- updates -------------------------------------------------------------------------

    def update_shares(
        self, principal: Principal, target: AccessTarget, change: ShareChange
    ) -> frozenset[str]:
        """Apply ``change`` to one target (BR1, contract C7).

        Returns:
            ``shared_group_ids`` after the update.

        Raises:
            CodedError: The type's NOT_FOUND (unreadable or missing).
            AccessDeniedError: Readable but not manageable.
            ResourceSharingError: Scope, owner, limit or repeated-conflict errors.
            LookupUnavailableError: The department list is unavailable.
        """
        table = self._table(target.resource_type)
        self._check_scope(principal, change)
        return self._apply(
            principal, table, target.resource_id, change, bulk=False, seen=[]
        )

    def bulk_update_shares(
        self, principal: Principal, targets: Sequence[AccessTarget], change: ShareChange
    ) -> BulkShareResult:
        """Apply ``change`` to each target and report the failures (BR2).

        Raises:
            ResourceSharingError: Counts or scope (checked once, nothing written).
            LookupUnavailableError: The department list is unavailable.
        """
        unique = list({t.resource_id: t for t in targets}.values())
        self._check_target_count([t.resource_id for t in unique])
        self._check_scope(principal, change)
        updated: list[str] = []
        failed: list[BulkShareFailure] = []
        for target in unique:
            table = self._table(target.resource_type)
            seen: list[AccessTarget] = []
            try:
                self._apply(
                    principal, table, target.resource_id, change, bulk=True, seen=seen
                )
                updated.append(target.resource_id)
            except CodedError as e:
                failed.append(self._failure(principal, target.resource_id, e, seen))
        return BulkShareResult(updated=updated, failed=failed)

    @staticmethod
    def _failure(
        principal: Principal,
        resource_id: str,
        error: CodedError,
        seen: list[AccessTarget],
    ) -> BulkShareFailure:
        name = None
        last = seen[-1] if seen else None
        if (
            error.code.kind is not ErrorKind.NOT_FOUND
            and last is not None
            and AccessPolicy.can_read(principal, last)
        ):
            name = last.display_name
        return BulkShareFailure(
            resource_id=resource_id, code=error.code, display_name=name
        )

    @staticmethod
    def _dedupe(ids: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(i for i in ids if i))

    @staticmethod
    def _check_target_count(ids: Sequence[str]) -> None:
        if not ids:
            raise ResourceSharingError("no target", code=ErrorCode.SHARE_NO_TARGET)
        if len(ids) > MAX_TARGETS:
            raise ResourceSharingError(
                f"{len(ids)} targets exceed {MAX_TARGETS}",
                code=ErrorCode.SHARE_TOO_MANY_TARGETS,
                context={"max_targets": MAX_TARGETS},
            )

    def _check_scope(self, principal: Principal, change: ShareChange) -> None:
        choices, existing = self._choices(principal)
        if not choices:
            raise ResourceSharingError(
                "the user has no department to share with",
                code=ErrorCode.SHARE_NO_DEPARTMENT,
            )
        choice_ids = frozenset(d.group_id for d in choices)
        removable = choice_ids
        if self._can_share_any(principal):
            removable = removable | (change.remove - existing)
        if not change.add <= choice_ids or not change.remove <= removable:
            raise ResourceSharingError(
                "a department outside the choices was sent",
                code=ErrorCode.SHARE_SCOPE_INVALID,
            )

    def _apply(
        self,
        principal: Principal,
        table: SharingTable,
        resource_id: str,
        change: ShareChange,
        *,
        bulk: bool,
        seen: list[AccessTarget],
    ) -> frozenset[str]:
        rtype = table.resource_type
        for attempt in range(1, MAX_ATTEMPTS + 1):
            body = self._read(table, resource_id)
            target = self.target_of(rtype, body, resource_id)
            seen.append(target)
            AccessPolicy.ensure_manageable(principal, target, table.not_found)
            assert body is not None  # ensure_manageable raised for a missing body
            current = target.shared_group_ids
            new = (current - change.remove) | change.add
            if new == current:
                return new
            added, removed = new - current, current - new
            if len(new) > MAX_GROUPS or len(added) + len(removed) > MAX_GROUPS:
                raise ResourceSharingError(
                    f"{len(new)} departments / {len(added) + len(removed)} changes",
                    code=ErrorCode.SHARE_TOO_MANY_GROUPS,
                    context={"max_groups": MAX_GROUPS},
                )
            if added and not target.owner_sub:
                raise ResourceSharingError(
                    "the resource has no owner", code=ErrorCode.SHARE_OWNER_MISSING
                )
            try:
                self._store.write_shares(
                    table.table,
                    rtype,
                    resource_id,
                    expected=current,
                    new=new,
                    created_at=str(body.get(_CREATED, "")),
                    owner_sub=target.owner_sub or "",
                )
            except (ShareConditionFailed, ShareTransactionConflict) as e:
                self._before_retry(rtype, resource_id, attempt, e)
                continue
            except ResourceShareStoreError as e:
                raise ResourceSharingError("the share write failed") from e
            log_event(
                "sharing.changed",
                actor_sub=principal.sub,
                resource_type=rtype.value,
                resource_id=resource_id,
                added=sorted(added),
                removed=sorted(removed),
                bulk=bulk,
            )
            return new
        log_event(
            "sharing.update_failed", resource_type=rtype.value, resource_id=resource_id
        )
        raise ResourceSharingError(
            "conditions kept failing", code=ErrorCode.SHARE_UPDATE_FAILED
        )

    def _before_retry(
        self, rtype: ResourceType, resource_id: str, attempt: int, error: Exception
    ) -> None:
        if attempt >= MAX_ATTEMPTS:
            return
        log_event(
            "sharing.retry",
            resource_type=rtype.value,
            resource_id=resource_id,
            attempt=attempt,
        )
        if isinstance(error, ShareTransactionConflict):
            base = CONFLICT_BACKOFF_SECONDS[
                min(attempt, len(CONFLICT_BACKOFF_SECONDS)) - 1
            ]
            self._sleep(base * (0.5 + self._jitter()))

    # --- delete ----------------------------------------------------------------------------

    def delete_with_shares(
        self, principal: Principal, resource_type: ResourceType, resource_id: str
    ) -> dict[str, Any] | None:
        """Delete a body and its index items together (BR4.1, BR4.2).

        Every attempt re-reads the body strongly and calls ``ensure_manageable``
        on that body.

        Returns:
            The body as deleted (cleanup columns included), or ``None`` when
            there was nothing to delete.

        Raises:
            CodedError: The type's NOT_FOUND, or its ``delete_failed`` code
                after three failed attempts.
            AccessDeniedError: Readable but not manageable.
        """
        table = self._table(resource_type)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                body = self._read(table, resource_id)
            except LookupUnavailableError as e:
                raise ResourceSharingError(
                    "delete read failed", code=table.delete_failed
                ) from e
            if body is None:
                return None
            target = self.target_of(resource_type, body, resource_id)
            AccessPolicy.ensure_manageable(principal, target, table.not_found)
            try:
                self._store.delete_with_shares(
                    table.table,
                    resource_type,
                    resource_id,
                    expected=target.shared_group_ids,
                    created_at=str(body.get(_CREATED, "")),
                    cleanup={c: body.get(c) for c in table.cleanup_attributes},
                )
                return body
            except (ShareConditionFailed, ShareTransactionConflict) as e:
                self._before_retry(resource_type, resource_id, attempt, e)
            except ResourceShareStoreError as e:
                raise ResourceSharingError(
                    "delete failed", code=table.delete_failed
                ) from e
        raise ResourceSharingError(
            "delete conditions kept failing", code=table.delete_failed
        )

    # --- tabs --------------------------------------------------------------------------------

    def effective_tab(self, principal: Principal, tab: ListTab) -> ListTab:
        """``all`` without ``resource:read_all`` is ``mine`` (BR3.1, never 403)."""
        if tab is ListTab.ALL and not self._reads_all(principal):
            return ListTab.MINE
        return tab

    @staticmethod
    def _reads_all(principal: Principal) -> bool:
        return bool(principal.sub) and Permission.RESOURCE_READ_ALL in permissions_of(
            principal.roles
        )

    def list_page(
        self,
        principal: Principal,
        resource_type: ResourceType,
        tab: ListTab,
        cursor: str | None,
        limit: int = PAGE_SIZE,
    ) -> ListPage:
        """One page of a tab (BR3.1〜BR3.6, BR3.9).

        ``total`` is filled on the first page only (``None`` when the count
        fails). The caller reads the bodies and re-checks them (BR3.7).

        Raises:
            LookupUnavailableError: The index could not be read.
        """
        table = self._table(resource_type)
        tab = self.effective_tab(principal, tab)
        position = self._position(principal, resource_type, tab, cursor)
        try:
            if tab is ListTab.SHARED:
                return self._shared_page(principal, resource_type, position, limit)
            if tab is ListTab.MINE:
                return self._mine_page(
                    principal, table, position, limit, cursor is None
                )
            return self._all_page(table, position, limit, cursor is None)
        except ResourceShareStoreError as e:
            raise LookupUnavailableError("could not read the list") from e

    def _cursor(self, tab: ListTab, resource_type: ResourceType, position: Any) -> str:
        return encode_position(
            {"t": tab.value, "r": resource_type.value, "p": position}
        )

    def _position(
        self,
        principal: Principal,
        resource_type: ResourceType,
        tab: ListTab,
        cursor: str | None,
    ) -> Any:
        """The validated position of ``cursor``, or ``None`` (first page, BR3.5)."""
        payload = decode_position(cursor) if cursor else None
        if (
            not payload
            or payload.get("t") != tab.value
            or payload.get("r") != resource_type.value
        ):
            return None
        position = payload.get("p")
        if tab is ListTab.SHARED:
            parsed = parse_share_key(position) if isinstance(position, str) else None
            return position if parsed and parsed[0] is resource_type else None
        expected = _MINE_KEYS if tab is ListTab.MINE else frozenset({_ID})
        if not isinstance(position, dict) or set(position) != expected:
            return None
        if not all(
            isinstance(v, dict) and isinstance(v.get("S"), str)
            for v in position.values()
        ):
            return None
        if tab is ListTab.MINE and position[_OWNER]["S"] != principal.sub:
            return None
        return position

    def _mine_page(
        self,
        principal: Principal,
        table: SharingTable,
        position: Any,
        limit: int,
        first: bool,
    ) -> ListPage:
        try:
            ids, last = self._store.query_owner(
                table.table, principal.sub, limit=limit, start_key=position
            )
        except ShareStartKeyInvalid:
            ids, last = self._store.query_owner(table.table, principal.sub, limit=limit)
        items = [
            AccessTarget(table.resource_type, i, owner_sub=principal.sub) for i in ids
        ]
        total = (
            self._safe_count(
                lambda: self._store.count_owner(table.table, principal.sub)
            )
            if first
            else None
        )
        # DynamoDB returns LastEvaluatedKey whenever Limit was reached, even on
        # the last item; peek one item ahead so a full last page has no
        # "もっと見る" leading to an empty page (like ``shared``).
        more = bool(last) and bool(
            self._store.query_owner(
                table.table, principal.sub, limit=1, start_key=last
            )[0]
        )
        next_cursor = (
            self._cursor(ListTab.MINE, table.resource_type, last) if more else None
        )
        return ListPage(items=items, next_cursor=next_cursor, total=total)

    def _shared_entries(
        self, principal: Principal, resource_type: ResourceType
    ) -> list[ResourceShareEntry]:
        merged: dict[str, ResourceShareEntry] = {}
        for group_id in sorted(principal.group_ids):
            for entry in self._store.query_group(group_id, resource_type):
                if entry.owner_sub != principal.sub:
                    merged.setdefault(entry.resource_id, entry)
        return sorted(merged.values(), key=lambda e: e.share_key, reverse=True)

    def _shared_page(
        self,
        principal: Principal,
        resource_type: ResourceType,
        position: Any,
        limit: int,
    ) -> ListPage:
        entries = self._shared_entries(principal, resource_type)
        remaining = [e for e in entries if position is None or e.share_key < position]
        page = remaining[:limit]
        items = [
            AccessTarget(resource_type, e.resource_id, owner_sub=e.owner_sub or None)
            for e in page
        ]
        next_cursor = (
            self._cursor(ListTab.SHARED, resource_type, page[-1].share_key)
            if len(remaining) > limit
            else None
        )
        return ListPage(items=items, next_cursor=next_cursor, total=len(entries))

    def _all_page(
        self, table: SharingTable, position: Any, limit: int, first: bool
    ) -> ListPage:
        attributes = [_ID, _OWNER, _SHARED, table.name_attribute]
        try:
            rows, last = self._store.scan_page(
                table.table, limit=limit, attributes=attributes, start_key=position
            )
        except ShareStartKeyInvalid:
            rows, last = self._store.scan_page(
                table.table, limit=limit, attributes=attributes
            )
        items = [self.target_of(table.resource_type, row) for row in rows]
        total = self._safe_count(lambda: self._all_count(table)) if first else None
        more = bool(last) and bool(
            self._store.scan_page(
                table.table, limit=1, attributes=[_ID], start_key=last
            )[0]
        )
        next_cursor = (
            self._cursor(ListTab.ALL, table.resource_type, last) if more else None
        )
        return ListPage(items=items, next_cursor=next_cursor, total=total)

    def _all_count(self, table: SharingTable) -> int:
        now = self._clock()
        cached = self._all_counts.get(table.resource_type)
        if cached is not None and now - cached.at < ALL_COUNT_TTL_SECONDS:
            return cached.value
        value = self._store.scan_count(table.table)
        with self._lock:
            self._all_counts[table.resource_type] = _Count(value, now)
        return value

    @staticmethod
    def _safe_count(count: Callable[[], int]) -> int | None:
        try:
            return count()
        except (ResourceShareStoreError, CodedError):
            return None

    def count(
        self, principal: Principal, resource_type: ResourceType, tab: ListTab
    ) -> int | None:
        """The count of a tab, or ``None`` when it cannot be read (BR3.4)."""
        table = self._table(resource_type)
        tab = self.effective_tab(principal, tab)
        if tab is ListTab.MINE:
            return self._safe_count(
                lambda: self._store.count_owner(table.table, principal.sub)
            )
        if tab is ListTab.SHARED:
            return self._safe_count(
                lambda: len(self._shared_entries(principal, resource_type))
            )
        return self._safe_count(lambda: self._all_count(table))

    def collect_ids(
        self,
        principal: Principal,
        resource_type: ResourceType,
        tab: ListTab,
        limit: int = SEARCH_LIMIT,
    ) -> tuple[list[str], bool]:
        """The newest ``limit`` IDs of ``mine`` / ``shared`` for a search (BR3.8).

        Returns:
            The IDs and whether more existed (the search was cut off).

        Raises:
            ValueError: ``tab`` is ``all`` after the read-all check (the caller
                searches ``all`` with its own scan).
            LookupUnavailableError: The index could not be read.
        """
        table = self._table(resource_type)
        tab = self.effective_tab(principal, tab)
        if tab is ListTab.ALL:
            raise ValueError("collect_ids does not scan; search all with a scan")
        try:
            if tab is ListTab.SHARED:
                ids = [
                    e.resource_id
                    for e in self._shared_entries(principal, resource_type)
                ]
            else:
                ids = self._owner_ids(table, principal.sub, limit + 1)
        except ResourceShareStoreError as e:
            raise LookupUnavailableError("could not read the list") from e
        return ids[:limit], len(ids) > limit

    def _owner_ids(self, table: SharingTable, owner_sub: str, wanted: int) -> list[str]:
        ids: list[str] = []
        start: dict[str, Any] | None = None
        while len(ids) < wanted:
            page, start = self._store.query_owner(
                table.table, owner_sub, limit=wanted - len(ids), start_key=start
            )
            ids += page
            if not start:
                break
        return ids
