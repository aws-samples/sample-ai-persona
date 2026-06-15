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

    def generate_personas(
        self,
        file_contents: list[tuple[bytes, str]],
        data_type: str,
        persona_count: int,
        data_description: str | None = None,
        custom_prompt: str | None = None,
        event_queue: Any = None,
        auto_link_behavior: bool = False,
    ) -> tuple[list[Persona], list[dict[str, str]]]:
        """
        統一ペルソナ生成

        Args:
            file_contents: (ファイル内容, ファイル名) のリスト
            data_type: データ種別 (interview, market_report, review, purchase, other, dwh)
            persona_count: 生成数 (1-10)
            data_description: データ説明（data_type="other"時）/ 分析の切り口（data_type="dwh"時）
            custom_prompt: カスタムプロンプト
            event_queue: リアルタイムイベント用 queue（DWH 用）
            auto_link_behavior: 行動データ自動紐付けオプション（DWH時のみ有効）

        Returns:
            list[Persona]: 生成されたペルソナリスト
        """
        from ..services.agent_service import AgentService, AgentServiceError

        if persona_count < 1 or persona_count > 10:
            raise PersonaManagerError("ペルソナ数は1-10の範囲で指定してください")

        # DWH（データ分析エージェント連携）の場合はファイル不要
        if data_type == "dwh":
            return self._generate_personas_from_dwh(
                analysis_angle=data_description or "",
                persona_count=persona_count,
                custom_prompt=custom_prompt,
                event_queue=event_queue,
                auto_link_behavior=auto_link_behavior,
            )

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
            csv_temp_paths: list[str] = []

            for content, filename in file_contents:
                if filename.lower().endswith(".csv"):
                    # CSVはMCP分析用に一時ファイルとして保存
                    import os

                    # エンコーディング検出してUTF-8で保存
                    for encoding in ("utf-8", "shift_jis", "euc-jp"):
                        try:
                            decoded = content.decode(encoding)
                            break
                        except UnicodeDecodeError:
                            continue
                    else:
                        raise PersonaManagerError(
                            "CSVファイルのエンコーディングを検出できません"
                        )

                    # /tmp直下にシンプルなパスで保存（LLMがパスを正確にコピーできるように）
                    import uuid

                    csv_path = f"/tmp/persona_csv_{uuid.uuid4().hex[:8]}.csv"
                    with open(csv_path, "w", encoding="utf-8") as f:
                        f.write(decoded)
                    csv_temp_paths.append(csv_path)

                    # プレビュー（先頭20行）をテキストとして追加
                    lines = decoded.splitlines()
                    preview = "\n".join(lines[:20])
                    if len(lines) > 20:
                        preview += f"\n... (全{len(lines)}行)"
                    texts.append(
                        f"--- {filename} (CSV, 全データは分析ツールで参照可能) ---\n{preview}"
                    )
                else:
                    text = file_manager.extract_text_from_file(content, filename)
                    texts.append(f"--- {filename} ---\n{text}")

            combined_text = "\n\n".join(texts)

            # CSV系データはMCPを使用
            use_mcp = len(csv_temp_paths) > 0

            try:
                agent_service = AgentService()
                personas, thinking_log = agent_service.generate_personas_with_agent(
                    data_text=combined_text,
                    data_type=data_type,
                    persona_count=persona_count,
                    data_description=data_description,
                    custom_prompt=custom_prompt,
                    use_mcp=use_mcp,
                    csv_paths=csv_temp_paths if csv_temp_paths else None,
                )
            finally:
                # 一時CSVファイルを削除
                import os

                for p in csv_temp_paths:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

            for persona in personas:
                self._validate_generated_persona(persona)

            self.logger.info(f"統一ペルソナ生成完了: {len(personas)}個")
            return personas, thinking_log

        except FileUploadError as e:
            raise PersonaManagerError(f"ファイル処理エラー: {e}")
        except AgentServiceError as e:
            raise PersonaManagerError(f"エージェントサービスエラー: {e}")
        except Exception as e:
            raise PersonaManagerError(f"予期しないエラー: {e}")

    def _generate_personas_from_dwh(
        self,
        analysis_angle: str,
        persona_count: int,
        custom_prompt: str | None = None,
        event_queue: Any = None,
        auto_link_behavior: bool = False,
    ) -> tuple[list[Persona], list[dict[str, str]]]:
        """DWH（データ分析エージェント連携）によるペルソナ生成。

        Agent が ask_data_agent ツールで データ分析エージェントに自律的に問い合わせてペルソナを生成する。
        event_queue が渡された場合、Agent のイベントをリアルタイムで queue に入れる。
        auto_link_behavior が True の場合、特定ユーザー深掘り型に限定し、行動データCSV抽出も指示する。
        """
        from ..services.agent_service import AgentService, AgentServiceError

        if not analysis_angle or not analysis_angle.strip():
            raise PersonaManagerError("分析の切り口を入力してください")

        if auto_link_behavior:
            persona_count = 1

        self.logger.info(
            f"DWH ペルソナ生成開始 (angle={analysis_angle!r}, count={persona_count}, auto_link={auto_link_behavior})"
        )

        # callback_handler: Agent イベントを queue に流す
        callback_handler = None
        if event_queue is not None:

            def _queue_callback(**kwargs: Any) -> None:
                data = kwargs.get("data", "")
                complete = kwargs.get("complete", False)

                if data:
                    event_queue.put({"type": "thinking", "content": data})
                if complete and data:
                    event_queue.put({"type": "thinking_done", "content": ""})

            callback_handler = _queue_callback

        try:
            agent_service = AgentService()
            data_text = f"分析の切り口: {analysis_angle}"

            if auto_link_behavior:
                data_text += (
                    "\n\n# ★重要: ペルソナ生成方式\n"
                    "※ 本タスクでは複数ペルソナの比較・統計的集約ではなく、特定1名の深掘り分析を行います。\n"
                    "条件に最も合致する実在ユーザー1名をDWHから特定し、そのユーザーの実データに基づいて\n"
                    "属性・行動パターンを詳細に分析してペルソナ化してください。\n"
                    "\n# 追加タスク: 行動データCSVエクスポート（必須）\n"
                    "ペルソナ生成後、選定したユーザーの行動データをCSVファイルとしてエクスポートしてください。\n"
                    "このCSVはAIがそのペルソナになりきる際の参照データとなります。\n\n"
                    "## エクスポートルール\n"
                    "1. 各データ種別ごとに ask_data_agent へCSV出力を依頼する（1回 = 1種別）\n"
                    "   ★ 必ず「CSVで出力してください」というフレーズを含めること（これがないとCSVファイルが生成されません）\n"
                    "2. 各CSVには必ずそのユーザーを識別するキーカラムを含めること\n"
                    "3. ★単一テーブルのIDや外部キーだけのデータは不十分です。\n"
                    "   関連テーブルをJOINし、人間が読んで意味のわかるリッチな情報（名称、カテゴリ、日時、金額など）を含めてください。\n"
                    "   AIがこのデータだけを見てそのユーザーの行動を具体的に語れるレベルが目標です。\n"
                    "4. 複数種別がある場合は種別ごとに個別にCSV出力を依頼する\n\n"
                    "## 依頼例\n"
                    '  ask_data_agent("<ユーザーID条件> の購買履歴をCSVで出力してください。\n'
                    "  関連テーブルをJOINして、日時・商品名・カテゴリ・数量・金額など具体的な情報を含めてください。\n"
                    '  識別キーカラムも含めてください")\n'
                )

            personas, thinking_log = agent_service.generate_personas_with_agent(
                data_text=data_text,
                data_type="dwh",
                persona_count=persona_count,
                custom_prompt=custom_prompt,
                callback_handler=callback_handler,
                event_queue=event_queue,
            )

            for persona in personas:
                self._validate_generated_persona(persona)

            self.logger.info(f"DWH ペルソナ生成完了: {len(personas)}個")
            return personas, thinking_log

        except AgentServiceError as e:
            raise PersonaManagerError(f"データ分析エージェント連携エラー: {e}")
        except Exception as e:
            raise PersonaManagerError(f"DWH ペルソナ生成エラー: {e}")

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
            all_personas = self.get_all_personas_full()
            query_lower = query.strip().lower()

            matching_personas = []
            for persona in all_personas:
                # Search in name, occupation, background, country, and tags
                tags_text = " ".join(persona.tags).lower() if persona.tags else ""
                if (
                    query_lower in persona.name.lower()
                    or query_lower in persona.occupation.lower()
                    or query_lower in persona.background.lower()
                    or (persona.country and query_lower in persona.country.lower())
                    or query_lower in tags_text
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

