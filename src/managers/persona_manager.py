"""
Persona Manager for AI Persona System.
ペルソナのCRUD、バリデーション、KB/Datasetバインディング管理を担当する。
"""

import logging
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..config import config
from ..models.errors import CodedError, ErrorCode
from ..models.persona import Persona
from ..models.demographics import VALID_GENDERS
from ..services import country_service
from ..services.database_service import DatabaseService, DatabaseError
from ..services.service_factory import service_factory
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


class PersonaManagerError(CodedError):
    """Custom exception for persona manager related errors."""

    pass


class PersonaManager:
    """
    ペルソナのCRUD操作とバインディング管理を行うManager。
    記憶管理はPersonaMemoryManagerに委譲済み。
    """

    def __init__(
        self,
        database_service: Optional[DatabaseService] = None,
        s3_service: Optional["S3Service"] = None,
    ):
        """
        Args:
            database_service: Database service instance for persistence (optional, uses singleton if not provided)
            s3_service: S3 service for avatar images (optional, uses singleton if not provided;
                None when S3_BUCKET_NAME is not configured, which disables avatar upload)
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

    def save_persona(self, persona: Persona) -> str:
        """
        Save a persona to the database.

        Args:
            persona: Persona object to save

        Returns:
            str: The persona ID

        Raises:
            PersonaManagerError: If save operation fails
        """
        if not persona:
            raise PersonaManagerError(
                "persona object is falsy", code=ErrorCode.PERSONA_INVALID
            )

        # Validate persona before saving
        self._validate_persona_for_save(persona)

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

    def get_all_personas(
        self,
        limit: int = 20,
        cursor: Optional[Dict[str, Any]] = None,
        search_all: bool = False,
    ) -> Tuple[List[Persona], Optional[Dict[str, Any]]]:
        """
        Retrieve personas with cursor-based pagination.

        Args:
            limit: Page size (default 20).
            cursor: LastEvaluatedKey from previous call.
            search_all: If True, fall back to full scan (for search queries
                that cannot be satisfied by GSI Query).

        Returns:
            Tuple of (personas, next_cursor). next_cursor is None if no more pages.

        Raises:
            PersonaManagerError: If retrieval operation fails
        """
        try:
            personas, next_cursor = self.database_service.get_all_personas(
                limit=limit, cursor=cursor, search_all=search_all
            )
            self.logger.info(
                f"Retrieved {len(personas)} personas (next_cursor={'yes' if next_cursor else 'no'})"
            )
            return personas, next_cursor

        except DatabaseError as e:
            self.logger.error(
                "Database error while retrieving all personas", exc_info=True
            )
            raise PersonaManagerError(
                f"persona retrieval failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e
        except Exception as e:
            self.logger.error(
                "Unexpected error while retrieving all personas", exc_info=True
            )
            raise PersonaManagerError(
                f"persona retrieval failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

    def get_all_personas_full(self) -> List[Persona]:
        """
        Retrieve every persona (scan-based). Use sparingly; prefer cursor
        pagination via get_all_personas() for UI listings.
        """
        personas, _ = self.get_all_personas(search_all=True)
        return personas

    def delete_persona(self, persona_id: str) -> bool:
        """
        Delete a persona from the database.

        Args:
            persona_id: ID of the persona to delete

        Returns:
            True if deletion was successful, False if persona not found

        Raises:
            PersonaManagerError: If deletion operation fails
        """
        if not persona_id or not persona_id.strip():
            raise PersonaManagerError(
                "persona id is blank", code=ErrorCode.PERSONA_ID_INVALID
            )

        try:
            existing = self.database_service.get_persona(persona_id.strip())
            success = self.database_service.delete_persona(persona_id.strip())
            if success:
                self.logger.info(f"Persona deleted successfully: {persona_id}")
                if existing and existing.avatar_path:
                    self._delete_avatar_object(existing.avatar_path)
            else:
                self.logger.warning(f"Persona not found for deletion: {persona_id}")
            return success

        except DatabaseError as e:
            self.logger.error("Database error while deleting persona", exc_info=True)
            raise PersonaManagerError(
                f"persona delete failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e
        except Exception as e:
            self.logger.error("Unexpected error while deleting persona", exc_info=True)
            raise PersonaManagerError(
                f"persona delete failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

    def update_persona(
        self,
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
    ) -> Optional[Persona]:
        """
        Edit an existing persona with new values.

        Args:
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
            Updated Persona object if successful, None if persona not found

        Raises:
            PersonaManagerError: If edit operation fails
        """
        if not persona_id or not persona_id.strip():
            raise PersonaManagerError(
                "persona id is blank", code=ErrorCode.PERSONA_ID_INVALID
            )

        try:
            # Get existing persona
            existing_persona = self.get_persona(persona_id)
            if not existing_persona:
                self.logger.warning(f"Persona not found for editing: {persona_id}")
                return None

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
            else:
                raise PersonaManagerError(
                    "database reported update failure",
                    code=ErrorCode.PERSONA_UPDATE_FAILED,
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

        The previous avatar object is deleted after the new one is saved.
        If the database update fails, the newly uploaded object is removed.

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
            success = self.database_service.update_persona(updated)
        except Exception as e:
            self.logger.error("Failed to save avatar path", exc_info=True)
            self._delete_avatar_object(new_path)
            raise PersonaManagerError(
                f"avatar path save failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_UPDATE_FAILED,
            ) from e
        if not success:
            self._delete_avatar_object(new_path)
            raise PersonaManagerError(
                "database reported update failure",
                code=ErrorCode.PERSONA_UPDATE_FAILED,
            )

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
        try:
            success = self.database_service.update_persona(updated)
        except Exception as e:
            self.logger.error("Failed to clear avatar path", exc_info=True)
            raise PersonaManagerError(
                f"avatar path clear failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_UPDATE_FAILED,
            ) from e
        if not success:
            raise PersonaManagerError(
                "database reported update failure",
                code=ErrorCode.PERSONA_UPDATE_FAILED,
            )

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

    def get_persona_count(self) -> int:
        """
        Get the total number of personas in the database.

        Returns:
            Number of personas

        Raises:
            PersonaManagerError: If count operation fails
        """
        try:
            return self.database_service.get_persona_count()

        except PersonaManagerError:
            # Re-raise PersonaManagerError
            raise
        except Exception as e:
            self.logger.error(
                "Unexpected error while getting persona count", exc_info=True
            )
            raise PersonaManagerError(
                f"persona count failed ({type(e).__name__})",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            ) from e

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
