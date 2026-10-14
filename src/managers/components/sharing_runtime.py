"""One set of sharing parts per task (U4).

``ResourceSharing`` keeps the ``all`` count for 60 seconds, ``DepartmentDirectory``
and ``OwnerDirectory`` keep their copies for five minutes. Those caches are per
task (``performance-design.md`` §3), so every Manager that shares resources
(``SharingManager``, ``PersonaManager``; U6 / U7 add theirs) must use the same
instances. This module holds them.

The parts are built from injected sources: the Managers pass a ``build``
callable that reads the services from ``service_factory``; this module never
reaches for the factory itself (``.claude/rules/architecture.md``, Component).

U5 adds a second set, :class:`AccessParts`: the one ``ResourceLookup`` (with
the persona, dataset and knowledge-base readers registered, contract C3) and
the one ``FileAttachments`` that ``FileManager`` and the parents' Managers
share. U6 / U7 register their parent readers in :func:`build_access_parts`.
"""

import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ...config import config
from ...models.authz import LookupReader, ResourceType
from ...services.authz_lookup_service import LookupTable
from .department_directory import DepartmentDirectory, DepartmentSource
from .file_attachments import FileAttachments, FileMetadataStore, ObjectTags
from .owner_directory import OwnerDirectory, UserSource
from .resource_lookup import ResourceLookup, build_resource_lookup
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
    global _parts, _access_parts
    with _lock:
        _parts = None
        _access_parts = None


# --- U5: decision reads and file attachments --------------------------------------


class LookupReaders(Protocol):
    """What :func:`build_access_parts` needs from ``AuthzLookupService``."""

    def reader(self, table: LookupTable) -> LookupReader: ...


@dataclass(frozen=True)
class AccessParts:
    """The per-task decision-read and attachment parts (U5).

    Attributes:
        lookup: Reads the decision columns of personas, datasets and
            knowledge bases (contract C3).
        files: Attaches uploads and decides who may read them.
    """

    lookup: ResourceLookup
    files: FileAttachments


_access_parts: AccessParts | None = None


def lookup_tables() -> list[LookupTable]:
    """The resource types U5 registers with ``ResourceLookup`` (C3).

    Datasets and knowledge bases have no ``shared_group_ids`` column until U7,
    so they read as unscoped organization assets (admin only, BR2.3).
    """
    return [
        LookupTable(ResourceType.PERSONA, config.table_name("Personas"), "name"),
        LookupTable(ResourceType.DATASET, config.table_name("Datasets"), "name"),
        LookupTable(
            ResourceType.KNOWLEDGE_BASE, config.table_name("KnowledgeBases"), "name"
        ),
    ]


def build_access_parts(
    lookup_service: LookupReaders,
    file_store: FileMetadataStore,
    tags: ObjectTags | None,
    **file_options: Any,
) -> AccessParts:
    """Assemble the lookup (with its readers) and the attachment component.

    Args:
        lookup_service: Builds one reader per table (``AuthzLookupService``).
        file_store: Upload metadata (``UploadedFileStore``).
        tags: Object tags (``S3Service``), or ``None`` without S3.
        **file_options: Passed to :class:`FileAttachments` (tests: ``clock``).
    """
    lookup = build_resource_lookup(
        (table.resource_type, lookup_service.reader(table)) for table in lookup_tables()
    )
    files = FileAttachments(file_store, tags, **file_options)
    # U6: files.register_parent("discussion", ...)
    # U7: files.register_parent("survey_template", ...)
    return AccessParts(lookup=lookup, files=files)


def shared_access_parts(build: Callable[[], AccessParts]) -> AccessParts:
    """The access parts of this task; ``build`` runs only for the first caller."""
    global _access_parts
    if _access_parts is None:
        with _lock:
            if _access_parts is None:
                _access_parts = build()
    return _access_parts
