"""The authorization decision (ADR-005, contract C2).

Every "may this user ..." question is answered here and nowhere else (NFR1.1).
The functions are pure: they look only at the :class:`Principal`, the
:class:`AccessTarget` and the permission table of ``src/models/authz.py``, and
never call DynamoDB or Cognito (NFR9.13). Reading the targets is
``ResourceLookup``'s job.

Rules (``functional-design/rules.md``):

- BR2.1: permissions come from the roles; no permission at all (no role, or
  only unknown role names) and an empty ``sub`` allow nothing (NFR1.7).
- BR2.2: ``require`` checks an operation permission. Admin-only screens and
  APIs pass ``hidden_as`` so a member gets the same 404 as for a missing ID.
- BR2.3 / BR2.4: read = owner, shared department, or ``resource:read_all``;
  manage = owner or ``resource:manage_all``. A ``None`` / empty owner matches
  nobody.
- BR2.6: organization assets are used by in-scope departments or
  ``settings:manage``; their owner is not looked at.
- BR2.7: unreadable -> the caller's resource-specific NOT_FOUND (same as a
  missing ID); readable but not manageable -> ``ACCESS_DENIED`` (403).
- BR2.13: every denial writes one ``authz.denied`` line (reason,
  resource_type, sub; never the resource ID) through :func:`_deny`.
"""

from typing import NoReturn

from ...models.authz import (
    AccessTarget,
    Permission,
    Principal,
    ResourceType,
    permissions_of,
)
from ...models.errors import AccessDeniedError, CodedError, ErrorCode
from ..shared.structured_log import log_event

#: Names of the decision calls. The Manager classification check
#: (``tests/unit/test_authz_classification.py``) looks for these.
DECISION_FUNCTIONS: frozenset[str] = frozenset(
    {
        "require",
        "ensure_readable",
        "ensure_manageable",
        "can_read",
        "can_manage",
        "can_use_asset",
        "shareable_groups",
        "is_owner",
        "ensure_owner",
    }
)


def _deny(
    reason: str,
    principal: Principal,
    resource_type: ResourceType | None,
    error: CodedError,
) -> NoReturn:
    """Log the denial (BR2.13) and raise ``error``.

    Logging is a local write, not an external call, so the decision stays pure.
    """
    log_event(
        "authz.denied",
        reason=reason,
        resource_type=resource_type.value if resource_type else None,
        sub=principal.sub,
    )
    raise error


