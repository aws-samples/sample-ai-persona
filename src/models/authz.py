"""
User context and authorization types shared across layers (contracts C1, C3, C4).

``AuthMiddleware`` builds a :class:`Principal` per request and stores it on
``request.state.principal``. Routers pass it to Managers as an argument;
Managers never receive the request object.

Authorization is always decided by :class:`Permission`, never by role names
(ADR-005). :data:`ROLE_PERMISSIONS` is the only place that knows which role
grants which permission (BR2.1). The decision itself lives in
``src/managers/components/access_policy.py``; this module only holds the data.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Iterable, Mapping, Protocol, Sequence


class Role(StrEnum):
    """Application roles, taken from Cognito ``role:`` groups."""

    ADMIN = "admin"
    MEMBER = "member"


class Permission(StrEnum):
    """What a user may do. Every authorization check names one of these."""

    AI_EXECUTE = "ai:execute"
    RESOURCE_CREATE = "resource:create"
    RESOURCE_READ_ALL = "resource:read_all"
    RESOURCE_MANAGE_ALL = "resource:manage_all"
    SHARE_ANY_GROUP = "share:any_group"
    SETTINGS_MANAGE = "settings:manage"


#: Role -> permissions. The single table of BR2.1; read-only at run time.
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = MappingProxyType(
    {
        Role.ADMIN: frozenset(Permission),
        Role.MEMBER: frozenset({Permission.AI_EXECUTE, Permission.RESOURCE_CREATE}),
    }
)


def permissions_of(roles: Iterable[Role]) -> frozenset[Permission]:
    """Return the union of the permissions of ``roles`` (BR2.1).

    No role, or only names that are not in :data:`ROLE_PERMISSIONS`, yields the
    empty set, which allows nothing (FR1.7, NFR1.7).
    """
    granted: set[Permission] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


@dataclass(frozen=True)
class Principal:
    """The signed-in user for one request. Never persisted; rebuilt per request.

    Attributes:
        sub: Cognito ``sub`` (``local-dev`` style identifier under AUTH_MODE=local).
        roles: Application roles. Empty means no role assigned yet.
        group_ids: Department group names without the ``group:`` prefix.
        display_name: Name shown in the header, when known.
        email: E-mail address, shown when no name is known.
    """

    sub: str
    roles: frozenset[Role] = field(default_factory=frozenset)
    group_ids: frozenset[str] = field(default_factory=frozenset)
    display_name: str | None = None
    email: str | None = None


class ResourceType(StrEnum):
    """Kinds of resources that carry an owner or a usage scope (contract C1)."""

    PERSONA = "persona"
    DISCUSSION = "discussion"
    SURVEY_TEMPLATE = "survey_template"
    SURVEY = "survey"
    KNOWLEDGE_BASE = "knowledge_base"
    DATASET = "dataset"
    CUSTOM_PERSONA_DATA = "custom_persona_data"


@dataclass(frozen=True)
class AccessTarget:
    """What an authorization decision looks at, and nothing more (ADR-011).

    Read by ``ResourceLookup`` and judged by ``AccessPolicy``. It never goes to
    a response: Routers must not return it (NFR3.7).

    Attributes:
        resource_type: Kind of the resource.
        resource_id: ID of the resource.
        owner_sub: Owner's ``sub``. ``None`` for organization assets and for
            resources whose owner is unknown.
        shared_group_ids: Departments it is shared with (the usage scope for
            organization assets).
        display_name: Name for messages, when the table has one.
        exists: ``False`` when the resource does not exist (or was deleted).
    """

    resource_type: ResourceType
    resource_id: str
    owner_sub: str | None = None
    shared_group_ids: frozenset[str] = field(default_factory=frozenset)
    display_name: str | None = None
    exists: bool = True

    @classmethod
    def missing(cls, resource_type: ResourceType, resource_id: str) -> "AccessTarget":
        """A target for an ID that does not exist (never an exception, C3)."""
        return cls(resource_type=resource_type, resource_id=resource_id, exists=False)


@dataclass(frozen=True)
class Department:
    """A department, made from a Cognito ``group:`` group (ADR-009, C4).

    Attributes:
        group_id: Group name without the ``group:`` prefix.
        display_name: The group description, or ``group_id`` when it has none.
    """

    group_id: str
    display_name: str


class LookupReader(Protocol):
    """How ``ResourceLookup`` reads one resource type (BR2.10, Q1).

    Implemented in the Service layer (it talks to DynamoDB) and registered with
    ``ResourceLookup`` once at start-up by the Unit that owns the table.
    """

    def read(self, ids: Sequence[str], deadline: float) -> dict[str, AccessTarget]:
        """Read at most 100 IDs before ``deadline`` (a ``time.monotonic`` value).

        Returns:
            One AccessTarget per ID found; IDs that do not exist may be left
            out (``ResourceLookup`` fills them with ``exists=False``).

        Raises:
            CodedError: The table could not be read in time. ``ResourceLookup``
                turns it into ``LookupUnavailableError`` (fail closed).
        """
        ...
