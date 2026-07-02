"""
ペルソナ記憶管理Manager
長期記憶の参照・追加・削除を担当する。
"""

import logging
import re
from typing import Optional

from ..services.database_service import DatabaseService, DatabaseError
from ..services.memory.memory_service import MemoryService, MemoryServiceError
from ..services.service_factory import service_factory


class PersonaMemoryManagerError(Exception):
    """記憶管理操作エラー"""

    pass


class PersonaMemoryManager:
    """ペルソナの記憶管理を行うManager"""

    def __init__(
        self,
        database_service: Optional[DatabaseService] = None,
        memory_service: Optional[MemoryService] = None,
    ):
        """
        Args:
            database_service: ペルソナ存在確認用（optional, service_factory経由）
            memory_service: 記憶操作用（optional, service_factory経由。Noneの場合は遅延取得）
        """
        self.logger = logging.getLogger(__name__)
        self.database_service = (
            database_service or service_factory.get_database_service()
        )
        self._memory_service = memory_service
        self._memory_service_resolved = memory_service is not None

    def add_knowledge(
        self,
        persona_id: str,
        topic_name: str,
        topic_content: str,
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
            PersonaMemoryManagerError: バリデーション失敗、ペルソナ不在、保存失敗時
        """
        self._validate_persona_id(persona_id)

        if not topic_name or not topic_name.strip():
            raise PersonaMemoryManagerError("トピック名を入力してください")

        if not topic_content or not topic_content.strip():
            raise PersonaMemoryManagerError("内容を入力してください")

        if len(topic_name) > 100:
            raise PersonaMemoryManagerError("トピック名は100文字以内で設定してください")

        if len(topic_content) > 10000:
            raise PersonaMemoryManagerError("内容は10000文字以内で設定してください")

        self._validate_persona_exists(persona_id)

        memory_service = self._get_memory_service()

        if not memory_service.is_semantic_enabled:
            raise PersonaMemoryManagerError(
                "Semantic記憶戦略が設定されていません。"
                "SEMANTIC_MEMORY_STRATEGY_IDを設定してください。"
            )

        try:
            formatted_content = self._format_topic_content(
                topic_name.strip(), topic_content.strip()
            )

            memory_id = memory_service.save_knowledge(
                actor_id=persona_id,
                content=formatted_content,
                metadata={"source": "manual", "topic_name": topic_name.strip()},
            )

            self.logger.info(
                f"Knowledge added for persona {persona_id}: {topic_name.strip()} (memory_id: {memory_id})"
            )
            return memory_id

        except MemoryServiceError as e:
            error_msg = f"知識の追加中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaMemoryManagerError(error_msg) from e
        except Exception as e:
            error_msg = f"知識の追加中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaMemoryManagerError(error_msg) from e

    def get_memories(
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
            PersonaMemoryManagerError: ペルソナ不在、取得失敗時
        """
        self._validate_persona_id(persona_id)

        memory_service = self._resolve_memory_service()
        if not memory_service:
            return ([], 1, 1)

        self._validate_persona_exists(persona_id)

        try:
            all_memories = memory_service.list_memories(actor_id=persona_id)

            filtered_memories = [
                m
                for m in all_memories
                if m.metadata and m.metadata.get("strategy_type") == strategy_type
            ]

            filtered_memories.sort(key=lambda m: m.created_at, reverse=True)

            total_count = len(filtered_memories)
            total_pages = max(1, (total_count + per_page - 1) // per_page)
            page = max(1, min(page, total_pages))

            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            page_memories = filtered_memories[start_idx:end_idx]

            for memory in page_memories:
                memory.parsed_topic = self._parse_topic_content(memory.content)

            return (page_memories, page, total_pages)

        except MemoryServiceError as e:
            error_msg = f"記憶の取得中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaMemoryManagerError(error_msg) from e
        except Exception as e:
            error_msg = f"記憶の取得中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaMemoryManagerError(error_msg) from e

    def delete_memory(self, persona_id: str, memory_id: str) -> bool:
        """
        ペルソナの特定の記憶を削除する。

        Args:
            persona_id: ペルソナID
            memory_id: 記憶ID

        Returns:
            削除成功ならTrue

        Raises:
            PersonaMemoryManagerError: 機能無効、削除失敗時
        """
        memory_service = self._get_memory_service()

        try:
            success = memory_service.delete_memory(
                actor_id=persona_id, memory_id=memory_id
            )
            if success:
                self.logger.info(f"Memory {memory_id} deleted for persona {persona_id}")
            return success
        except (ConnectionError, TimeoutError) as e:
            raise PersonaMemoryManagerError(
                "記憶サービスへの接続に失敗しました。"
            ) from e
        except MemoryServiceError as e:
            error_msg = f"記憶の削除中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaMemoryManagerError(error_msg) from e
        except Exception as e:
            error_msg = f"記憶の削除中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaMemoryManagerError(error_msg) from e

    def delete_all_memories(
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
            PersonaMemoryManagerError: 機能無効、削除失敗時
        """
        memory_service = self._get_memory_service()

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
            raise PersonaMemoryManagerError(
                "記憶サービスへの接続に失敗しました。"
            ) from e
        except MemoryServiceError as e:
            error_msg = f"全記憶の削除中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaMemoryManagerError(error_msg) from e
        except Exception as e:
            error_msg = f"全記憶の削除中にエラーが発生しました: {e}"
            self.logger.error(error_msg)
            raise PersonaMemoryManagerError(error_msg) from e

    def safe_get_memories(
        self, persona_id: str, strategy_type: str = "summary"
    ) -> list:
        """
        エラー時に安全に記憶リストを取得するヘルパー。
        取得失敗時は空リストを返す（例外を投げない）。
        """
        try:
            memory_service = self._resolve_memory_service()
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

    # --- 内部メソッド ---

    def _get_memory_service(self) -> MemoryService:
        """MemoryServiceを取得。未設定の場合はservice_factory経由で遅延取得。
        取得不可の場合は PersonaMemoryManagerError を投げる。
        """
        if not self._memory_service_resolved:
            self._memory_service = service_factory.get_memory_service()
            self._memory_service_resolved = True

        if not self._memory_service:
            raise PersonaMemoryManagerError("長期記憶機能が無効です")

        return self._memory_service

    def _resolve_memory_service(self) -> Optional[MemoryService]:
        """MemoryServiceを取得。無効な場合はNoneを返す（例外なし）。"""
        if not self._memory_service_resolved:
            self._memory_service = service_factory.get_memory_service()
            self._memory_service_resolved = True
        return self._memory_service

    def _validate_persona_id(self, persona_id: str) -> None:
        """persona_idの空文字/None検証"""
        if not persona_id or not persona_id.strip():
            raise PersonaMemoryManagerError("ペルソナIDが無効です")

    def _validate_persona_exists(self, persona_id: str) -> None:
        """ペルソナの存在を確認する。不在の場合はエラー。"""
        try:
            persona = self.database_service.get_persona(persona_id.strip())
            if not persona:
                raise PersonaMemoryManagerError("ペルソナが見つかりません")
        except DatabaseError as e:
            raise PersonaMemoryManagerError(f"ペルソナの確認に失敗しました: {e}") from e

    def _format_topic_content(self, topic_name: str, topic_content: str) -> str:
        """トピック形式のコンテンツを構築"""
        return f'<topic name="{topic_name}">{topic_content}</topic>'

    def _parse_topic_content(self, content: str) -> Optional[dict]:
        """<topic name="...">...</topic> 形式のコンテンツをパース"""
        pattern = r'<topic\s+name="([^"]+)">\s*(.*?)\s*</topic>'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            return {"name": match.group(1), "content": match.group(2).strip()}
        return None
