"""One set of sharing parts per task (U4).

``ResourceSharing`` keeps the ``all`` count for 60 seconds, ``DepartmentDirectory``
and ``OwnerDirectory`` keep their copies for five minutes. Those caches are per
task (``performance-design.md`` §3), so every Manager that shares resources
(``SharingManager``, ``PersonaManager``; U6 / U7 add theirs) must use the same
instances. This module holds them.

The parts are built from injected sources: the Managers pass a ``build``
callable that reads the services from ``service_factory``; this module never
reaches for the factory itself (``.claude/rules/architecture.md``, Component).
"""

import threading
from dataclasses import dataclass
from typing import Callable

from .department_directory import DepartmentDirectory, DepartmentSource
from .owner_directory import OwnerDirectory, UserSource
from .resource_sharing import ResourceSharing, ShareStore, default_sharing_tables


@dataclass(frozen=True)
class SharingParts:
    """The per-task sharing parts.

    Attributes:
        sharing: Sharing decisions and tab queries.
        owners: The owner-name copy (display only).
    """

    sharing: ResourceSharing
    owners: OwnerDirectory


_parts: SharingParts | None = None
_lock = threading.Lock()


def build_sharing_parts(
    share_store: ShareStore,
    department_source: DepartmentSource,
    user_source: UserSource,
) -> SharingParts:
    """Assemble the parts and register every shareable type."""
    sharing = ResourceSharing(share_store, DepartmentDirectory(department_source))
    for table in default_sharing_tables():
        sharing.register(table)
    return SharingParts(sharing=sharing, owners=OwnerDirectory(user_source))


def shared_parts(build: Callable[[], SharingParts]) -> SharingParts:
    """The parts of this task; ``build`` runs only for the first caller."""
    global _parts
    if _parts is None:
        with _lock:
            if _parts is None:
                _parts = build()
    return _parts


def reset_shared_parts() -> None:
    """Forget the parts (tests isolate their caches with this)."""
    global _parts
    with _lock:
        _parts = None
