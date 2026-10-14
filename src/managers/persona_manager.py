"""
Persona Manager for AI Persona System.
ペルソナのCRUD、バリデーション、KB/Datasetバインディング管理を担当する。
"""

import logging
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
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
from ..models.errors import CodedError, ErrorCode
from ..models.persona import Persona
from ..models.demographics import VALID_GENDERS
from ..models.sharing import ListTab, TabbedList
from ..services import country_service
from ..services.database_service import DatabaseService, DatabaseError
from ..services.service_factory import service_factory
from .components.access_policy import AccessPolicy
from .components.owner_directory import OWNER_UNKNOWN, OwnerDirectory
from .components.resource_sharing import ResourceSharing
from .components.sharing_runtime import SharingParts, build_sharing_parts, shared_parts
from .shared.image_normalize import (
    AVATAR_FILE_EXTENSION,
    AVATAR_MIME_TYPE,
    AvatarImageError,
    AvatarImageTooLargeError,
    normalize_avatar_image,
)

if TYPE_CHECKING:
    from ..services.s3_service import S3Service

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


class PersonaManager:
    """
    ペルソナのCRUD操作とバインディング管理を行うManager。
    記憶管理はPersonaMemoryManagerに委譲済み。

    v1.0.0 U4: 保存した人を所有者にし（BR6.1）、一覧・検索・詳細・編集・削除は
    利用者（Principal）を受け取って ResourceSharing と AccessPolicy で判定する。
    """

    def __init__(
        self,
        database_service: Optional[DatabaseService] = None,
        s3_service: Optional["S3Service"] = None,
        resource_sharing: Optional[ResourceSharing] = None,
        owners: Optional[OwnerDirectory] = None,
    ):
        """
        Args:
            database_service: Database service instance for persistence (optional, uses singleton if not provided)
            s3_service: S3 service for avatar images (optional, uses singleton if not provided;
                None when S3_BUCKET_NAME is not configured, which disables avatar upload)
            resource_sharing: Sharing component (optional; the per-task
                instance shared with the other Managers, built on first use)
            owners: Owner-name copy (optional; the per-task instance)
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

    def get_persona(self, persona_id: str) -> Optional[Persona]:
        """
        Retrieve a persona by ID.

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
        personas, _ = self._read_bodies(
            lambda: self.database_service.get_all_personas(search_all=True)
        )
        return personas

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
        avatar_path = deleted.get("avatar_path")
        if avatar_path:
            self._delete_avatar_object(str(avatar_path))
        return True

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

    def set_avatar(self, persona_id: str, content: bytes) -> Persona:
        """
        Normalize an uploaded image and set it as the persona's avatar.

        The path is written with a compare-and-set condition (see
        ``_compare_and_set_avatar``). The previous avatar object is deleted only
        after that write succeeds; if it fails, the newly uploaded object is removed.

        Args:
            persona_id: ID of the persona
            content: Raw uploaded image bytes

        Returns:
            Updated Persona

        Raises:
            PersonaManagerError: On invalid input, missing persona, or storage failure
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
        persona = self._get_existing_persona(persona_id)

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

    def delete_avatar(self, persona_id: str) -> Persona:
        """
        Remove the persona's avatar image and revert to the auto-generated avatar.

        Args:
            persona_id: ID of the persona

        Returns:
            Updated Persona (unchanged if no avatar was set)

        Raises:
            PersonaManagerError: If the persona is missing or the update fails
        """
        self._require_avatar_storage()
        persona = self._get_existing_persona(persona_id)
        if not persona.avatar_path:
            return persona

        updated = persona.with_avatar(None)
        self._compare_and_set_avatar(persona, updated)

        self._delete_avatar_object(persona.avatar_path)
        self.logger.info(f"Avatar removed for persona {persona.id}")
        return updated

    def get_avatar_url(self, persona_id: str) -> str:
        """
        Return a short-lived presigned URL of the persona's avatar image.

        Raises:
            PersonaManagerError: If the persona or its avatar is missing,
                or the URL cannot be generated
        """
        s3_service = self._require_avatar_storage()
        persona = self._get_existing_persona(persona_id)
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

        if self.get_persona(current.id) is None:
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

    def _get_existing_persona(self, persona_id: str) -> Persona:
        """Return the persona, or raise PERSONA_NOT_FOUND."""
        persona = self.get_persona(persona_id)
        if persona is None:
            raise PersonaManagerError(
                "persona not found", code=ErrorCode.PERSONA_NOT_FOUND
            )
        return persona

    def _delete_avatar_object(self, avatar_path: str) -> None:
        """Delete an avatar object on a best-effort basis (failures are logged)."""
        if self.s3_service is None:
            self.logger.warning("Avatar object left in place: S3 is not configured")
            return
        try:
            self.s3_service.delete_file(avatar_path)
        except Exception:
            self.logger.warning("Failed to delete avatar object", exc_info=True)

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

    # --- ナレッジベース紐付け操作 ---

    def get_kb_binding(self, persona_id: str) -> Tuple[list, Any]:
        """
        ペルソナのナレッジベース紐付け情報を取得する。

        Args:
            persona_id: ペルソナID

        Returns:
            (knowledge_bases, binding) のタプル
        """
        knowledge_bases = self.database_service.get_all_knowledge_bases()
        binding = self.database_service.get_kb_binding_by_persona(persona_id)

        if binding:
            kb = self.database_service.get_knowledge_base(binding.kb_id)
            if not kb:
                self.database_service.delete_kb_binding(binding.id)
                binding = None

        return knowledge_bases, binding

    def create_kb_binding(
        self,
        persona_id: str,
        kb_id: str,
        metadata_filters: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        ナレッジベース紐付けを作成する（既存があれば上書き）。

        Args:
            persona_id: ペルソナID
            kb_id: ナレッジベースID
            metadata_filters: メタデータフィルター

        Returns:
            作成されたバインディング
        """
        from ..models.knowledge_base import PersonaKBBinding

        binding = PersonaKBBinding.create_new(
            persona_id=persona_id,
            kb_id=kb_id,
            metadata_filters=metadata_filters or {},
        )
        self.database_service.save_kb_binding(binding)
        self.logger.info(f"Created KB binding: persona={persona_id}, kb={kb_id}")
        return binding

    def delete_kb_binding(self, binding_id: str) -> None:
        """ナレッジベース紐付けを解除する。"""
        self.database_service.delete_kb_binding(binding_id)
        self.logger.info(f"Deleted KB binding: {binding_id}")

    # --- データセット紐付け操作 ---

    def get_dataset_bindings(self, persona_id: str) -> Tuple[list, Dict[str, Any]]:
        """
        ペルソナのデータセット紐付け一覧を取得する。

        Args:
            persona_id: ペルソナID

        Returns:
            (datasets, bindings_map) のタプル
        """
        datasets = self.database_service.get_all_datasets()
        bindings = self.database_service.get_bindings_by_persona(persona_id)
        bindings_map = {b.dataset_id: b for b in bindings}
        return datasets, bindings_map

    def create_dataset_binding(
        self,
        persona_id: str,
        dataset_id: str,
        key_name: str = "",
        key_value: str = "",
    ) -> Any:
        """
        データセット紐付けを作成する。

        Args:
            persona_id: ペルソナID
            dataset_id: データセットID
            key_name: キーカラム名
            key_value: キー値

        Returns:
            作成されたバインディング

        Raises:
            PersonaManagerError: バリデーション失敗時
        """
        from ..models.dataset import PersonaDatasetBinding

        binding_keys: Dict[str, str] = {}
        if key_name and key_value:
            dataset = self.database_service.get_dataset(dataset_id)
            if dataset:
                valid_columns = {col.name for col in dataset.columns}
                if key_name not in valid_columns:
                    raise PersonaManagerError(
                        f"column {key_name!r} not in dataset columns",
                        code=ErrorCode.DATASET_COLUMN_NOT_FOUND,
                        context={"column": key_name},
                    )
            binding_keys[key_name] = key_value

        binding = PersonaDatasetBinding.create_new(
            persona_id=persona_id,
            dataset_id=dataset_id,
            binding_keys=binding_keys,
        )
        self.database_service.save_binding(binding)
        self.logger.info(
            f"Created dataset binding: persona={persona_id}, dataset={dataset_id}"
        )
        return binding

    def delete_dataset_binding(self, binding_id: str) -> None:
        """データセット紐付けを削除する。"""
        self.database_service.delete_binding(binding_id)
        self.logger.info(f"Deleted dataset binding: {binding_id}")


def _matches(persona: Persona, query: str) -> bool:
    """Partial match on the name, occupation and background (BR3.8)."""
    needle = query.strip().lower()
    return (
        needle in persona.name.lower()
        or needle in persona.occupation.lower()
        or needle in persona.background.lower()
    )