class AccessPolicy:
    """Stateless authorization decisions. All methods are static."""

    @staticmethod
    def _allowed(principal: Principal) -> frozenset[Permission]:
        """Permissions of ``principal``; empty when there is no ``sub``."""
        if not principal.sub:
            return frozenset()
        return permissions_of(principal.roles)

    @staticmethod
    def is_owner(principal: Principal, owner_sub: str | None) -> bool:
        """Whether ``principal`` owns something owned by ``owner_sub``.

        Both sides must be non-empty and equal (NFR1.7), and the principal must
        hold at least one permission.
        """
        if not AccessPolicy._allowed(principal) or not owner_sub:
            return False
        return owner_sub == principal.sub

    @staticmethod
    def ensure_owner(
        principal: Principal,
        owner_sub: str | None,
        not_found: ErrorCode,
        *,
        resource_type: ResourceType | None = None,
    ) -> None:
        """Raise ``not_found`` unless ``principal`` owns it (owner-only things).

        For things only the owner may use, even with ``resource:read_all``
        (an interview session, BR3.2). A missing thing is passed as
        ``owner_sub=None`` and gets the same answer and the same one
        ``authz.denied`` line as someone else's (NFR1.4, BR2.13).

        Args:
            principal: The user.
            owner_sub: Owner of the thing; ``None`` when it does not exist.
            not_found: The caller's resource-specific NOT_FOUND code.
            resource_type: For the denial log only.

        Raises:
            CodedError: ``not_found`` when ``principal`` is not the owner.
        """
        if AccessPolicy.is_owner(principal, owner_sub):
            return
        _deny(
            "not_found",
            principal,
            resource_type,
            CodedError("caller is not the owner", code=not_found),
        )

    @staticmethod
    def require(
        principal: Principal,
        permission: Permission,
        *,
        hidden_as: ErrorCode | None = None,
        resource_type: ResourceType | None = None,
    ) -> None:
        """Check an operation permission (BR2.2).

        Args:
            principal: The user.
            permission: The permission the operation needs.
            hidden_as: For admin-only screens and APIs, the NOT_FOUND code to
                answer instead of 403, so the operation's existence is hidden.
            resource_type: For the denial log only.

        Raises:
            CodedError: ``hidden_as`` (404) when given and not permitted.
            AccessDeniedError: ``ACCESS_DENIED`` (403) when not permitted.
        """
        if permission in AccessPolicy._allowed(principal):
            return
        if hidden_as is not None:
            _deny(
                "hidden",
                principal,
                resource_type,
                CodedError("operation hidden from this user", code=hidden_as),
            )
        _deny(
            "denied",
            principal,
            resource_type,
            AccessDeniedError(f"missing permission {permission.value}"),
        )

    @staticmethod
    def can_read(principal: Principal, target: AccessTarget) -> bool:
        """Owner, a shared department, or ``resource:read_all`` (BR2.3)."""
        allowed = AccessPolicy._allowed(principal)
        if not target.exists or not allowed:
            return False
        return (
            AccessPolicy.is_owner(principal, target.owner_sub)
            or bool(target.shared_group_ids & principal.group_ids)
            or Permission.RESOURCE_READ_ALL in allowed
        )

    @staticmethod
    def can_manage(principal: Principal, target: AccessTarget) -> bool:
        """Owner or ``resource:manage_all`` (BR2.4)."""
        allowed = AccessPolicy._allowed(principal)
        if not target.exists or not allowed:
            return False
        return (
            AccessPolicy.is_owner(principal, target.owner_sub)
            or Permission.RESOURCE_MANAGE_ALL in allowed
        )

    @staticmethod
    def can_use_asset(principal: Principal, target: AccessTarget) -> bool:
        """In-scope department or ``settings:manage`` (BR2.6). Owner ignored."""
        allowed = AccessPolicy._allowed(principal)
        if not target.exists or not allowed:
            return False
        return (
            bool(target.shared_group_ids & principal.group_ids)
            or Permission.SETTINGS_MANAGE in allowed
        )

    @staticmethod
    def shareable_groups(
        principal: Principal, all_groups: frozenset[str]
    ) -> frozenset[str]:
        """Departments ``principal`` may share with (BR2.5).

        Args:
            all_groups: IDs of the departments that exist now.
        """
        allowed = AccessPolicy._allowed(principal)
        if Permission.SHARE_ANY_GROUP in allowed:
            return frozenset(all_groups)
        if not allowed:
            return frozenset()
        return principal.group_ids & frozenset(all_groups)

    @staticmethod
    def ensure_readable(
        principal: Principal, target: AccessTarget, not_found: ErrorCode
    ) -> None:
        """Raise ``not_found`` unless ``principal`` can read ``target`` (BR2.7).

        An unreadable target gets exactly what a missing one gets (NFR1.4).
        """
        if AccessPolicy.can_read(principal, target):
            return
        _deny(
            "not_found",
            principal,
            target.resource_type,
            CodedError("target is not readable", code=not_found),
        )

    @staticmethod
    def ensure_manageable(
        principal: Principal, target: AccessTarget, not_found: ErrorCode
    ) -> None:
        """Read check first (404), then the manage check (403) (BR2.7).

        Raises:
            CodedError: ``not_found`` when the target is not readable.
            AccessDeniedError: Readable but not manageable.
        """
        AccessPolicy.ensure_readable(principal, target, not_found)
        if AccessPolicy.can_manage(principal, target):
            return
        _deny(
            "denied",
            principal,
            target.resource_type,
            AccessDeniedError("target is readable but not manageable"),
        )
