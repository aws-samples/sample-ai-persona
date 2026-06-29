"""
Persona Manager for AI Persona System.
Handles persona generation workflow, editing, and saving functionality.
"""

import logging
from typing import List, Optional, Dict, Any, Tuple

from ..models.persona import Persona
from ..models.demographics import VALID_GENDERS
from ..services import country_service
from ..services.ai_service import AIService
from ..services.database_service import DatabaseService, DatabaseError
from ..services.service_factory import service_factory


class PersonaManagerError(Exception):
    """Custom exception for persona manager related errors."""

    pass


class PersonaManager:
    """
    Manager class for handling persona-related operations.
    Orchestrates persona generation workflow, editing, and persistence.
    """

    def __init__(
        self,
        ai_service: AIService | None = None,
        database_service: Optional[DatabaseService] = None,
        file_manager: Any = None,
    ):
        """
        Initialize persona manager.

        Args:
            ai_service: AI service instance for persona generation (optional, uses singleton if not provided)
            database_service: Database service instance for persistence (optional, uses singleton if not provided)
            file_manager: FileManager instance (optional, created lazily if not provided)
        """
        self.logger = logging.getLogger(__name__)

        # Use singleton services if not provided
        self.ai_service = ai_service or service_factory.get_ai_service()
        self.database_service = (
            database_service or service_factory.get_database_service()
        )
        self._file_manager = file_manager

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
            raise PersonaManagerError("ペルソナオブジェクトが無効です")

        # Validate persona before saving
        self._validate_persona_for_save(persona)

        try:
            persona_id = self.database_service.save_persona(persona)
            self.logger.info(
                f"Persona saved successfully: {persona.name} (ID: {persona_id})"
            )
            return persona_id

        except DatabaseError as e:
            error_msg = f"Database error while saving persona: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while saving persona: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

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
            raise PersonaManagerError("ペルソナIDが無効です")

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
            error_msg = f"Database error while retrieving persona: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while retrieving persona: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

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
            error_msg = f"Database error while retrieving all personas: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while retrieving all personas: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

    def get_all_personas_full(self) -> List[Persona]:
        """
        Retrieve every persona (scan-based). Use sparingly; prefer cursor
        pagination via get_all_personas() for UI listings.
        """
        personas, _ = self.get_all_personas(search_all=True)
        return personas

    def update_persona(self, persona: Persona) -> bool:
        """
        Update an existing persona in the database.

        Args:
            persona: Updated persona object

        Returns:
            True if update was successful, False if persona not found

        Raises:
            PersonaManagerError: If update operation fails
        """
        if not persona:
            raise PersonaManagerError("ペルソナオブジェクトが無効です")

        # Validate persona before updating
        self._validate_persona_for_save(persona)

        try:
            # Update the updated_at timestamp
            updated_persona = (
                persona.update()
            )  # This creates a new instance with updated timestamp

            success = self.database_service.update_persona(updated_persona)
            if success:
                self.logger.info(
                    f"Persona updated successfully: {persona.name} (ID: {persona.id})"
                )
            else:
                self.logger.warning(f"Persona not found for update: {persona.id}")
            return success

        except DatabaseError as e:
            error_msg = f"Database error while updating persona: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while updating persona: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

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
            raise PersonaManagerError("ペルソナIDが無効です")

        try:
            success = self.database_service.delete_persona(persona_id.strip())
            if success:
                self.logger.info(f"Persona deleted successfully: {persona_id}")
            else:
                self.logger.warning(f"Persona not found for deletion: {persona_id}")
            return success

        except DatabaseError as e:
            error_msg = f"Database error while deleting persona: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while deleting persona: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

    def edit_persona(
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
            raise PersonaManagerError("ペルソナIDが無効です")

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
                raise PersonaManagerError("ペルソナの更新に失敗しました")

        except PersonaManagerError:
            # Re-raise PersonaManagerError
            raise
        except Exception as e:
            error_msg = f"Unexpected error while editing persona: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

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
            error_msg = f"Unexpected error while getting persona count: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

    def _validate_generated_persona(self, persona: Persona) -> None:
        """
        Validate a generated persona object.

        Args:
            persona: Persona object to validate

        Raises:
            PersonaManagerError: If validation fails
        """
        if not persona:
            raise PersonaManagerError("生成されたペルソナが無効です")

        # Basic validation
        self._validate_persona_for_save(persona)

        # Additional validation for generated personas
        if not persona.id:
            raise PersonaManagerError("生成されたペルソナにIDが設定されていません")

        if not persona.created_at or not persona.updated_at:
            raise PersonaManagerError(
                "生成されたペルソナにタイムスタンプが設定されていません"
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
            raise PersonaManagerError("ペルソナオブジェクトが無効です")

        # Validate required fields
        if not persona.name or not persona.name.strip():
            raise PersonaManagerError("ペルソナ名が設定されていません")

        if persona.age is None or persona.age < 0 or persona.age > 150:
            raise PersonaManagerError("年齢は0から150の範囲で設定してください")

        if not persona.occupation or not persona.occupation.strip():
            raise PersonaManagerError("職業が設定されていません")

        if not persona.background or not persona.background.strip():
            raise PersonaManagerError("背景が設定されていません")

        # Validate list fields
        if not persona.values or len(persona.values) == 0:
            raise PersonaManagerError("価値観が設定されていません")

        if not persona.pain_points or len(persona.pain_points) == 0:
            raise PersonaManagerError("課題・悩みが設定されていません")

        if not persona.goals or len(persona.goals) == 0:
            raise PersonaManagerError("目標・願望が設定されていません")

        # Validate list content
        for value in persona.values:
            if not value or not value.strip():
                raise PersonaManagerError("価値観に空の項目があります")

        for pain_point in persona.pain_points:
            if not pain_point or not pain_point.strip():
                raise PersonaManagerError("課題・悩みに空の項目があります")

        for goal in persona.goals:
            if not goal or not goal.strip():
                raise PersonaManagerError("目標・願望に空の項目があります")

        # Validate field lengths
        if len(persona.name) > 100:
            raise PersonaManagerError("ペルソナ名は100文字以内で設定してください")

        if len(persona.occupation) > 200:
            raise PersonaManagerError("職業は200文字以内で設定してください")

        if len(persona.background) > 2000:
            raise PersonaManagerError("背景は2000文字以内で設定してください")

        # Validate list sizes
        if len(persona.values) > 10:
            raise PersonaManagerError("価値観は10項目以内で設定してください")

        if len(persona.pain_points) > 10:
            raise PersonaManagerError("課題・悩みは10項目以内で設定してください")

        if len(persona.goals) > 10:
            raise PersonaManagerError("目標・願望は10項目以内で設定してください")

        # Validate demographic fields (optional, only when set)
        if persona.gender is not None and persona.gender not in VALID_GENDERS:
            raise PersonaManagerError(
                f"性別は {', '.join(sorted(VALID_GENDERS))} のいずれかで設定してください"
            )

        if persona.country and not country_service.is_valid_country(persona.country):
            # ISO 3166-1 alpha-2 として実在する国コードのみ許可（架空コード XX や
            # alpha-3 JPN を弾く）。検証は pycountry ベースの country_service に委譲。
            raise PersonaManagerError(
                "国はISO 3166-1 alpha-2の実在する国コードで設定してください"
            )

        if persona.city and len(persona.city) > 100:
            raise PersonaManagerError("居住都市は100文字以内で設定してください")

        if persona.tags:
            if len(persona.tags) > 20:
                raise PersonaManagerError("タグは20個以内で設定してください")
            for tag in persona.tags:
                if not tag or not tag.strip():
                    raise PersonaManagerError("タグに空の項目があります")
                if len(tag) > 50:
                    raise PersonaManagerError(
                        "タグは1個あたり50文字以内で設定してください"
                    )
                # data属性ではカンマ区切りでフィルタに渡すため、タグ内のカンマを禁止
                if "," in tag:
                    raise PersonaManagerError("タグにカンマ（,）は使用できません")

    # ============================================
    # Memory Management Methods
    # ============================================

    def add_persona_knowledge(
        self, persona_id: str, topic_name: str, topic_content: str
    ) -> str:
        """
        ペルソナに手動で知識（Semantic Memory）を追加する。

        短期記憶を経由せず、直接長期記憶（LTM）に保存する。

        Args:
            persona_id: ペルソナID
            topic_name: トピック名（例: 好きな食べ物）
            topic_content: トピック内容（例: ラーメンが好き）

        Returns:
            保存された記憶のID

        Raises:
            PersonaManagerError: 追加に失敗した場合
        """
        # 入力検証
        if not persona_id or not persona_id.strip():
            raise PersonaManagerError("ペルソナIDが無効です")

        if not topic_name or not topic_name.strip():
            raise PersonaManagerError("トピック名を入力してください")

        if not topic_content or not topic_content.strip():
            raise PersonaManagerError("内容を入力してください")

        if len(topic_name) > 100:
            raise PersonaManagerError("トピック名は100文字以内で設定してください")

        if len(topic_content) > 10000:
            raise PersonaManagerError("内容は10000文字以内で設定してください")

        # ペルソナの存在確認
        persona = self.get_persona(persona_id)
        if not persona:
            raise PersonaManagerError("ペルソナが見つかりません")

        # メモリサービスを取得
        memory_service = service_factory.get_memory_service()

        if not memory_service:
            raise PersonaManagerError("長期記憶機能が無効です")

        # Semantic戦略が有効か確認
        if not memory_service._semantic_strategy:
            raise PersonaManagerError(
                "Semantic記憶戦略が設定されていません。"
                "SEMANTIC_MEMORY_STRATEGY_IDを設定してください。"
            )

        try:
            # トピック形式でコンテンツを構築
            topic_name_clean = topic_name.strip()
            topic_content_clean = topic_content.strip()
            formatted_content = (
                f'<topic name="{topic_name_clean}">{topic_content_clean}</topic>'
            )

            # 直接LTMに保存（短期記憶を経由しない）
            memory_id = memory_service._semantic_strategy.save_directly_to_ltm(
                actor_id=persona_id,
                content=formatted_content,
                metadata={"source": "manual", "topic_name": topic_name_clean},
            )

            self.logger.info(
                f"Knowledge added directly to LTM for persona {persona_id}: {topic_name_clean} (memory_id: {memory_id})"
            )

            return memory_id

        except Exception as e:
            error_msg = f"知識の追加中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

    def get_persona_memories(
        self,
        persona_id: str,
        strategy_type: str = "summary",
        page: int = 1,
        per_page: int = 10,
    ) -> tuple:
        """
        ペルソナの記憶を取得する。

        Args:
            persona_id: ペルソナID
            strategy_type: 戦略タイプ（"summary" または "semantic"）
            page: ページ番号
            per_page: 1ページあたりの件数

        Returns:
            (memories, current_page, total_pages) のタプル

        Raises:
            PersonaManagerError: 取得に失敗した場合
        """

        if not persona_id or not persona_id.strip():
            raise PersonaManagerError("ペルソナIDが無効です")

        # ペルソナの存在確認
        persona = self.get_persona(persona_id)
        if not persona:
            raise PersonaManagerError("ペルソナが見つかりません")

        # メモリサービスを取得
        memory_service = service_factory.get_memory_service()

        if not memory_service:
            # 長期記憶機能が無効の場合は空を返す
            return ([], 1, 1)

        try:
            # 全記憶を取得
            all_memories = memory_service.list_memories(actor_id=persona_id)

            # 戦略タイプでフィルタリング
            filtered_memories = [
                m
                for m in all_memories
                if m.metadata and m.metadata.get("strategy_type") == strategy_type
            ]

            # 作成日時でソート（新しい順）
            filtered_memories.sort(key=lambda m: m.created_at, reverse=True)

            # ページネーション計算
            total_count = len(filtered_memories)
            total_pages = max(1, (total_count + per_page - 1) // per_page)
            page = max(1, min(page, total_pages))

            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            page_memories = filtered_memories[start_idx:end_idx]

            # 各記憶にパース済みトピック情報を付与
            for memory in page_memories:
                memory.parsed_topic = self._parse_topic_content(memory.content)

            return (page_memories, page, total_pages)

        except Exception as e:
            error_msg = f"記憶の取得中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

    def _parse_topic_content(self, content: str) -> Optional[dict]:
        """
        <topic name="...">...</topic> 形式のコンテンツをパース

        Returns:
            パース成功時: {"name": トピック名, "content": 内容}
            パース失敗時: None
        """
        import re

        pattern = r'<topic\s+name="([^"]+)">\s*(.*?)\s*</topic>'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            return {"name": match.group(1), "content": match.group(2).strip()}
        return None

    # --- メモリ削除操作 ---

    def delete_persona_memory(self, persona_id: str, memory_id: str) -> bool:
        """
        ペルソナの特定の記憶を削除する。

        Args:
            persona_id: ペルソナID
            memory_id: 記憶ID

        Returns:
            削除成功ならTrue

        Raises:
            PersonaManagerError: 削除に失敗した場合
        """
        memory_service = service_factory.get_memory_service()
        if not memory_service:
            raise PersonaManagerError("長期記憶機能が無効です")

        try:
            success = memory_service.delete_memory(
                actor_id=persona_id, memory_id=memory_id
            )
            if success:
                self.logger.info(f"Memory {memory_id} deleted for persona {persona_id}")
            return success
        except (ConnectionError, TimeoutError) as e:
            raise PersonaManagerError("記憶サービスへの接続に失敗しました。") from e
        except Exception as e:
            error_msg = f"記憶の削除中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg) from e

    def delete_all_persona_memories(
        self, persona_id: str, strategy_type: str = "summary"
    ) -> int:
        """
        ペルソナの指定戦略タイプの全記憶を削除する。

        Args:
            persona_id: ペルソナID
            strategy_type: 戦略タイプ（"summary" または "semantic"）

        Returns:
            削除件数

        Raises:
            PersonaManagerError: 削除に失敗した場合
        """
        memory_service = service_factory.get_memory_service()
        if not memory_service:
            raise PersonaManagerError("長期記憶機能が無効です")

        try:
            all_memories = memory_service.list_memories(actor_id=persona_id)
            memories_to_delete = [
                m
                for m in all_memories
                if m.metadata and m.metadata.get("strategy_type") == strategy_type
            ]

            deleted_count = 0
            for memory in memories_to_delete:
                try:
                    if memory_service.delete_memory(
                        actor_id=persona_id, memory_id=memory.id
                    ):
                        deleted_count += 1
                except Exception as e:
                    self.logger.warning(f"Failed to delete memory {memory.id}: {e}")

            self.logger.info(
                f"Deleted {deleted_count} {strategy_type} memories for persona {persona_id}"
            )
            return deleted_count

        except (ConnectionError, TimeoutError) as e:
            raise PersonaManagerError("記憶サービスへの接続に失敗しました。") from e
        except Exception as e:
            error_msg = f"全記憶の削除中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg) from e

    def safe_get_memories(
        self, persona_id: str, strategy_type: str = "summary"
    ) -> list:
        """
        エラー時に安全に記憶リストを取得するヘルパー。

        Args:
            persona_id: ペルソナID
            strategy_type: 戦略タイプ

        Returns:
            記憶リスト（取得失敗時は空リスト）
        """
        try:
            memory_service = service_factory.get_memory_service()
            if memory_service:
                all_memories = memory_service.list_memories(actor_id=persona_id)
                memories = [
                    m
                    for m in all_memories
                    if m.metadata and m.metadata.get("strategy_type") == strategy_type
                ]
                memories.sort(key=lambda m: m.created_at, reverse=True)
                return memories
        except Exception as e:
            self.logger.warning(f"Failed to retrieve memories for error recovery: {e}")
        return []

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
                        f"カラム「{key_name}」はデータセットに存在しません"
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
