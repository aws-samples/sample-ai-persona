"""
Display values for the signed-in user in the page header (BR1.16, M1).

A template global, not business logic: it only formats the Principal that
``AuthMiddleware`` put on ``request.state.principal``. In U1 the department
names are the Cognito group names as-is.
"""

from dataclasses import dataclass
from typing import Any

from src.models.authz import Permission, Principal, Role, permissions_of

MAX_SHOWN_DEPARTMENTS = 2
NO_ROLE_LABEL = "ロールなし"
NO_DEPARTMENT_LABEL = "所属グループなし"
_SEPARATOR = "、"


@dataclass(frozen=True)
class UserBadge:
    """What the header shows for the signed-in user."""

    name: str
    role_label: str
    shown_departments: tuple[str, ...]
    all_departments: tuple[str, ...]
    hidden_count: int
    #: U7 BR3.2: show the "設定" link (``settings:manage``, never the role name).
    can_manage_settings: bool = False

    @property
    def departments_label(self) -> str:
        """Up to two departments, or the no-department label."""
        if not self.shown_departments:
            return NO_DEPARTMENT_LABEL
        return _SEPARATOR.join(self.shown_departments)


def build_user_badge(principal: Principal | None) -> UserBadge | None:
    """Format a Principal for the header; ``None`` when nobody is signed in."""
    if principal is None:
        return None
    if Role.ADMIN in principal.roles:
        role_label = Role.ADMIN.value
    elif Role.MEMBER in principal.roles:
        role_label = Role.MEMBER.value
    else:
        role_label = NO_ROLE_LABEL
    departments = tuple(sorted(principal.group_ids))
    shown = departments[:MAX_SHOWN_DEPARTMENTS]
    return UserBadge(
        name=principal.display_name or principal.email or "",
        role_label=role_label,
        shown_departments=shown,
        all_departments=departments,
        hidden_count=len(departments) - len(shown),
        can_manage_settings=bool(principal.sub)
        and Permission.SETTINGS_MANAGE in permissions_of(principal.roles),
    )


def user_badge(request: Any) -> UserBadge | None:
    """Template global: the badge for ``request.state.principal``, if any."""
    principal = getattr(getattr(request, "state", None), "principal", None)
    return build_user_badge(principal if isinstance(principal, Principal) else None)
