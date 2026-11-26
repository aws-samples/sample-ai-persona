"""
Types for department sharing and the tabbed lists.

``ResourceSharing`` (``src/managers/components/resource_sharing.py``) works on
these values; this module only holds data and pure helpers.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, Iterable, TypeVar

from .authz import AccessTarget, Department, ResourceType
from .errors import ErrorCode

#: The resource types that can be shared with departments.
SHAREABLE_TYPES: frozenset[ResourceType] = frozenset(
    {
        ResourceType.PERSONA,
        ResourceType.DISCUSSION,
        ResourceType.SURVEY_TEMPLATE,
        ResourceType.SURVEY,
    }
)

#: Separator inside a ``share_key`` (``{resource_type}#{created_at}#{resource_id}``).
SHARE_KEY_SEPARATOR = "#"


class ListTab(StrEnum):
    """The three list tabs."""

    MINE = "mine"
    SHARED = "shared"
    ALL = "all"

    @classmethod
    def parse(cls, value: str | None) -> "ListTab | None":
        """Return the tab named ``value``, or ``None`` when it is unknown."""
        if not value:
            return None
        try:
            return cls(value)
        except ValueError:
            return None


@dataclass(frozen=True)
class ShareChange:
    """A difference to apply to ``shared_group_ids``.

    Departments in neither set stay as they are (the mixed state of a bulk
    share).

    Raises:
        TypeError: ``add`` or ``remove`` is not a frozenset.
        ValueError: A department is in both sets.
    """

    add: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.add, frozenset) or not isinstance(
            self.remove, frozenset
        ):
            raise TypeError("ShareChange.add and .remove must be frozensets")
        if self.add & self.remove:
            raise ValueError("a department cannot be both added and removed")

    @property
    def is_empty(self) -> bool:
        """Whether the change adds and removes nothing."""
        return not self.add and not self.remove


@dataclass(frozen=True)
class AssetScopeChange:
    """A change of an organization asset's usage scope.

    The same shape as :class:`ShareChange`: ``add`` is the departments chosen,
    ``remove`` those unchecked that were in the scope when the dialog opened.

    Raises:
        TypeError: ``add`` or ``remove`` is not a frozenset.
        ValueError: A department is in both sets.
    """

    add: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.add, frozenset) or not isinstance(
            self.remove, frozenset
        ):
            raise TypeError("AssetScopeChange.add and .remove must be frozensets")
        if self.add & self.remove:
            raise ValueError("a department cannot be both added and removed")

    @classmethod
    def from_form(
        cls, selected: Iterable[str], initial: Iterable[str]
    ) -> "AssetScopeChange":
        """The change of the dialog: add what is checked, remove what was unchecked.

        ``initial`` is the scope when the dialog opened, so a department another
        admin added meanwhile is left as it is.
        """
        chosen = frozenset(g for g in selected if g)
        opened = frozenset(g for g in initial if g)
        return cls(add=chosen, remove=opened - chosen)

    @property
    def is_empty(self) -> bool:
        """Whether the change adds and removes nothing."""
        return not self.add and not self.remove

    def applied_to(self, current: frozenset[str]) -> frozenset[str]:
        """The scope after the change: ``(current - remove) | add``."""
        return (frozenset(current) - self.remove) | self.add


@dataclass(frozen=True)
class AssetScopeDialog:
    """The content of the usage-scope dialog.

    Attributes:
        resource_type: ``knowledge_base``, ``dataset`` or ``custom_persona_data``.
        resource_id: The asset.
        name: Its name (the dialog title).
        choices: Every department (admins may choose any).
        selected: The scope when the dialog opened (sent back as ``initial``).
        vanished: Departments in the scope that no longer exist; shown as
            ``（削除されたグループ）`` and checked, so they can be removed.
    """

    resource_type: ResourceType
    resource_id: str
    name: str
    choices: tuple[Department, ...]
    selected: frozenset[str] = frozenset()
    vanished: tuple[str, ...] = ()


_AssetT = TypeVar("_AssetT")


@dataclass(frozen=True)
class ScopedAssets(Generic[_AssetT]):
    """Organization assets on the settings screen with their scope.

    Attributes:
        items: The assets.
        scope_labels: Asset id -> names of its scope's departments (empty =
            admin only; deleted groups read ``（削除されたグループ）``).
    """

    items: list[_AssetT]
    scope_labels: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ListPage:
    """One page of a tab.

    Attributes:
        items: At most one page of targets. The caller reads the bodies.
        next_cursor: Opaque cursor of the next page; ``None`` on the last one.
        total: Count of the tab, or ``None`` when it could not be read.
    """

    items: list[AccessTarget]
    next_cursor: str | None = None
    total: int | None = None


@dataclass(frozen=True)
class BulkShareFailure:
    """One target a bulk share could not update.

    Attributes:
        resource_id: The target.
        code: Why it failed.
        display_name: Its name, only when the user may read it.
    """

    resource_id: str
    code: ErrorCode
    display_name: str | None = None


@dataclass(frozen=True)
class BulkShareResult:
    """The result of ``bulk_update_shares``."""

    updated: list[str]
    failed: list[BulkShareFailure]


@dataclass(frozen=True)
class SharingTable:
    """Registration of one shareable resource type.

    Attributes:
        resource_type: The type.
        table: Physical name of the body table.
        name_attribute: Column holding the display name.
        not_found: Code for an unreadable or missing target.
        delete_failed: Code when the delete transaction keeps conflicting.
        cleanup_attributes: Columns the caller cleans up after deleting; they
            are part of the delete condition.
    """

    resource_type: ResourceType
    table: str
    name_attribute: str
    not_found: ErrorCode
    delete_failed: ErrorCode
    cleanup_attributes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceShareEntry:
    """One item of the ``ResourceShares`` table."""

    group_id: str
    resource_type: ResourceType
    resource_id: str
    created_at: str
    owner_sub: str

    @property
    def share_key(self) -> str:
        """The sort key of this item."""
        return share_key(self.resource_type, self.created_at, self.resource_id)


@dataclass(frozen=True)
class OwnerSummary:
    """A user in the owner copy (display only)."""

    sub: str
    display_name: str | None
    enabled: bool


def share_key(resource_type: ResourceType, created_at: str, resource_id: str) -> str:
    """Build ``{resource_type}#{created_at}#{resource_id}``."""
    return SHARE_KEY_SEPARATOR.join((resource_type.value, created_at, resource_id))


