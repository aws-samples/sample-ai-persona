"""
Persona Manager for AI Persona System.
ペルソナのCRUD、バリデーション、KB/Datasetバインディング管理を担当する。
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    TypeVar,
)

from ..config import config
from ..models.authz import AccessTarget, Permission, Principal, ResourceType
from ..models.dataset import Dataset
from ..models.errors import AccessDeniedError, CodedError, ErrorCode
from ..models.knowledge_base import KnowledgeBase
from ..models.persona import Persona
from ..models.persona_binding import (
    KB_TARGET_KEY,
    BindingTargetType,
    PersonaBinding,
    dataset_target_key,
)
from ..models.persona_candidate import (
    BehaviorDatasetCandidate,
    CandidateClaim,
    CandidateSaveResult,
    PersonaCandidate,
)
from ..models.demographics import VALID_GENDERS
from ..models.sharing import ListTab, TabbedList
from ..services import country_service
from ..services.database_service import DatabaseService, DatabaseError
from ..services.service_factory import service_factory
from .components.access_policy import AccessPolicy
from .components.owner_directory import OWNER_UNKNOWN, OwnerDirectory
from .components.resource_lookup import ResourceLookup
from .components.resource_sharing import ResourceSharing
from .components.sharing_runtime import (
    SharingParts,
    build_access_parts,
    build_sharing_parts,
    shared_access_parts,
    shared_parts,
)
from .shared.persona_export import export_filename, render_persona_markdown
from .shared.structured_log import log_event
from .shared.image_normalize import (
    AVATAR_FILE_EXTENSION,
    AVATAR_MIME_TYPE,
    AvatarImageError,
    AvatarImageTooLargeError,
    normalize_avatar_image,
)

if TYPE_CHECKING:
    from ..services.persona_binding_store import PersonaBindingStore
    from ..services.persona_candidate_store import PersonaCandidateStore
    from ..services.s3_service import S3Service


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


#: S3 key prefix for avatar images: persona_avatars/{persona_id}/{uuid}.webp
AVATAR_KEY_PREFIX = "persona_avatars"
#: Lifetime of the presigned URL the avatar endpoint redirects to
AVATAR_URL_EXPIRATION_SECONDS = 3600
#: Most rows a search returns (BR3.8, unchanged from before v1.0.0)
SEARCH_RESULT_LIMIT = 100

_PERSONA = ResourceType.PERSONA
_T = TypeVar("_T")


class PersonaManagerError(CodedError):
    """Custom exception for persona manager related errors."""

    pass


@dataclass(frozen=True)
class PersonaDetail:
    """What the detail page shows (BR6.2, BR6.4).

    Attributes:
        persona: The persona (read with a strongly consistent read).
        manageable: Whether the user may edit, delete and share it.
        owner_label: The owner's name, ``所有者不明``, or ``None`` when the
            owner copy is unavailable (shown as ``-``).
        shared_with: Names of the departments it is shared with.
    """

    persona: Persona
    manageable: bool
    owner_label: str | None = None
    shared_with: list[str] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class BindingRow:
    """One binding as the detail page shows it (BR2.7, BR2.8).

    Attributes:
        target_id: The dataset or knowledge-base ID.
        name: The target's name.
        usable: Whether the viewer may use the target (``利用権限なし`` when
            not); persona-dedicated datasets are always usable.
        dedicated: A persona-dedicated dataset.
        binding_keys: Dataset rows narrowed to this persona.
        metadata_filters: Knowledge-base search filters.
    """

    target_id: str
    name: str
    usable: bool
    dedicated: bool = False
    binding_keys: Dict[str, str] = dataclass_field(default_factory=dict)
    metadata_filters: Dict[str, str] = dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class KbBindingView:
    """The knowledge-base binding of a persona (BR2.1, BR4.2).

    Attributes:
        persona_id: The persona.
        manageable: Whether the viewer may change the binding.
        binding: The current binding, or ``None``.
        choices: Knowledge bases the viewer may bind (managers only).
    """

    persona_id: str
    manageable: bool
    binding: Optional[BindingRow]
    choices: List[KnowledgeBase] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class DatasetBindingsView:
    """The dataset bindings of a persona (BR2.1, BR4.2).

    Attributes:
        persona_id: The persona.
        manageable: Whether the viewer may add or remove bindings.
        bindings: The current bindings (vanished targets left out).
        choices: Organization datasets the viewer may bind, not yet bound.
    """

    persona_id: str
    manageable: bool
    bindings: List[BindingRow]
    choices: List[Dataset] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class PersonaExport:
    """A persona as a Markdown file (entities.md PersonaExport, BR7.1).

    Attributes:
        filename: ``{name}.md`` with unusable characters replaced.
        body: The Markdown document.
    """

    filename: str
    body: str


#: Deadline of the long-term memory deletion when a persona is deleted.
MEMORY_DELETE_DEADLINE_SECONDS = 30.0
#: Key prefix of persona-dedicated datasets: persona_datasets/{persona}/{dataset}.csv
DEDICATED_DATASET_PREFIX = "persona_datasets"

_UNSET: Any = object()


class PersonaManager:
    """
    ペルソナのCRUD操作とバインディング管理を行うManager。
    記憶管理はPersonaMemoryManagerに委譲済み。

    v1.0.0 U4: 保存した人を所有者にし（BR6.1）、一覧・検索・詳細・編集・削除は
    利用者（Principal）を受け取って ResourceSharing と AccessPolicy で判定する。

    v1.0.0 U5: 紐付け（PersonaBindings）・プレビュー・候補からの保存・削除の後始末・
    エクスポート・アバターの判定を持つ。
    """

    def __init__(
        self,
        database_service: Optional[DatabaseService] = None,
        s3_service: Optional["S3Service"] = None,
        resource_sharing: Optional[ResourceSharing] = None,
        owners: Optional[OwnerDirectory] = None,
        binding_store: Optional["PersonaBindingStore"] = None,
        resource_lookup: Optional[ResourceLookup] = None,
        memory_service: Any = _UNSET,
        preview_service: Any = None,
        candidate_store: Optional["PersonaCandidateStore"] = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        """
        Args:
            database_service: Database service instance for persistence (optional, uses singleton if not provided)
            s3_service: S3 service for avatar images (optional, uses singleton if not provided;
                None when S3_BUCKET_NAME is not configured, which disables avatar upload)
            resource_sharing: Sharing component (optional; the per-task
                instance shared with the other Managers, built on first use)
            owners: Owner-name copy (optional; the per-task instance)
            binding_store: ``PersonaBindings`` (optional, built on first use)
            resource_lookup: Decision reads of datasets and knowledge bases
                (optional; the per-task instance)
            memory_service: Long-term memory; ``None`` when the feature is
                off (optional, from the factory when omitted)
            preview_service: Runs dataset previews (``SurveyBatchService``;
                optional, built on first use)
            candidate_store: ``PersonaCandidates``; each candidate's hold is
                confirmed right before it is saved (R-04; optional, built on
                first use)
            clock: UTC clock for the hold checks (tests pass a fake one)
        """
        self.logger = logging.getLogger(__name__)
        self.database_service = (
            database_service or service_factory.get_database_service()
        )
        if s3_service is not None:
            self.s3_service: Optional["S3Service"] = s3_service
        else:
            try:
                self.s3_service = service_factory.get_s3_service()
            except RuntimeError:
                self.s3_service = None
        self._sharing = resource_sharing
        self._owner_directory = owners
        self._binding_store = binding_store
        self._lookup = resource_lookup
        self._memory = memory_service
        self._preview = preview_service
        self._candidate_store = candidate_store
        self._clock = clock

    @property
    def _candidates(self) -> "PersonaCandidateStore":
        if self._candidate_store is None:
            self._candidate_store = service_factory.get_persona_candidate_store()
        return self._candidate_store

    @property
    def _bindings(self) -> "PersonaBindingStore":
        if self._binding_store is None:
            self._binding_store = service_factory.get_persona_binding_store()
        return self._binding_store

    @property
    def _resource_lookup(self) -> ResourceLookup:
        if self._lookup is None:
            self._lookup = shared_access_parts(
                lambda: build_access_parts(
                    service_factory.get_authz_lookup_service(),
                    service_factory.get_uploaded_file_store(),
                    service_factory.get_optional_s3_service(),
                )
            ).lookup
        return self._lookup

    @property
    def _memory_service(self) -> Any:
        if self._memory is _UNSET:
            self._memory = service_factory.get_memory_service()
        return self._memory

    @property
    def _preview_service(self) -> Any:
        if self._preview is None:
            self._preview = service_factory.get_survey_batch_service()
        return self._preview

    # ------------------------------------------------------------------
    # Sharing parts (built lazily so that callers that never list or share
    # do not create the DynamoDB and Cognito clients)
    # ------------------------------------------------------------------

    @staticmethod
    def _parts() -> SharingParts:
        return shared_parts(
            lambda: build_sharing_parts(
                service_factory.get_resource_share_store(),
                service_factory.get_department_source(),
                service_factory.get_user_source(),
            )
        )

    @property
    def _resource_sharing(self) -> ResourceSharing:
        if self._sharing is None:
            self._sharing = self._parts().sharing
        return self._sharing

    @property
    def _owners(self) -> OwnerDirectory:
        if self._owner_directory is None:
            self._owner_directory = self._parts().owners
        return self._owner_directory

    def save_persona(self, principal: Principal, persona: Persona) -> str:
        """
        Save a new persona owned by ``principal`` (BR6.1).

        The persona starts unshared; ``owner_sub`` never changes afterwards.

        Args:
            principal: The user who saves it (needs ``resource:create``)
            persona: Persona object to save

        Returns:
            str: The persona ID

        Raises:
            AccessDeniedError: The user may not create resources
            PersonaManagerError: If save operation fails
        """
        AccessPolicy.require(
            principal, Permission.RESOURCE_CREATE, resource_type=_PERSONA
        )
        if not persona:
            raise PersonaManagerError(
                "persona object is falsy", code=ErrorCode.PERSONA_INVALID
            )

        # Validate persona before saving
        self._validate_persona_for_save(persona)
        persona = persona.owned_by(principal.sub)

        try:
            persona_id = self.database_service.save_persona(persona)
            self.logger.info(
                f"Persona saved successfully: {persona.name} (ID: {persona_id})"
            )
            return persona_id

        except DatabaseError as e:
            self.logger.error("Database error while saving persona", exc_info=True)
            raise PersonaManagerError(
                f"persona save failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e
        except Exception as e:
            self.logger.error("Unexpected error while saving persona", exc_info=True)
            raise PersonaManagerError(
                f"persona save failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

    def get_persona(self, principal: Principal, persona_id: str) -> Persona:
        """
        Read one persona the user may read (BR4.5).

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or missing
            PersonaManagerError: When the persona cannot be read
        """
        persona = self._read_consistent(persona_id)
        AccessPolicy.ensure_readable(
            principal, self._target_of(persona, persona_id), ErrorCode.PERSONA_NOT_FOUND
        )
        assert persona is not None
        return persona

    def get_persona_unchecked(self, persona_id: str) -> Optional[Persona]:
        """
        Retrieve a persona by ID without an authorization decision.

        Discussions and interviews still read personas this way; U6 moves
        those callers to :meth:`get_persona` (classified ``pending("U6")``).

        Args:
            persona_id: ID of the persona to retrieve

        Returns:
            Persona object if found, None otherwise

        Raises:
            PersonaManagerError: If retrieval operation fails
        """
        if not persona_id or not persona_id.strip():
            raise PersonaManagerError(
                "persona id is blank", code=ErrorCode.PERSONA_ID_INVALID
            )

        try:
            persona = self.database_service.get_persona(persona_id.strip())
            if persona:
                self.logger.debug(
                    f"Persona retrieved successfully: {persona.name} (ID: {persona_id})"
                )
            else:
                self.logger.debug(f"Persona not found: {persona_id}")
            return persona

        except DatabaseError as e:
            self.logger.error("Database error while retrieving persona", exc_info=True)
            raise PersonaManagerError(
                f"persona retrieval failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e
        except Exception as e:
            self.logger.error(
                "Unexpected error while retrieving persona", exc_info=True
            )
            raise PersonaManagerError(
                f"persona retrieval failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

    # ------------------------------------------------------------------
    # Tabbed lists, search, detail (BR3, BR6.2〜BR6.4)
    # ------------------------------------------------------------------

    def list_personas(
        self, principal: Principal, tab: str | None, cursor: str | None
    ) -> TabbedList[Persona]:
        """
        One page of a tab, newest first (``all`` is in scan order).

        ``all`` without ``resource:read_all`` (and an unknown tab) is
        ``mine`` (BR3.1). The rows are read again with a strongly consistent
        batch read and rows the user can no longer read are left out (BR3.7).
        Counts are only computed for the first page.

        Raises:
            CodedError: ``LOOKUP_UNAVAILABLE`` when the index cannot be read
            PersonaManagerError: When the bodies cannot be read
        """
        active = self._resource_sharing.effective_tab(
            principal, ListTab.parse(tab) or ListTab.MINE
        )
        page = self._resource_sharing.list_page(principal, _PERSONA, active, cursor)
        personas = self._readable(principal, [t.resource_id for t in page.items])
        counts = self._counts(principal, {active: page.total}) if cursor is None else {}
        return self._listing(
            principal, active, personas, next_cursor=page.next_cursor, counts=counts
        )

    def search_personas(
        self, principal: Principal, tab: str | None, query: str
    ) -> TabbedList[Persona]:
        """
        Search names, occupations and backgrounds inside one tab (BR3.8).

        ``mine`` / ``shared`` look at the newest 500 items of their index
        (``search_truncated`` tells when more existed); ``all`` scans the
        table (``resource:read_all`` only). At most 100 rows are returned.
        Only the searched tab is counted, from the same read (``None`` when
        the search was cut off).
        """
        active = self._resource_sharing.effective_tab(
            principal, ListTab.parse(tab) or ListTab.MINE
        )
        truncated = False
        if active is ListTab.ALL:
            scanned = self._scan_all()
            total: int | None = len(scanned)
            personas = [
                p
                for p in scanned
                if AccessPolicy.can_read(principal, self._target_of(p, p.id))
            ]
        else:
            ids, truncated = self._resource_sharing.collect_ids(
                principal, _PERSONA, active
            )
            # The index read above already holds the whole tab unless it was
            # cut off; then the count is unknown and the truncation notice
            # is shown instead (R-06: no second read for the count).
            total = None if truncated else len(ids)
            personas = self._readable(principal, ids)
        matched = [p for p in personas if _matches(p, query)][:SEARCH_RESULT_LIMIT]
        return self._listing(
            principal,
            active,
            matched,
            counts={active.value: total},
            search_truncated=truncated,
        )

    def list_accessible_personas(
        self, principal: Principal, search: str | None = None
    ) -> List[Persona]:
        """
        The user's own personas and the ones shared with them (JSON API).

        Each tab contributes its newest 500 items; ``search`` filters names and
        occupations.
        """
        ids: list[str] = []
        for tab in (ListTab.MINE, ListTab.SHARED):
            tab_ids, _ = self._resource_sharing.collect_ids(principal, _PERSONA, tab)
            ids += tab_ids
        personas = self._readable(principal, list(dict.fromkeys(ids)))
        if not search:
            return personas
        needle = search.lower()
        return [
            p
            for p in personas
            if needle in p.name.lower() or needle in p.occupation.lower()
        ]

    def get_persona_detail(
        self, principal: Principal, persona_id: str
    ) -> PersonaDetail:
        """
        The detail page of a persona the user may read (BR6.2, BR6.4).

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or missing
            PersonaManagerError: When the persona cannot be read
        """
        persona = self._read_consistent(persona_id)
        target = self._target_of(persona, persona_id)
        AccessPolicy.ensure_readable(principal, target, ErrorCode.PERSONA_NOT_FOUND)
        assert persona is not None  # ensure_readable raised for a missing persona
        return self.describe_persona(principal, persona)

    def describe_persona(self, principal: Principal, persona: Persona) -> PersonaDetail:
        """
        The detail rows (owner and shares, BR6.4) of a persona already read.

        Used by the detail page and by the response to an edit, so the
        header shows the same rows before and after saving (R-07). Nothing is
        read again: the owner name comes from the owner copy and the
        department names from the department directory (display only).

        Args:
            principal: The user (decides ``manageable``)
            persona: A persona the caller has read

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when the user cannot read it
                (the owner name is never shown for an unreadable persona)
        """
        target = self._target_of(persona, persona.id)
        AccessPolicy.ensure_readable(principal, target, ErrorCode.PERSONA_NOT_FOUND)
        labels = self._owner_labels([persona])
        return PersonaDetail(
            persona=persona,
            manageable=AccessPolicy.can_manage(principal, target),
            owner_label=None if labels is None else labels[persona.id],
            shared_with=self._resource_sharing.department_names(
                sorted(persona.shared_group_ids)
            ),
        )

    def get_persona_for_edit(self, principal: Principal, persona_id: str) -> Persona:
        """
        The persona for the edit form; the user must manage it (BR6.2).

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or missing
            AccessDeniedError: Readable but not manageable (403)
        """
        persona = self._read_consistent(persona_id)
        AccessPolicy.ensure_manageable(
            principal,
            self._target_of(persona, persona_id),
            ErrorCode.PERSONA_NOT_FOUND,
        )
        assert persona is not None
        return persona

    # --- list helpers -----------------------------------------------------

    @staticmethod
    def _target_of(persona: Persona | None, persona_id: str) -> AccessTarget:
        if persona is None:
            return AccessTarget.missing(_PERSONA, persona_id)
        return AccessTarget(
            resource_type=_PERSONA,
            resource_id=persona.id,
            owner_sub=persona.owner_sub,
            shared_group_ids=persona.shared_group_ids,
            display_name=persona.name,
        )

    def _read_consistent(self, persona_id: str) -> Optional[Persona]:
        if not persona_id or not persona_id.strip():
            raise PersonaManagerError(
                "persona id is blank", code=ErrorCode.PERSONA_ID_INVALID
            )
        return self._read_bodies(
            lambda: self.database_service.get_persona(
                persona_id.strip(), consistent=True
            )
        )

    def _readable(self, principal: Principal, ids: List[str]) -> List[Persona]:
        """Bodies of ``ids`` (in order) that the user can still read (BR3.7)."""
        if not ids:
            return []
        found = self._read_bodies(
            lambda: self.database_service.get_personas_by_ids(ids)
        )
        return [
            p
            for i in ids
            if (p := found.get(i)) is not None
            and AccessPolicy.can_read(principal, self._target_of(p, i))
        ]

    def _scan_all(self) -> List[Persona]:
        return self._read_bodies(self.database_service.scan_all_personas)

    def _read_bodies(self, read: Callable[[], _T]) -> _T:
        """Run a body read; failures become ``PERSONA_OPERATION_FAILED``."""
        try:
            return read()
        except Exception as e:
            self.logger.error("Failed to read personas", exc_info=True)
            raise PersonaManagerError(
                f"persona retrieval failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

    def _counts(
        self, principal: Principal, known: Dict[ListTab, int | None]
    ) -> Dict[str, int | None]:
        """Count of every visible tab; ``None`` when it could not be read."""
        tabs = [ListTab.MINE, ListTab.SHARED]
        if self._resource_sharing.effective_tab(principal, ListTab.ALL) is ListTab.ALL:
            tabs.append(ListTab.ALL)
        counts: Dict[str, int | None] = {}
        for tab in tabs:
            if tab in known and known[tab] is not None:
                counts[tab.value] = known[tab]
            else:
                counts[tab.value] = self._resource_sharing.count(
                    principal, _PERSONA, tab
                )
        return counts

    def _owner_labels(self, personas: Iterable[Persona]) -> Dict[str, str] | None:
        """Row id -> owner label (BR5.2); ``None`` when the copy is unavailable."""
        rows = list(personas)
        subs = {p.owner_sub for p in rows if p.owner_sub}
        labels = self._owners.labels(subs)
        if labels is None:
            return None
        return {
            p.id: labels.get(p.owner_sub, OWNER_UNKNOWN)
            if p.owner_sub
            else OWNER_UNKNOWN
            for p in rows
        }

    def _listing(
        self,
        principal: Principal,
        active: ListTab,
        personas: List[Persona],
        *,
        next_cursor: str | None = None,
        counts: Dict[str, int | None],
        search_truncated: bool = False,
    ) -> TabbedList[Persona]:
        sharing = self._resource_sharing
        owners: Dict[str, str | None] = {}
        if active is not ListTab.MINE and personas:
            labels = self._owner_labels(personas)
            owners = {p.id: None if labels is None else labels[p.id] for p in personas}
        shared_with = {
            p.id: sharing.department_names(
                sorted(
                    p.shared_group_ids & principal.group_ids
                    if active is ListTab.SHARED
                    else p.shared_group_ids
                )
            )
            for p in personas
        }
        return TabbedList(
            tab=active,
            items=personas,
            next_cursor=next_cursor,
            counts=counts,
            show_all=sharing.effective_tab(principal, ListTab.ALL) is ListTab.ALL,
            owners=owners,
            shared_with=shared_with,
            manageable=frozenset(
                p.id
                for p in personas
                if AccessPolicy.can_manage(principal, self._target_of(p, p.id))
            ),
            no_department=not principal.group_ids,
            search_truncated=search_truncated,
        )

    def delete_persona(self, principal: Principal, persona_id: str) -> bool:
        """
        Delete a persona the user manages, with its department shares (BR4).

        ``ResourceSharing.delete_with_shares`` reads the persona strongly,
        judges ``ensure_manageable`` on that body and deletes it together with
        its ``ResourceShares`` items, returning the body as deleted. Its avatar
        image (the one at deletion time) is removed afterwards.

        Args:
            principal: The user
            persona_id: ID of the persona to delete

        Returns:
            True when deleted

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or already
                missing (the same answer, NFR1.14)
            AccessDeniedError: Readable but not manageable (403)
            PersonaManagerError: If deletion operation fails
        """
        if not persona_id or not persona_id.strip():
            raise PersonaManagerError(
                "persona id is blank", code=ErrorCode.PERSONA_ID_INVALID
            )

        try:
            deleted = self._resource_sharing.delete_with_shares(
                principal, _PERSONA, persona_id.strip()
            )
        except CodedError:
            raise
        except Exception as e:
            self.logger.error("Unexpected error while deleting persona", exc_info=True)
            raise PersonaManagerError(
                f"persona delete failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e
        if deleted is None:
            self.logger.info(f"Persona not found for deletion: {persona_id}")
            raise PersonaManagerError(
                "nothing to delete", code=ErrorCode.PERSONA_NOT_FOUND
            )
        self.logger.info(f"Persona deleted successfully: {persona_id}")
        self._clean_up_after_delete(persona_id.strip(), deleted)
        return True

    def _clean_up_after_delete(self, persona_id: str, deleted: Dict[str, Any]) -> None:
        """Remove what belonged to a deleted persona (BR3.1〜BR3.3).

        Bindings (after noting the dedicated datasets), long-term memory, the
        dedicated datasets and the avatar, in that order. Every step runs even
        when an earlier one failed; failures are ``persona.cleanup_failed``
        lines, never errors to the user.
        """
        dedicated: List[Dataset] = []
        bindings: Optional[List[PersonaBinding]] = None
        try:
            bindings = self._bindings.list_for_persona(persona_id)
        except Exception as e:
            self._cleanup_failed(persona_id, "bindings", e)
        if bindings is not None:
            try:
                dedicated = self._dedicated_datasets(persona_id, bindings)
            except Exception as e:
                self._cleanup_failed(persona_id, "persona_datasets", e)
            try:
                self._bindings.delete_all(persona_id, bindings)
            except Exception as e:
                self._cleanup_failed(persona_id, "bindings", e)

        memory = self._memory_service
        if memory is None and config.ENABLE_LONG_TERM_MEMORY:
            # R-03: memory is on but its service could not be built, so the
            # records stay behind; make that visible (BR3.3)
            self._cleanup_failed(persona_id, "memory", None, reason="error")
        if memory is not None:
            try:
                result = memory.delete_all(
                    persona_id,
                    deadline=time.monotonic() + MEMORY_DELETE_DEADLINE_SECONDS,
                )
                if not result.complete:
                    self._cleanup_failed(
                        persona_id,
                        "memory",
                        None,
                        reason="timeout" if result.timed_out else "error",
                        error_type=result.error_type,
                        remaining=result.remaining,
                    )
            except Exception as e:
                self._cleanup_failed(persona_id, "memory", e)

        for dataset in dedicated:
            try:
                self._delete_dedicated_dataset(dataset)
            except Exception as e:
                self._cleanup_failed(persona_id, "persona_datasets", e)

        avatar_path = deleted.get("avatar_path")
        if avatar_path:
            try:
                self._delete_avatar_or_raise(str(avatar_path))
            except Exception as e:
                self._cleanup_failed(persona_id, "avatar", e)

    def _dedicated_datasets(
        self, persona_id: str, bindings: List[PersonaBinding]
    ) -> List[Dataset]:
        """The bound datasets whose ``owner_persona_id`` is this persona (BR6.4)."""
        found: List[Dataset] = []
        for binding in bindings:
            if not binding.is_dataset:
                continue
            dataset = self.database_service.get_dataset(binding.target_id)
            if dataset is not None and dataset.is_dedicated_to(persona_id):
                found.append(dataset)
        return found

    def _delete_dedicated_dataset(self, dataset: Dataset) -> None:
        """Delete a dedicated dataset's registration and CSV (raises on failure)."""
        self.database_service.delete_dataset(dataset.id)
        if self.s3_service is not None and dataset.s3_path.startswith("s3://"):
            self.s3_service.delete_file(dataset.s3_path)

    @staticmethod
    def _cleanup_failed(
        persona_id: str,
        step: str,
        error: Optional[BaseException],
        *,
        reason: str = "error",
        error_type: Optional[str] = None,
        remaining: Optional[int] = None,
    ) -> None:
        log_event(
            "persona.cleanup_failed",
            level="ERROR",
            persona_id=persona_id,
            step=step,
            reason=reason,
            error_type=error_type or (type(error).__name__ if error else None),
            remaining=remaining,
        )

    def update_persona(
        self,
        principal: Principal,
        persona_id: str,
        name: str | None = None,
        age: int | None = None,
        occupation: str | None = None,
        background: str | None = None,
        values: List[str] | None = None,
        pain_points: List[str] | None = None,
        goals: List[str] | None = None,
        gender: str | None = None,
        country: str | None = None,
        city: str | None = None,
        tags: List[str] | None = None,
    ) -> Persona:
        """
        Edit a persona the user manages (BR6.2, BR6.7).

        The persona is read strongly and judged with ``ensure_manageable``;
        only the editable columns are written (a partial update), so the
        owner, the shares and ``ResourceShares`` never change (BR6.6).

        Args:
            principal: The user
            persona_id: ID of the persona to edit
            name: New name (optional)
            age: New age (optional)
            occupation: New occupation (optional)
            background: New background (optional)
            values: New values list (optional)
            pain_points: New pain points list (optional)
            goals: New goals list (optional)
            gender: New gender code (optional)
            country: New country code (ISO 3166-1 alpha-2, optional)
            city: New city (optional)
            tags: New filter tags list (optional)

        Returns:
            The updated Persona

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or missing
                (also when it was deleted while being edited)
            AccessDeniedError: Readable but not manageable (403)
            PersonaManagerError: If edit operation fails
        """
        existing_persona = self._read_consistent(persona_id)
        AccessPolicy.ensure_manageable(
            principal,
            self._target_of(existing_persona, persona_id),
            ErrorCode.PERSONA_NOT_FOUND,
        )
        assert existing_persona is not None

        try:
            # Create updated persona
            updated_persona = existing_persona.update(
                name=name,
                age=age,
                occupation=occupation,
                background=background,
                values=values,
                pain_points=pain_points,
                goals=goals,
                gender=gender,
                country=country,
                city=city,
                tags=tags,
            )

            # Validate updated persona
            self._validate_persona_for_save(updated_persona)

            # Save updated persona
            success = self.database_service.update_persona(updated_persona)
            if success:
                self.logger.info(
                    f"Persona edited successfully: {updated_persona.name} (ID: {persona_id})"
                )
                return updated_persona
            # The conditional write found no persona: deleted meanwhile
            raise PersonaManagerError(
                "persona deleted while editing", code=ErrorCode.PERSONA_NOT_FOUND
            )

        except PersonaManagerError:
            # Re-raise PersonaManagerError
            raise
        except Exception as e:
            self.logger.error("Unexpected error while editing persona", exc_info=True)
            raise PersonaManagerError(
                f"persona update failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

    # ------------------------------------------------------------------
    # Avatar image
    # ------------------------------------------------------------------

    def avatar_upload_enabled(self) -> bool:
        """Return True if avatar images can be stored (S3 is configured)."""
        return self.s3_service is not None

    def set_avatar(
        self, principal: Principal, persona_id: str, content: bytes
    ) -> Persona:
        """
        Normalize an uploaded image and set it as the persona's avatar (BR4.3).

        The path is written with a compare-and-set condition (see
        ``_compare_and_set_avatar``). The previous avatar object is deleted only
        after that write succeeds; if it fails, the newly uploaded object is removed.

        Args:
            principal: The user (must manage the persona)
            persona_id: ID of the persona
            content: Raw uploaded image bytes

        Returns:
            Updated Persona

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or missing
            AccessDeniedError: Readable but not manageable (403)
            PersonaManagerError: On invalid input or storage failure
        """
        s3_service = self._require_avatar_storage()
        if not content:
            raise PersonaManagerError("avatar file is empty", code=ErrorCode.FILE_EMPTY)
        max_size = config.MAX_IMAGE_SIZE
        if len(content) > max_size:
            raise PersonaManagerError(
                f"avatar size {len(content)} exceeds limit {max_size}",
                code=ErrorCode.FILE_TOO_LARGE,
                context={"max_size_mb": max_size / (1024 * 1024)},
            )
        persona = self._manageable_persona(principal, persona_id)

        try:
            normalized = normalize_avatar_image(content)
        except AvatarImageTooLargeError as e:
            self.logger.info(f"Rejected avatar image for {persona.id}: {e}")
            raise PersonaManagerError(
                "avatar image exceeds the pixel limit",
                code=ErrorCode.PERSONA_AVATAR_TOO_MANY_PIXELS,
                # 万画素単位の表示値（例: "1,600"）
                context={"max_pixels_10k": f"{e.max_pixels // 10_000:,}"},
            ) from e
        except AvatarImageError as e:
            self.logger.info(f"Rejected avatar image for {persona.id}: {e}")
            raise PersonaManagerError(
                "avatar image could not be normalized",
                code=ErrorCode.PERSONA_AVATAR_INVALID_IMAGE,
            ) from e

        s3_key = (
            f"{AVATAR_KEY_PREFIX}/{persona.id}/{uuid.uuid4()}{AVATAR_FILE_EXTENSION}"
        )
        try:
            new_path = s3_service.upload_file(
                normalized, s3_key, content_type=AVATAR_MIME_TYPE
            )
        except Exception as e:
            self.logger.error("Failed to upload avatar image", exc_info=True)
            raise PersonaManagerError(
                f"avatar upload failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

        updated = persona.with_avatar(new_path)
        try:
            self._compare_and_set_avatar(persona, updated)
        except PersonaManagerError:
            self._delete_avatar_object(new_path)
            raise

        if persona.avatar_path:
            self._delete_avatar_object(persona.avatar_path)
        self.logger.info(f"Avatar updated for persona {persona.id}")
        return updated

    def delete_avatar(self, principal: Principal, persona_id: str) -> Persona:
        """
        Remove the persona's avatar image and revert to the auto-generated avatar (BR4.3).

        Args:
            principal: The user (must manage the persona)
            persona_id: ID of the persona

        Returns:
            Updated Persona (unchanged if no avatar was set)

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or missing
            AccessDeniedError: Readable but not manageable (403)
            PersonaManagerError: If the update fails
        """
        self._require_avatar_storage()
        persona = self._manageable_persona(principal, persona_id)
        if not persona.avatar_path:
            return persona

        updated = persona.with_avatar(None)
        self._compare_and_set_avatar(persona, updated)

        self._delete_avatar_object(persona.avatar_path)
        self.logger.info(f"Avatar removed for persona {persona.id}")
        return updated

    def get_avatar_url(self, principal: Principal, persona_id: str) -> str:
        """
        Return a short-lived presigned URL of the persona's avatar image (BR4.4).

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or missing (no
                URL is made)
            PersonaManagerError: If the avatar is missing, or the URL cannot
                be generated
        """
        s3_service = self._require_avatar_storage()
        persona, _ = self._readable_target(principal, persona_id)
        if not persona.avatar_path:
            raise PersonaManagerError(
                "persona has no avatar image",
                code=ErrorCode.PERSONA_AVATAR_NOT_FOUND,
            )
        try:
            return s3_service.generate_presigned_url(
                persona.avatar_path, AVATAR_URL_EXPIRATION_SECONDS
            )
        except Exception as e:
            self.logger.error("Failed to generate avatar URL", exc_info=True)
            raise PersonaManagerError(
                f"avatar url generation failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

    def _compare_and_set_avatar(self, current: Persona, updated: Persona) -> None:
        """
        Write ``updated.avatar_path`` only if the persona still exists and its
        avatar path is still ``current.avatar_path``.

        Image processing takes seconds between reading the persona and writing
        the path, so a full-record overwrite here would resurrect a persona
        deleted in the meantime, drop concurrent edits, or orphan the image of
        a concurrent upload.

        Raises:
            PersonaManagerError: PERSONA_NOT_FOUND if the persona was deleted,
                PERSONA_AVATAR_CONFLICT if the avatar changed concurrently,
                PERSONA_UPDATE_FAILED if the write itself failed
        """
        try:
            written = self.database_service.update_persona_avatar(
                current.id,
                new_path=updated.avatar_path,
                expected_path=current.avatar_path,
                updated_at=updated.updated_at,
            )
        except Exception as e:
            self.logger.error("Failed to write avatar path", exc_info=True)
            raise PersonaManagerError(
                f"avatar path write failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_UPDATE_FAILED,
            ) from e
        if written:
            return

        if self.get_persona_unchecked(current.id) is None:
            self.logger.info(f"Persona {current.id} was deleted during avatar update")
            raise PersonaManagerError(
                "persona deleted during avatar update",
                code=ErrorCode.PERSONA_NOT_FOUND,
            )
        self.logger.info(f"Avatar of persona {current.id} changed concurrently")
        raise PersonaManagerError(
            "avatar path changed concurrently",
            code=ErrorCode.PERSONA_AVATAR_CONFLICT,
        )

    def _require_avatar_storage(self) -> "S3Service":
        """Return the S3 service, or raise if avatar storage is not configured."""
        if self.s3_service is None:
            raise PersonaManagerError(
                "avatar storage is not configured",
                code=ErrorCode.PERSONA_AVATAR_UNAVAILABLE,
            )
        return self.s3_service

    def _manageable_persona(self, principal: Principal, persona_id: str) -> Persona:
        """The persona read strongly; the user must manage it.

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or missing
            AccessDeniedError: Readable but not manageable (403)
        """
        persona = self._read_consistent(persona_id)
        AccessPolicy.ensure_manageable(
            principal, self._target_of(persona, persona_id), ErrorCode.PERSONA_NOT_FOUND
        )
        assert persona is not None
        return persona

    def _delete_avatar_object(self, avatar_path: str) -> None:
        """Delete an avatar object on a best-effort basis (failures are logged)."""
        try:
            self._delete_avatar_or_raise(avatar_path)
        except Exception:
            self.logger.warning("Failed to delete avatar object", exc_info=True)

    def _delete_avatar_or_raise(self, avatar_path: str) -> None:
        if self.s3_service is None:
            self.logger.warning("Avatar object left in place: S3 is not configured")
            return
        self.s3_service.delete_file(avatar_path)

    def _validate_generated_persona(self, persona: Persona) -> None:
        """
        Validate a generated persona object.

        Args:
            persona: Persona object to validate

        Raises:
            PersonaManagerError: If validation fails
        """
        if not persona:
            raise PersonaManagerError(
                "generated persona object is falsy", code=ErrorCode.PERSONA_INVALID
            )

        # Basic validation
        self._validate_persona_for_save(persona)

        # Additional validation for generated personas
        if not persona.id:
            raise PersonaManagerError(
                "generated persona has no id", code=ErrorCode.PERSONA_INVALID
            )

        if not persona.created_at or not persona.updated_at:
            raise PersonaManagerError(
                "generated persona has no timestamps",
                code=ErrorCode.PERSONA_INVALID,
            )

    def _validate_persona_for_save(self, persona: Persona) -> None:
        """
        Validate a persona object before saving.

        Args:
            persona: Persona object to validate

        Raises:
            PersonaManagerError: If validation fails
        """
        if not persona:
            raise PersonaManagerError(
                "persona object is falsy", code=ErrorCode.PERSONA_INVALID
            )

        # Validate required fields
        for field, value in (
            ("name", persona.name),
            ("occupation", persona.occupation),
            ("background", persona.background),
        ):
            if not value or not value.strip():
                raise PersonaManagerError(
                    f"{field} is blank",
                    code=ErrorCode.PERSONA_FIELD_REQUIRED,
                    context={"field": field},
                )

        if persona.age is None or persona.age < 0 or persona.age > 150:
            raise PersonaManagerError(
                f"age {persona.age} out of range 0-150",
                code=ErrorCode.PERSONA_AGE_OUT_OF_RANGE,
                context={"min_age": 0, "max_age": 150},
            )

        # Validate list fields: presence, item content and size
        for field, items in (
            ("values", persona.values),
            ("pain_points", persona.pain_points),
            ("goals", persona.goals),
        ):
            if not items:
                raise PersonaManagerError(
                    f"{field} is empty",
                    code=ErrorCode.PERSONA_LIST_EMPTY,
                    context={"field": field},
                )
            for item in items:
                if not item or not item.strip():
                    raise PersonaManagerError(
                        f"{field} contains a blank item",
                        code=ErrorCode.PERSONA_LIST_HAS_EMPTY_ITEM,
                        context={"field": field},
                    )
            if len(items) > 10:
                raise PersonaManagerError(
                    f"{field} has {len(items)} items, max is 10",
                    code=ErrorCode.PERSONA_LIST_TOO_MANY_ITEMS,
                    context={"field": field, "max_items": 10},
                )

        # Validate field lengths
        for field, text, max_length in (
            ("name", persona.name, 100),
            ("occupation", persona.occupation, 200),
            ("background", persona.background, 2000),
            ("city", persona.city, 100),
        ):
            if text and len(text) > max_length:
                raise PersonaManagerError(
                    f"{field} length {len(text)} exceeds {max_length}",
                    code=ErrorCode.PERSONA_FIELD_TOO_LONG,
                    context={"field": field, "max_length": max_length},
                )

        # Validate demographic fields (optional, only when set)
        if persona.gender is not None and persona.gender not in VALID_GENDERS:
            raise PersonaManagerError(
                f"gender {persona.gender!r} not in VALID_GENDERS",
                code=ErrorCode.PERSONA_GENDER_INVALID,
                context={"allowed_genders": ", ".join(sorted(VALID_GENDERS))},
            )

        if persona.country and not country_service.is_valid_country(persona.country):
            # ISO 3166-1 alpha-2 として実在する国コードのみ許可（架空コード XX や
            # alpha-3 JPN を弾く）。検証は pycountry ベースの country_service に委譲。
            raise PersonaManagerError(
                f"country {persona.country!r} is not a valid ISO 3166-1 alpha-2 code",
                code=ErrorCode.PERSONA_COUNTRY_INVALID,
            )

        if persona.tags:
            if len(persona.tags) > 20:
                raise PersonaManagerError(
                    f"tags has {len(persona.tags)} items, max is 20",
                    code=ErrorCode.PERSONA_LIST_TOO_MANY_ITEMS,
                    context={"field": "tags", "max_items": 20},
                )
            for tag in persona.tags:
                if not tag or not tag.strip():
                    raise PersonaManagerError(
                        "tags contains a blank item",
                        code=ErrorCode.PERSONA_LIST_HAS_EMPTY_ITEM,
                        context={"field": "tags"},
                    )
                if len(tag) > 50:
                    raise PersonaManagerError(
                        f"tag length {len(tag)} exceeds 50",
                        code=ErrorCode.PERSONA_LIST_ITEM_TOO_LONG,
                        context={"field": "tags", "max_length": 50},
                    )
                # data属性ではカンマ区切りでフィルタに渡すため、タグ内のカンマを禁止
                if "," in tag:
                    raise PersonaManagerError(
                        "tag contains a comma",
                        code=ErrorCode.PERSONA_TAG_COMMA_NOT_ALLOWED,
                    )

    # ------------------------------------------------------------------
    # Bindings (U5, ADR-007, BR2)
    # ------------------------------------------------------------------

    def _readable_target(
        self, principal: Principal, persona_id: str
    ) -> Tuple[Persona, AccessTarget]:
        persona = self._read_consistent(persona_id)
        target = self._target_of(persona, persona_id)
        AccessPolicy.ensure_readable(principal, target, ErrorCode.PERSONA_NOT_FOUND)
        assert persona is not None
        return persona, target

    def _read_bindings(self, persona_id: str) -> List[PersonaBinding]:
        try:
            return self._bindings.list_for_persona(persona_id)
        except Exception as e:
            self.logger.error("Failed to read bindings", exc_info=True)
            raise PersonaManagerError(
                f"binding read failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

    def _asset_targets(
        self, resource_type: ResourceType, ids: List[str]
    ) -> Dict[str, AccessTarget]:
        """Decision columns of organization assets (one lookup, at most 500)."""
        if not ids:
            return {}
        return self._resource_lookup.get_many(resource_type, ids[:500])

    def get_kb_binding(self, principal: Principal, persona_id: str) -> KbBindingView:
        """The knowledge-base binding of a persona the user may read (BR2.1).

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or missing
        """
        _, target = self._readable_target(principal, persona_id)
        manageable = AccessPolicy.can_manage(principal, target)
        binding = next(
            (
                b
                for b in self._read_bindings(persona_id)
                if b.target_type is BindingTargetType.KB
            ),
            None,
        )
        row: Optional[BindingRow] = None
        if binding is not None:
            found = self._asset_targets(
                ResourceType.KNOWLEDGE_BASE, [binding.target_id]
            )
            kb_target = found.get(binding.target_id)
            if kb_target is not None and kb_target.exists:  # BR2.8
                row = BindingRow(
                    target_id=binding.target_id,
                    name=kb_target.display_name or "",
                    usable=AccessPolicy.can_use_asset(principal, kb_target),
                    metadata_filters=dict(binding.metadata_filters),
                )
        choices: List[KnowledgeBase] = []
        if manageable:
            kbs = self.database_service.get_all_knowledge_bases()
            usable = self._asset_targets(
                ResourceType.KNOWLEDGE_BASE, [kb.id for kb in kbs]
            )
            choices = [
                kb
                for kb in kbs
                if (t := usable.get(kb.id)) is not None
                and AccessPolicy.can_use_asset(principal, t)
            ]
        return KbBindingView(
            persona_id=persona_id, manageable=manageable, binding=row, choices=choices
        )

    def create_kb_binding(
        self,
        principal: Principal,
        persona_id: str,
        kb_id: str,
        metadata_filters: Optional[Dict[str, Any]] = None,
    ) -> PersonaBinding:
        """Bind a knowledge base, replacing the previous one in one write (BR2.9).

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when the persona is unreadable
            AccessDeniedError: Readable but not manageable (403)
            PersonaManagerError: ``KB_BINDING_NOT_FOUND`` when the knowledge
                base is missing or the user may not use it (BR2.3)
        """
        self._manageable_persona(principal, persona_id)
        kb_target = self._asset_targets(ResourceType.KNOWLEDGE_BASE, [kb_id]).get(kb_id)
        if kb_target is None or not AccessPolicy.can_use_asset(principal, kb_target):
            raise PersonaManagerError(
                "knowledge base not usable", code=ErrorCode.KB_BINDING_NOT_FOUND
            )
        binding = PersonaBinding.for_kb(
            persona_id,
            kb_id,
            metadata_filters={
                str(k): str(v) for k, v in (metadata_filters or {}).items()
            },
        )
        self._write_binding(binding)
        self.logger.info(f"Created KB binding: persona={persona_id}")
        return binding

    def delete_kb_binding(self, principal: Principal, persona_id: str) -> None:
        """Remove the persona's knowledge-base binding (BR2.2, BR2.5).

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when the persona is unreadable
            AccessDeniedError: Readable but not manageable (403)
            PersonaManagerError: ``KB_BINDING_NOT_FOUND`` when there is none
        """
        self._manageable_persona(principal, persona_id)
        if not self._remove_binding(persona_id, KB_TARGET_KEY):
            raise PersonaManagerError(
                "no knowledge-base binding", code=ErrorCode.KB_BINDING_NOT_FOUND
            )
        self.logger.info(f"Deleted KB binding: persona={persona_id}")

    def get_dataset_bindings(
        self, principal: Principal, persona_id: str
    ) -> DatasetBindingsView:
        """The dataset bindings of a persona the user may read (BR2.1, BR2.7).

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or missing
        """
        _, target = self._readable_target(principal, persona_id)
        manageable = AccessPolicy.can_manage(principal, target)
        bindings = [b for b in self._read_bindings(persona_id) if b.is_dataset]
        found = self._asset_targets(
            ResourceType.DATASET, [b.target_id for b in bindings]
        )
        rows: List[BindingRow] = []
        for b in bindings:
            t = found.get(b.target_id)
            if t is None or not t.exists:
                continue  # the dataset is gone (BR2.8)
            rows.append(
                BindingRow(
                    target_id=b.target_id,
                    name=t.display_name or "",
                    usable=b.dedicated or AccessPolicy.can_use_asset(principal, t),
                    dedicated=b.dedicated,
                    binding_keys=dict(b.binding_keys),
                )
            )
        choices: List[Dataset] = []
        if manageable:
            bound = {b.target_id for b in bindings}
            datasets = [
                d for d in self.database_service.get_all_datasets() if d.id not in bound
            ]
            usable = self._asset_targets(ResourceType.DATASET, [d.id for d in datasets])
            choices = [
                d
                for d in datasets
                if (t := usable.get(d.id)) is not None
                and AccessPolicy.can_use_asset(principal, t)
            ]
        return DatasetBindingsView(
            persona_id=persona_id, manageable=manageable, bindings=rows, choices=choices
        )

    def create_dataset_binding(
        self,
        principal: Principal,
        persona_id: str,
        dataset_id: str,
        key_name: str = "",
        key_value: str = "",
    ) -> PersonaBinding:
        """Bind a dataset to a persona the user manages (BR2.2〜BR2.4).

        Organization assets must be usable by the user (``can_use_asset``;
        admin only until U7). A persona-dedicated dataset binds only to its
        own persona.

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when the persona is unreadable
            AccessDeniedError: Readable but not manageable (403)
            PersonaManagerError: ``DATASET_NOT_FOUND`` (missing or not
                usable), ``DATASET_COLUMN_NOT_FOUND`` (unknown key column)
        """
        self._manageable_persona(principal, persona_id)
        dataset = self._read_dataset(dataset_id)
        if dataset is None:
            raise PersonaManagerError(
                "dataset not found", code=ErrorCode.DATASET_NOT_FOUND
            )
        dedicated = False
        if dataset.owner_persona_id:
            if not dataset.is_dedicated_to(persona_id):
                raise PersonaManagerError(
                    "dataset belongs to another persona",
                    code=ErrorCode.DATASET_NOT_FOUND,
                )
            dedicated = True
        else:
            asset = self._asset_targets(ResourceType.DATASET, [dataset_id]).get(
                dataset_id
            )
            if asset is None or not AccessPolicy.can_use_asset(principal, asset):
                raise PersonaManagerError(
                    "dataset not usable", code=ErrorCode.DATASET_NOT_FOUND
                )

        binding_keys: Dict[str, str] = {}
        if key_name and key_value:
            if key_name not in {col.name for col in dataset.columns}:
                raise PersonaManagerError(
                    f"column {key_name!r} not in dataset columns",
                    code=ErrorCode.DATASET_COLUMN_NOT_FOUND,
                    context={"column": key_name},
                )
            binding_keys[key_name] = key_value

        binding = PersonaBinding.for_dataset(
            persona_id, dataset_id, binding_keys=binding_keys, dedicated=dedicated
        )
        self._write_binding(binding)
        self.logger.info(f"Created dataset binding: persona={persona_id}")
        return binding

    def delete_dataset_binding(
        self, principal: Principal, persona_id: str, dataset_id: str
    ) -> None:
        """Remove one dataset binding; a dedicated dataset goes with it (BR6.4).

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when the persona is unreadable
            AccessDeniedError: Readable but not manageable (403)
            PersonaManagerError: ``DATASET_BINDING_NOT_FOUND`` when there is
                no binding to that dataset
        """
        self._manageable_persona(principal, persona_id)
        if not self._remove_binding(persona_id, dataset_target_key(dataset_id)):
            raise PersonaManagerError(
                "no binding to this dataset",
                code=ErrorCode.DATASET_BINDING_NOT_FOUND,
            )
        self.logger.info(f"Deleted dataset binding: persona={persona_id}")
        try:
            dataset = self.database_service.get_dataset(dataset_id)
            if dataset is not None and dataset.is_dedicated_to(persona_id):
                self._delete_dedicated_dataset(dataset)
        except Exception as e:
            self._cleanup_failed(persona_id, "persona_datasets", e)

    def preview_binding_data(
        self,
        principal: Principal,
        persona_id: str,
        dataset_id: str,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Rows of a bound dataset narrowed by the binding keys (BR2.6).

        Returns:
            ``{"columns": [...], "rows": [[...], ...], "total_count": int}``

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when the persona is unreadable
            PersonaManagerError: ``DATASET_BINDING_NOT_FOUND`` (no binding, or
                an organization asset the user may not use),
                ``DATASET_NOT_FOUND``, ``DATASET_OPERATION_FAILED``
        """
        self._readable_target(principal, persona_id)
        binding = next(
            (
                b
                for b in self._read_bindings(persona_id)
                if b.is_dataset and b.target_id == dataset_id
            ),
            None,
        )
        if binding is None:
            raise PersonaManagerError(
                "binding not found", code=ErrorCode.DATASET_BINDING_NOT_FOUND
            )
        dataset = self._read_dataset(dataset_id)
        if dataset is None:
            raise PersonaManagerError(
                "dataset not found", code=ErrorCode.DATASET_NOT_FOUND
            )
        if not dataset.is_dedicated_to(persona_id):
            asset = self._asset_targets(ResourceType.DATASET, [dataset_id]).get(
                dataset_id
            )
            if asset is None or not AccessPolicy.can_use_asset(principal, asset):
                raise PersonaManagerError(
                    "dataset not usable", code=ErrorCode.DATASET_BINDING_NOT_FOUND
                )
        return self._run_preview(dataset, binding.binding_keys, limit)

    def _run_preview(
        self, dataset: Dataset, binding_keys: Dict[str, str], limit: int
    ) -> Dict[str, Any]:
        valid_columns = {col.name for col in dataset.columns}
        where_clauses: List[str] = []
        params: List[Any] = []
        for key, value in binding_keys.items():
            if key not in valid_columns or not re.fullmatch(r"[\w]+", key, re.UNICODE):
                raise PersonaManagerError(
                    "binding key is not a safe dataset column",
                    code=ErrorCode.DATASET_OPERATION_FAILED,
                )
            where_clauses.append(f'"{key}" = ${len(params) + 1}')
            params.append(value)
        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        try:
            total_count, columns, rows = self._preview_service.execute_dataset_preview(
                s3_path=dataset.s3_path,
                count_sql=f"SELECT COUNT(*) FROM dataset{where_sql}",
                data_sql=f"SELECT * FROM dataset{where_sql} LIMIT {int(limit)}",
                params=params if params else None,
            )
        except Exception as e:
            self.logger.error("Dataset preview failed", exc_info=True)
            raise PersonaManagerError(
                f"dataset preview failed ({type(e).__name__})",
                code=ErrorCode.DATASET_OPERATION_FAILED,
            ) from e
        return {"columns": columns, "rows": rows, "total_count": total_count}

    def _read_dataset(self, dataset_id: str) -> Optional[Dataset]:
        try:
            return self.database_service.get_dataset(dataset_id)
        except Exception as e:
            self.logger.error("Failed to read a dataset", exc_info=True)
            raise PersonaManagerError(
                f"dataset read failed ({type(e).__name__})",
                code=ErrorCode.DATASET_OPERATION_FAILED,
            ) from e

    def _write_binding(self, binding: PersonaBinding) -> None:
        try:
            self._bindings.put(binding)
        except Exception as e:
            self.logger.error("Failed to write a binding", exc_info=True)
            raise PersonaManagerError(
                f"binding write failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

    def _remove_binding(self, persona_id: str, target_key: str) -> bool:
        try:
            return self._bindings.delete(persona_id, target_key)
        except Exception as e:
            self.logger.error("Failed to delete a binding", exc_info=True)
            raise PersonaManagerError(
                f"binding delete failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

    # ------------------------------------------------------------------
    # Saving generated candidates (U5, BR1.4, BR1.5, BR1.9, BR6.1)
    # ------------------------------------------------------------------

    def save_generated_personas(
        self,
        principal: Principal,
        claim: CandidateClaim,
        *,
        edited: Optional[Persona] = None,
        selected_temp_ids: Iterable[str] = (),
    ) -> CandidateSaveResult:
        """Save claimed candidates as the user's personas.

        One-by-one save (``edited`` given): the claim holds one candidate; the
        persona is made from the edited fields under a new ID and keeps the
        candidate's generation record. Selected behaviour CSVs that the
        candidate really holds become datasets dedicated to the persona
        (copy -> registration -> binding; a failure removes what was made).
        A failed save raises, so the user can correct the form.

        Bulk save (no ``edited``): every candidate is saved as generated; one
        failure does not stop the others.

        R-04: right before each candidate is saved, the request's hold on it is
        confirmed (and renewed) with one conditional update; a hold that went
        stale and was taken over by another request is not saved here (bulk:
        it goes to the failed ones; one-by-one: ``GENERATION_PERSONA_CACHE_
        EXPIRED``). Once saved, ``saved_at`` is written under the same hold
        before the cleanup, so the candidate can never be saved twice.

        Returns:
            The outcome per candidate (``finish_candidates`` cleans up from it).

        Raises:
            AccessDeniedError: The user may not create resources.
            PersonaManagerError: The one-by-one save failed.
        """
        AccessPolicy.require(
            principal, Permission.RESOURCE_CREATE, resource_type=_PERSONA
        )
        saved_ids: List[str] = []
        failed_ids: List[str] = []
        persona_ids: List[str] = []
        failed_names: List[str] = []
        failed_datasets: List[str] = []
        selected = set(selected_temp_ids)
        for candidate in claim.candidates:
            base = candidate.to_persona()
            if edited is not None:
                persona = replace(
                    edited,
                    id=base.id,
                    generation_log=candidate.generation_log,
                    generation_context=candidate.generation_context,
                )
            else:
                persona = base
            try:
                self._confirm_hold(claim, candidate)
                persona_id = self.save_persona(principal, persona)
            except (PersonaManagerError, AccessDeniedError):
                if edited is not None:
                    raise
                self.logger.warning("Saving a generated persona failed", exc_info=True)
                failed_ids.append(candidate.candidate_id)
                failed_names.append(candidate.persona_name)
                continue
            saved_ids.append(candidate.candidate_id)
            persona_ids.append(persona_id)
            self._mark_candidate_saved(claim, candidate)
            if edited is None:
                continue
            for behavior in candidate.behavior_datasets:
                if behavior.temp_id not in selected:
                    continue  # only what the candidate holds (BR1.5)
                if not self._make_dedicated_dataset(persona_id, behavior):
                    failed_datasets.append(behavior.name)
        return CandidateSaveResult(
            saved_ids=tuple(saved_ids),
            failed_ids=tuple(failed_ids),
            saved_persona_ids=tuple(persona_ids),
            failed_names=tuple(failed_names),
            failed_datasets=tuple(failed_datasets),
        )

    def _confirm_hold(self, claim: CandidateClaim, candidate: PersonaCandidate) -> None:
        """Check that ``claim`` still holds the candidate, right before its save.

        Raises:
            PersonaManagerError: ``GENERATION_PERSONA_CACHE_EXPIRED`` when the
                hold was taken over or the candidate was saved meanwhile;
                ``GENERATION_OPERATION_FAILED`` when the check itself failed.
        """
        try:
            held = self._candidates.confirm_claim(
                candidate.candidate_id, claim.token, self._clock()
            )
        except Exception as e:
            raise PersonaManagerError(
                f"hold check failed ({type(e).__name__})",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e
        if not held:
            # Not an authorization denial: the same INFO line as a failed claim
            log_event("persona.candidate_unavailable", level="INFO", count=1)
            raise PersonaManagerError(
                "candidate hold lost", code=ErrorCode.GENERATION_PERSONA_CACHE_EXPIRED
            )

    def _mark_candidate_saved(
        self, claim: CandidateClaim, candidate: PersonaCandidate
    ) -> None:
        """Write ``saved_at`` under the same hold right after the save (R-04).

        A failure is only logged: the cleanup deletes the candidate anyway, and
        its fallback marks it saved.
        """
        try:
            if self._candidates.mark_saved(
                candidate.candidate_id, claim.token, self._clock()
            ):
                return
            error: Optional[BaseException] = None
        except Exception as e:
            error = e
        log_event(
            "persona.candidate_cleanup_failed",
            level="WARNING",
            candidate_id=candidate.candidate_id,
            step="mark_saved",
            error_type=type(error).__name__ if error else None,
        )

    def _make_dedicated_dataset(
        self, persona_id: str, behavior: BehaviorDatasetCandidate
    ) -> bool:
        """Copy the CSV, register the dataset and bind it (BR6.1).

        Returns:
            Whether the dataset was made. On failure what was made is removed
            right away (a removal failure is ``persona.cleanup_failed``).
        """
        if self.s3_service is None:
            return False
        dataset_id = str(uuid.uuid4())
        dest_key = f"{DEDICATED_DATASET_PREFIX}/{persona_id}/{dataset_id}.csv"
        copied: Optional[str] = None
        registered = False
        try:
            copied = self.s3_service.copy_object(behavior.temp_s3_key, dest_key)
            self.database_service.save_dataset(
                Dataset.create_new(
                    name=behavior.name,
                    description=f"{behavior.data_type_label}（自動取得）",
                    s3_path=copied,
                    columns=list(behavior.columns),
                    row_count=behavior.row_count,
                    owner_persona_id=persona_id,
                    dataset_id=dataset_id,
                )
            )
            registered = True
            self._bindings.put(
                PersonaBinding.for_dataset(
                    persona_id,
                    dataset_id,
                    binding_keys=behavior.binding_keys,
                    dedicated=True,
                )
            )
            return True
        except Exception:
            self.logger.warning("A dedicated dataset could not be made", exc_info=True)
            try:
                if registered:
                    self.database_service.delete_dataset(dataset_id)
                if copied:
                    self.s3_service.delete_file(copied)
            except Exception as cleanup_error:
                self._cleanup_failed(persona_id, "persona_datasets", cleanup_error)
            return False

    # ------------------------------------------------------------------
    # Export (U5, BR7)
    # ------------------------------------------------------------------

    def export_persona(self, principal: Principal, persona_id: str) -> PersonaExport:
        """The persona as Markdown for anyone who may read it (BR7.1〜BR7.3).

        Memory, bindings, the owner and the shares are never included.

        Raises:
            CodedError: ``PERSONA_NOT_FOUND`` when unreadable or missing
        """
        persona, _ = self._readable_target(principal, persona_id)
        return PersonaExport(
            filename=export_filename(persona.name),
            body=render_persona_markdown(persona),
        )


def _matches(persona: Persona, query: str) -> bool:
    """Partial match on the name, occupation and background (BR3.8)."""
    needle = query.strip().lower()
    return (
        needle in persona.name.lower()
        or needle in persona.occupation.lower()
        or needle in persona.background.lower()
    )
