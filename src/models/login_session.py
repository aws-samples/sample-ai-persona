"""
LoginSession data model (U1, ``entities.md``).

A LoginSession is the server-side state behind the ``session_id`` cookie.
For ``oidc`` / ``admin_api`` it lives in the DynamoDB Sessions table; for
``local`` it lives in the process (BR1.15). Only the hash of the session id is
ever stored (BR1.5).

Writes come in two kinds that must never overwrite each other (BR1.10,
BR1.12):

- the activity write: ``last_accessed_at`` only, without touching ``version``
  (:meth:`LoginSession.touch` / :meth:`LoginSession.last_access_attributes`);
- the versioned write: tokens, roles, departments and
  ``refresh_failing_since``, conditional on ``version`` and bumping it
  (:meth:`LoginSession.update` / :meth:`LoginSession.versioned_attributes`).
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol

from .authz import Principal, Role

#: Cleanup horizon for the DynamoDB TTL attribute. Expiry decisions are made by
#: the AuthManager from ``created_at`` / ``last_accessed_at``, never from ``ttl``.
SESSION_TTL = timedelta(days=7)

#: Attributes written by the versioned write, in a stable order.
VERSIONED_ATTRIBUTES: tuple[str, ...] = (
    "roles",
    "group_ids",
    "display_name",
    "email",
    "access_token_enc",
    "refresh_token_enc",
    "key_version",
    "access_expires_at",
    "refresh_failing_since",
    "version",
)

_SET_ATTRIBUTES = ("roles", "group_ids")
_TIME_ATTRIBUTES = (
    "created_at",
    "last_accessed_at",
    "access_expires_at",
    "refresh_failing_since",
)


class AuthMethod(StrEnum):
    """How the session was established. Must match the running AUTH_MODE (BR1.21)."""

    OIDC = "oidc"
    ADMIN_API = "admin_api"
    LOCAL = "local"


@dataclass(frozen=True)
class LoginSession:
    """Server-side login session. Immutable: updates return new instances."""

    session_id_hash: str
    user_sub: str
    auth_method: AuthMethod
    created_at: datetime
    last_accessed_at: datetime
    ttl: int
    roles: frozenset[Role] = field(default_factory=frozenset)
    group_ids: frozenset[str] = field(default_factory=frozenset)
    display_name: str | None = None
    email: str | None = None
    access_token_enc: str | None = None
    refresh_token_enc: str | None = None
    key_version: str | None = None
    access_expires_at: datetime | None = None
    refresh_failing_since: datetime | None = None
    version: int = 1

    @classmethod
    def create_new(
        cls,
        *,
        session_id_hash: str,
        user_sub: str,
        auth_method: AuthMethod,
        now: datetime,
        roles: frozenset[Role] = frozenset(),
        group_ids: frozenset[str] = frozenset(),
        display_name: str | None = None,
        email: str | None = None,
        access_token_enc: str | None = None,
        refresh_token_enc: str | None = None,
        key_version: str | None = None,
        access_expires_at: datetime | None = None,
        refresh_failing_since: datetime | None = None,
    ) -> "LoginSession":
        """Create a session at ``version=1`` whose ``ttl`` is ``now + 7 days``."""
        return cls(
            session_id_hash=session_id_hash,
            user_sub=user_sub,
            auth_method=auth_method,
            created_at=now,
            last_accessed_at=now,
            ttl=int((now + SESSION_TTL).timestamp()),
            roles=frozenset(roles),
            group_ids=frozenset(group_ids),
            display_name=display_name,
            email=email,
            access_token_enc=access_token_enc,
            refresh_token_enc=refresh_token_enc,
            key_version=key_version,
            access_expires_at=access_expires_at,
            refresh_failing_since=refresh_failing_since,
        )

    def update(
        self,
        *,
        roles: frozenset[Role] | None = None,
        group_ids: frozenset[str] | None = None,
        display_name: str | None = None,
        email: str | None = None,
        access_token_enc: str | None = None,
        refresh_token_enc: str | None = None,
        key_version: str | None = None,
        access_expires_at: datetime | None = None,
        refresh_failing_since: datetime | None | Literal["keep"] = "keep",
    ) -> "LoginSession":
        """Return the result of a versioned write (``version`` + 1).

        ``None`` keeps the current value, except for ``refresh_failing_since``
        where ``None`` clears it and ``"keep"`` (the default) keeps it.
        """

        def keep(new: Any, current: Any) -> Any:
            return current if new is None else new

        failing_since = (
            self.refresh_failing_since
            if isinstance(refresh_failing_since, str)
            else refresh_failing_since
        )
        return replace(
            self,
            roles=keep(roles, self.roles),
            group_ids=keep(group_ids, self.group_ids),
            display_name=keep(display_name, self.display_name),
            email=keep(email, self.email),
            access_token_enc=keep(access_token_enc, self.access_token_enc),
            refresh_token_enc=keep(refresh_token_enc, self.refresh_token_enc),
            key_version=keep(key_version, self.key_version),
            access_expires_at=keep(access_expires_at, self.access_expires_at),
            refresh_failing_since=failing_since,
            version=self.version + 1,
        )

    def touch(self, now: datetime) -> "LoginSession":
        """Return the result of the activity write (``version`` unchanged)."""
        return replace(self, last_accessed_at=now)

    def to_principal(self) -> Principal:
        """Build the per-request Principal (contract C1)."""
        return Principal(
            sub=self.user_sub,
            roles=self.roles,
            group_ids=self.group_ids,
            display_name=self.display_name,
            email=self.email,
        )

    def _serialized(self, name: str) -> Any:
        """Storage form of one attribute, or ``None`` when it must be absent.

        Empty sets are absent because DynamoDB string sets cannot be empty
        (Functional Design review R-13).
        """
        value = getattr(self, name)
        if value is None:
            return None
        if name in _TIME_ATTRIBUTES:
            return value.isoformat()
        if name in _SET_ATTRIBUTES:
            return {str(v) for v in value} or None
        if isinstance(value, AuthMethod):
            return value.value
        return value

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage. ``None`` and empty sets are omitted (R-13)."""
        names = (
            "session_id_hash",
            "user_sub",
            "auth_method",
            "created_at",
            "last_accessed_at",
            "ttl",
            *VERSIONED_ATTRIBUTES,
        )
        data = {name: self._serialized(name) for name in names}
        return {name: value for name, value in data.items() if value is not None}

    def last_access_attributes(self) -> dict[str, Any]:
        """Attributes of the activity write: ``last_accessed_at`` only (BR1.10)."""
        return {"last_accessed_at": self._serialized("last_accessed_at")}

    def versioned_attributes(self) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Attributes of the versioned write (BR1.11, BR1.12).

        Returns:
            ``(to_set, to_remove)``: values to write, and names whose value is
            now ``None`` or an empty set and must be removed from the item.
            ``last_accessed_at`` is never part of this write.
        """
        to_set: dict[str, Any] = {}
        to_remove: list[str] = []
        for name in VERSIONED_ATTRIBUTES:
            value = self._serialized(name)
            if value is None:
                to_remove.append(name)
            else:
                to_set[name] = value
        return to_set, tuple(to_remove)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoginSession":
        """Deserialize. Missing ``roles`` / ``group_ids`` become empty sets."""

        def parse_time(name: str) -> datetime | None:
            value = data.get(name)
            return datetime.fromisoformat(value) if value else None

        created_at = parse_time("created_at")
        last_accessed_at = parse_time("last_accessed_at")
        if created_at is None or last_accessed_at is None:
            raise ValueError("LoginSession requires created_at and last_accessed_at")
        return cls(
            session_id_hash=str(data["session_id_hash"]),
            user_sub=str(data["user_sub"]),
            auth_method=AuthMethod(data["auth_method"]),
            created_at=created_at,
            last_accessed_at=last_accessed_at,
            ttl=int(data["ttl"]),
            roles=frozenset(Role(v) for v in data.get("roles") or ()),
            group_ids=frozenset(str(v) for v in data.get("group_ids") or ()),
            display_name=data.get("display_name"),
            email=data.get("email"),
            access_token_enc=data.get("access_token_enc"),
            refresh_token_enc=data.get("refresh_token_enc"),
            key_version=data.get("key_version"),
            access_expires_at=parse_time("access_expires_at"),
            refresh_failing_since=parse_time("refresh_failing_since"),
            version=int(data.get("version", 1)),
        )


class SessionStore(Protocol):
    """Storage port shared by the DynamoDB and the in-process session stores.

    Defined here (Models, standard library only) because the two Service
    implementations must not import each other. Conditional writes report a
    lost race by returning ``False``; implementations raise their own
    exception when the storage itself is unreachable.
    """

    def get(self, session_id_hash: str) -> LoginSession | None:
        """Read with strong consistency; ``None`` when absent."""
        ...

    def put(self, session: LoginSession) -> None:
        """Create (or replace) the whole item."""
        ...

    def touch(self, session: LoginSession) -> None:
        """Write ``last_accessed_at`` only, and only if the item still exists."""
        ...

    def update_versioned(self, session: LoginSession, expected_version: int) -> bool:
        """Write the versioned attributes if the stored version matches."""
        ...

    def delete(self, session_id_hash: str, expected_version: int | None = None) -> bool:
        """Delete, optionally only if the stored version matches."""
        ...