def parse_share_key(value: str) -> tuple[ResourceType, str, str] | None:
    """Split a ``share_key``; ``None`` when it is malformed.

    ``created_at`` never contains ``#`` (ISO 8601), so the first and the last
    separators delimit it.
    """
    head, sep, rest = value.partition(SHARE_KEY_SEPARATOR)
    created_at, sep2, resource_id = rest.rpartition(SHARE_KEY_SEPARATOR)
    if not sep or not sep2 or not created_at or not resource_id:
        return None
    try:
        resource_type = ResourceType(head)
    except ValueError:
        return None
    return resource_type, created_at, resource_id


_ItemT = TypeVar("_ItemT")


@dataclass(frozen=True)
class TabbedList(Generic[_ItemT]):
    """What a tabbed list screen shows, built by the owning Manager.

    Attributes:
        tab: The tab actually shown (``all`` becomes ``mine`` without
            ``resource:read_all``).
        items: The rows, newest first.
        next_cursor: Cursor of the next page (``None`` when there is none).
        counts: Count per visible tab; ``None`` when it could not be read.
        show_all: Whether the ``all`` tab is shown.
        owners: Row id -> owner label; ``None`` when the owner copy is
            unavailable (shown as ``-``).
        shared_with: Row id -> names of the departments it is shared with
            (``mine`` / ``all``) or shared from (``shared``).
        manageable: Row ids the user may edit, delete and share.
        no_department: The user belongs to no department.
        search_truncated: A search stopped at the newest 500 items.
    """

    tab: ListTab
    items: list[_ItemT]
    next_cursor: str | None = None
    counts: dict[str, int | None] = field(default_factory=dict)
    show_all: bool = False
    owners: dict[str, str | None] = field(default_factory=dict)
    shared_with: dict[str, list[str]] = field(default_factory=dict)
    manageable: frozenset[str] = frozenset()
    no_department: bool = False
    search_truncated: bool = False


@dataclass(frozen=True)
class ShareDialogView:
    """The content of the share dialog.

    Attributes:
        resource_type: Type of the targets.
        target_ids: Targets the user may manage (sent back on save).
        target_label: ``佐藤 太郎`` or ``佐藤 太郎 ほか 2 件``.
        choices: Departments the user may choose.
        vanished: Departments that no longer exist but are still shared;
            only for ``share:any_group``.
        selected: Departments shared by every target.
        mixed: For a bulk dialog, ``{group_id: (shared, total)}``.
        unmanageable_count: Targets left out because they cannot be managed.
        show_scope_note: Show "選べるのは所属グループだけです".
        show_persona_notice: Show the long-term memory notice.
        no_department: No department to choose; saving is disabled.
    """

    resource_type: ResourceType
    target_ids: tuple[str, ...]
    target_label: str
    choices: tuple[Department, ...]
    vanished: tuple[str, ...] = ()
    selected: frozenset[str] = frozenset()
    mixed: dict[str, tuple[int, int]] | None = None
    unmanageable_count: int = 0
    show_scope_note: bool = False
    show_persona_notice: bool = False
    no_department: bool = False

    @property
    def is_bulk(self) -> bool:
        """Whether the dialog was opened for a selection."""
        return self.mixed is not None
