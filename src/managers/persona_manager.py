"""
Persona Manager for AI Persona System.
Handles persona generation workflow, editing, and saving functionality.
"""

import logging
from typing import List, Optional

from ..models.persona import Persona
from ..services.ai_service import AIService, AIServiceError
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
    ):
        """
        Initialize persona manager.

        Args:
            ai_service: AI service instance for persona generation (optional, uses singleton if not provided)
            database_service: Database service instance for persistence (optional, uses singleton if not provided)
        """
        self.logger = logging.getLogger(__name__)

        # Use singleton services if not provided
        self.ai_service = ai_service or service_factory.get_ai_service()
        self.database_service = (
            database_service or service_factory.get_database_service()
        )

    def generate_persona_from_interview(self, interview_text: str) -> Persona:
        """
        Generate a persona from N1 interview text.

        This method implements the complete persona generation workflow:
        1. Validates input interview text
        2. Uses AI service to generate persona
        3. Returns the generated persona object

        Args:
            interview_text: N1 interview text content

        Returns:
            Persona: Generated persona object

        Raises:
            PersonaManagerError: If persona generation fails
        """
        if not interview_text or not interview_text.strip():
            raise PersonaManagerError("インタビューテキストが空です")

        # Validate interview text length
        if len(interview_text.strip()) < 50:
            raise PersonaManagerError(
                "インタビューテキストが短すぎます。より詳細な内容が必要です"
            )

        if len(interview_text) > 50000:  # 50KB limit
            raise PersonaManagerError(
                "インタビューテキストが長すぎます。50,000文字以内で入力してください"
            )

        self.logger.info(
            f"Starting persona generation from interview text (length: {len(interview_text)} chars)"
        )

        try:
            # Generate persona using AI service
            persona = self.ai_service.generate_persona(interview_text)

            # Validate generated persona
            self._validate_generated_persona(persona)

            self.logger.info(
                f"Persona generation completed successfully: {persona.name} (ID: {persona.id})"
            )
            return persona

        except AIServiceError as e:
            error_msg = f"AI service error during persona generation: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error during persona generation: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

    def generate_personas(
        self,
        file_contents: list[tuple[bytes, str]],
        data_type: str,
        persona_count: int,
        data_description: str | None = None,
        custom_prompt: str | None = None,
    ) -> list[Persona]:
        """
        統一ペルソナ生成

        Args:
            file_contents: (ファイル内容, ファイル名) のリスト
            data_type: データ種別 (interview, market_report, review, purchase, other)
            persona_count: 生成数 (1-10)
            data_description: データ説明（data_type="other"時）
            custom_prompt: カスタムプロンプト

        Returns:
            list[Persona]: 生成されたペルソナリスト
        """
        from ..services.agent_service import AgentService, AgentServiceError

        if persona_count < 1 or persona_count > 10:
            raise PersonaManagerError("ペルソナ数は1-10の範囲で指定してください")

        if not file_contents:
            raise PersonaManagerError("ファイルが選択されていません")

        self.logger.info(
            f"統一ペルソナ生成開始 (data_type={data_type}, count={persona_count}, files={len(file_contents)})"
        )

        try:
            # 全ファイルからテキスト抽出・結合
            from ..managers.file_manager import FileManager, FileUploadError

            file_manager = FileManager()
            texts = []
            for content, filename in file_contents:
                text = file_manager.extract_text_from_file(content, filename)
                texts.append(f"--- {filename} ---\n{text}")
            combined_text = "\n\n".join(texts)

            # CSV系データはMCPを使用
            use_mcp = data_type in ("purchase", "review") and any(
                fn.lower().endswith(".csv") for _, fn in file_contents
            )

            agent_service = AgentService()
            personas = agent_service.generate_personas_with_agent(
                data_text=combined_text,
                data_type=data_type,
                persona_count=persona_count,
                data_description=data_description,
                custom_prompt=custom_prompt,
                use_mcp=use_mcp,
            )

            self.logger.info(f"統一ペルソナ生成完了: {len(personas)}個")
            return personas

        except FileUploadError as e:
            raise PersonaManagerError(f"ファイル処理エラー: {e}")
        except AgentServiceError as e:
            raise PersonaManagerError(f"エージェントサービスエラー: {e}")
        except Exception as e:
            raise PersonaManagerError(f"予期しないエラー: {e}")

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

    def get_all_personas(self) -> List[Persona]:
        """
        Retrieve all personas from the database.

        Returns:
            List of Persona objects ordered by creation date (newest first)

        Raises:
            PersonaManagerError: If retrieval operation fails
        """
        try:
            personas = self.database_service.get_all_personas()
            self.logger.info(f"Retrieved {len(personas)} personas from database")
            return personas

        except DatabaseError as e:
            error_msg = f"Database error while retrieving all personas: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error while retrieving all personas: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

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
            personas = self.get_all_personas()
            return len(personas)

        except PersonaManagerError:
            # Re-raise PersonaManagerError
            raise
        except Exception as e:
            error_msg = f"Unexpected error while getting persona count: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

    def persona_exists(self, persona_id: str) -> bool:
        """
        Check if a persona exists in the database.

        Args:
            persona_id: ID of the persona to check

        Returns:
            True if persona exists, False otherwise

        Raises:
            PersonaManagerError: If check operation fails
        """
        if not persona_id or not persona_id.strip():
            return False

        try:
            persona = self.get_persona(persona_id)
            return persona is not None

        except PersonaManagerError:
            # Re-raise PersonaManagerError
            raise
        except Exception as e:
            error_msg = f"Unexpected error while checking persona existence: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)

    def search_personas(self, query: str) -> List[Persona]:
        """
        Search personas by name, occupation, or background content.

        Args:
            query: Search query string

        Returns:
            List of matching Persona objects

        Raises:
            PersonaManagerError: If search operation fails
        """
        if not query or not query.strip():
            return []

        try:
            all_personas = self.get_all_personas()
            query_lower = query.strip().lower()

            matching_personas = []
            for persona in all_personas:
                # Search in name, occupation, and background
                if (
                    query_lower in persona.name.lower()
                    or query_lower in persona.occupation.lower()
                    or query_lower in persona.background.lower()
                ):
                    matching_personas.append(persona)

            self.logger.info(
                f"Found {len(matching_personas)} personas matching query: '{query}'"
            )
            return matching_personas

        except PersonaManagerError:
            # Re-raise PersonaManagerError
            raise
        except Exception as e:
            error_msg = f"Unexpected error while searching personas: {e}"
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

    def generate_personas_from_market_report(
        self, file_content: bytes, filename: str, persona_count: int
    ) -> List[Persona]:
        """
        市場調査レポートなどから複数のペルソナを生成

        このメソッドは以下のワークフローを実装します：
        1. ファイルからテキストを抽出（PDF/Word/テキスト対応）
        2. テキスト内容を検証
        3. AgentServiceを使用して複数ペルソナを生成
        4. 生成されたペルソナを返却

        Args:
            file_content: ファイル内容（バイト）
            filename: ファイル名
            persona_count: 生成するペルソナの数（1-10）

        Returns:
            List[Persona]: 生成されたペルソナのリスト

        Raises:
            PersonaManagerError: ペルソナ生成に失敗した場合
        """
        from ..managers.file_manager import FileManager, FileUploadError
        from ..services.agent_service import AgentService, AgentServiceError

        # 入力検証
        if persona_count < 1 or persona_count > 10:
            raise PersonaManagerError("ペルソナ数は1-10の範囲で指定してください")

        self.logger.info(
            f"調査、分析レポートから{persona_count}人のペルソナ生成を開始 "
            f"(ファイル: {filename}, サイズ: {len(file_content)} bytes)"
        )

        try:
            # ファイルマネージャーを使用してテキストを抽出
            file_manager = FileManager()
            report_text = file_manager.extract_text_from_file(file_content, filename)

            # テキスト長の検証
            text_length = len(report_text.strip())
            if text_length < 100:
                raise PersonaManagerError(
                    "レポート内容が短すぎます。より詳細な市場調査レポートが必要です"
                )

            if text_length > 100000:  # 100KB limit
                raise PersonaManagerError(
                    "レポート内容が長すぎます。100,000文字以内のレポートをアップロードしてください"
                )

            self.logger.info(f"レポートテキスト抽出完了 (長さ: {text_length} 文字)")

            # AgentServiceを使用して複数ペルソナを生成
            agent_service = AgentService()
            personas = agent_service.generate_personas_from_report(
                report_text, persona_count
            )

            # 生成されたペルソナを検証
            for i, persona in enumerate(personas, 1):
                self._validate_generated_persona(persona)
                self.logger.info(
                    f"ペルソナ {i}/{len(personas)} 検証完了: {persona.name}"
                )

            self.logger.info(
                f"市場調査レポートから{len(personas)}個のペルソナ生成が完了しました"
            )
            return personas

        except FileUploadError as e:
            error_msg = f"ファイル処理エラー: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)
        except AgentServiceError as e:
            error_msg = f"エージェントサービスエラー: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)
        except Exception as e:
            error_msg = f"予期しないエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaManagerError(error_msg)
