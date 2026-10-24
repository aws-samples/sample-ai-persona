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
from ...models.custom_persona_data import NAME_RESERVATION_PREFIX
from ...services.authz_lookup_service import LookupTable
from .access_policy import AccessPolicy
from .department_directory import DepartmentDirectory, DepartmentSource
from .discussion_records import DiscussionRecords, DiscussionStorePort
from .file_attachments import FileAttachments, FileMetadataStore, ObjectTags
from .participant_reader import ParticipantReader, PersonaBodies
from .owner_directory import OwnerDirectory, UserSource
from .resource_lookup import ResourceLookup, build_resource_lookup
from .resource_sharing import ResourceSharing, ShareStore, default_sharing_tables


@dataclass(frozen=True)
class SharingParts:
    """The per-task sharing parts.

    Attributes:
        sharing: Sharing decisions and tab queries.
        owners: The owner-name copy (display only).
        departments: The department copy (U7: the usage-scope choices, C4).
    """

    sharing: ResourceSharing
    owners: OwnerDirectory
    departments: DepartmentDirectory


_parts: SharingParts | None = None
_lock = threading.Lock()


def build_sharing_parts(
    share_store: ShareStore,
    department_source: DepartmentSource,
    user_source: UserSource,
) -> SharingParts:
    """Assemble the parts and register every shareable type."""
    departments = DepartmentDirectory(department_source)
    sharing = ResourceSharing(share_store, departments)
    for table in default_sharing_tables():
        sharing.register(table)
    return SharingParts(
        sharing=sharing, owners=OwnerDirectory(user_source), departments=departments
    )


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
    global _parts, _access_parts, _discussion_parts
    with _lock:
        _parts = None
        _access_parts = None
        _discussion_parts = None


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
    """The resource types registered with ``ResourceLookup`` (C3).

    Datasets, knowledge bases and custom persona data are organization assets:
    their ``shared_group_ids`` is the usage scope (U7 BR5.7); an asset without
    the column is admin only.
    """
    return [
        LookupTable(ResourceType.PERSONA, config.table_name("Personas"), "name"),
        # U6: 議論（表示名は topic。所有者のない古い議論は owner_sub が空）
        LookupTable(ResourceType.DISCUSSION, config.table_name("Discussions"), "topic"),
        LookupTable(ResourceType.DATASET, config.table_name("Datasets"), "name"),
        LookupTable(
            ResourceType.KNOWLEDGE_BASE, config.table_name("KnowledgeBases"), "name"
        ),
        # U7: テンプレートと結果（所有者のない古い行は owner_sub が空、BR1.6）
        LookupTable(
            ResourceType.SURVEY_TEMPLATE, config.table_name("SurveyTemplates"), "name"
        ),
        LookupTable(ResourceType.SURVEY, config.table_name("Surveys"), "name"),
        # U7: カスタムペルソナデータ（利用範囲だけ。名前の押さえ name# は読まない）
        LookupTable(
            ResourceType.CUSTOM_PERSONA_DATA,
            config.CUSTOM_PERSONA_DATA_TABLE_NAME,
            "name",
            hidden_id_prefix=NAME_RESERVATION_PREFIX,
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
    # U6 BR5.1: 議論を親とする資料は、議論を読める人に配信する
    files.register_parent(
        "discussion",
        lambda principal, discussion_id: AccessPolicy.can_read(
            principal, lookup.get(ResourceType.DISCUSSION, discussion_id)
        ),
    )
    # U7 BR1.9: テンプレートを親とする画像は、テンプレートを読める人に配信する
    files.register_parent(
        "survey_template",
        lambda principal, template_id: AccessPolicy.can_read(
            principal, lookup.get(ResourceType.SURVEY_TEMPLATE, template_id)
        ),
    )
    return AccessParts(lookup=lookup, files=files)


def shared_access_parts(build: Callable[[], AccessParts]) -> AccessParts:
    """The access parts of this task; ``build`` runs only for the first caller."""
    global _access_parts
    if _access_parts is None:
        with _lock:
            if _access_parts is None:
                _access_parts = build()
    return _access_parts


# --- U6: discussions -------------------------------------------------------------


@dataclass(frozen=True)
class DiscussionParts:
    """The per-task discussion parts (U6, ADR-006).

    Attributes:
        records: Saves, reads and deletes discussions (the four Managers).
        participants: Decision-first reads of participant personas.
    """

    records: DiscussionRecords
    participants: ParticipantReader


_discussion_parts: DiscussionParts | None = None


def build_discussion_parts(
    store: DiscussionStorePort,
    persona_bodies: PersonaBodies,
    sharing: ResourceSharing,
    access: AccessParts,
) -> DiscussionParts:
    """Assemble ``DiscussionRecords`` and ``ParticipantReader`` from shared parts.

    Args:
        store: ``DiscussionStore``.
        persona_bodies: Batch reads of persona bodies (``PersonaStore``).
        sharing: The per-task ``ResourceSharing``.
        access: The per-task lookup and file attachments.
    """
    participants = ParticipantReader(access.lookup, persona_bodies)
    records = DiscussionRecords(store, sharing, access.files, participants)
    return DiscussionParts(records=records, participants=participants)


def shared_discussion_parts(build: Callable[[], DiscussionParts]) -> DiscussionParts:
    """The discussion parts of this task; ``build`` runs only for the first caller."""
    global _discussion_parts
    if _discussion_parts is None:
        with _lock:
            if _discussion_parts is None:
                _discussion_parts = build()
    return _discussion_parts


class PartsSources(Protocol):
    """The services the per-task parts are built from (``service_factory``).

    The Managers pass the factory in; this module never imports it.
    """

    def get_authz_lookup_service(self) -> Any: ...

    def get_uploaded_file_store(self) -> Any: ...

    def get_optional_s3_service(self) -> Any: ...

    def get_resource_share_store(self) -> Any: ...

    def get_department_source(self) -> Any: ...

    def get_user_source(self) -> Any: ...

    def get_discussion_store(self) -> Any: ...

    def get_persona_store(self) -> Any: ...


def access_parts_from(sources: PartsSources) -> AccessParts:
    """The per-task access parts, built from ``sources`` on first use."""
    return shared_access_parts(
        lambda: build_access_parts(
            sources.get_authz_lookup_service(),
            sources.get_uploaded_file_store(),
            sources.get_optional_s3_service(),
        )
    )


def sharing_parts_from(sources: PartsSources) -> SharingParts:
    """The per-task sharing parts, built from ``sources`` on first use."""
    return shared_parts(
        lambda: build_sharing_parts(
            sources.get_resource_share_store(),
            sources.get_department_source(),
            sources.get_user_source(),
        )
    )


def discussion_parts_from(sources: PartsSources) -> DiscussionParts:
    """The per-task discussion parts (and the parts they use) from ``sources``."""
    access = access_parts_from(sources)
    sharing = sharing_parts_from(sources)
    return shared_discussion_parts(
        lambda: build_discussion_parts(
            sources.get_discussion_store(),
            sources.get_persona_store(),
            sharing.sharing,
            access,
        )
    )
