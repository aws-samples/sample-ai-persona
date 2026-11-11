"""Which v0 users are re-created in the v1 user pool.

Passwords cannot be exported, so a v0 user is created again in the v1 pool
with an invitation email and the ``role:member`` group. A v1 user with the same
email (the first admin, or anyone created by hand) is left exactly as it is:
no new user and no group change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str | None) -> str:
    """Strip and lower-case an email (the comparison key)."""
    return (email or "").strip().lower()


@dataclass(frozen=True)
class V0User:
    """A user of the v0 pool.

    Attributes:
        username: The v0 ``Username`` (for the report).
        email: The ``email`` attribute, if any.
        enabled: Whether the user may sign in.
    """

    username: str
    email: str | None
    enabled: bool = True


@dataclass
class UserPlan:
    """What the ``users`` step does.

    Attributes:
        create: Normalized emails to create in v1.
        existing: Normalized emails that already exist in v1 (left alone).
        invalid: ``(username, reason)`` of v0 users that are not created.
    """

    create: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    invalid: list[tuple[str, str]] = field(default_factory=list)


def plan_users(v0_users: Iterable[V0User], v1_emails: set[str]) -> UserPlan:
    """Split the v0 users into created, existing and invalid ones.

    Args:
        v0_users: Users of the v0 pool.
        v1_emails: Normalized emails of the users already in the v1 pool.
    """
    plan = UserPlan()
    seen: set[str] = set()
    for user in v0_users:
        email = normalize_email(user.email)
        if not email:
            plan.invalid.append((user.username, "email_missing"))
        elif not _EMAIL.match(email):
            plan.invalid.append((user.username, "email_invalid"))
        elif email in seen:
            plan.invalid.append((user.username, "email_duplicate"))
        elif not user.enabled:
            seen.add(email)
            plan.invalid.append((user.username, "disabled"))
        elif email in v1_emails:
            seen.add(email)
            plan.existing.append(email)
        else:
            seen.add(email)
            plan.create.append(email)
    return plan
