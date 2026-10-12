"""
User context types shared across layers (contract C1).

``AuthMiddleware`` builds a :class:`Principal` per request and stores it on
``request.state.principal``. Routers pass it to Managers as an argument;
Managers never receive the request object. U2 adds ``Permission`` and the
access-target types to this module.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class Role(StrEnum):
    """Application roles, taken from Cognito ``role:`` groups."""

    ADMIN = "admin"
    MEMBER = "member"


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
