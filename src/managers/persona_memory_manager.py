"""
ペルソナ記憶管理Manager
長期記憶の参照・追加・削除を担当する。
"""

import logging
import re
from typing import Optional

from ..models.authz import Principal, ResourceType
from ..models.errors import (
    AccessDeniedError,
    CodedError,
    ErrorCode,
    LookupUnavailableError,
)
from ..services.database_service import DatabaseService
from ..services.memory.memory_service import MemoryService, MemoryServiceError
from ..services.service_factory import service_factory
from .components.access_policy import AccessPolicy
from .components.resource_lookup import ResourceLookup
from .components.sharing_runtime import build_access_parts, shared_access_parts

#: 1 つの知識の内容の上限（手入力でもファイルの変換結果でも同じ）
MAX_KNOWLEDGE_CONTENT_CHARS = 10000


class PersonaMemoryManagerError(CodedError):
    """記憶管理操作エラー"""

    pass


def prepare_long_term_memory() -> None:
    """Resolve AgentCore Memory strategy IDs once at application startup.

    Delegates to the Service layer (``GetMemory``); a failed lookup turns
    long-term memory off instead of stopping the app.
    """
    service_factory.resolve_memory_strategies()


class PersonaMemoryManager:
    """ペルソナの記憶管理を行うManager

    v1.0.0 U5（BR4.1、AC4.2.4）: メモリ画面の入口はどれも、ペルソナを管理できる人
    （所有者と admin）だけが使える。共有されているだけの人と読めない人には、どちらも
    PERSONA_NOT_FOUND（404）を返す（読み取りの 403 は存在を知らせるため）。親の
    ペルソナは ResourceLookup で読み、判定は AccessPolicy に任せる。
    """

    def __init__(
        self,
        database_service: Optional[DatabaseService] = None,
        memory_service: Optional[MemoryService] = None,
        resource_lookup: Optional[ResourceLookup] = None,
    ):
        """
        Args:
            database_service: 互換のため受け取る（optional, service_factory経由）
            memory_service: 記憶操作用（optional, service_factory経由。Noneの場合は遅延取得）
            resource_lookup: Decision reads of personas (optional; the per-task
                instance)
        """
        self.logger = logging.getLogger(__name__)
        self.database_service = (
            database_service or service_factory.get_database_service()
        )
        self._memory_service = memory_service
        self._memory_service_resolved = memory_service is not None
        self._lookup = resource_lookup

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

    def ensure_memory_manageable(self, principal: Principal, persona_id: str) -> None:
        """Let only people who manage the persona use its memory screen (BR4.1).

        The knowledge-file preview calls this before converting the file.

        Raises:
            PersonaMemoryManagerError: ``PERSONA_ID_INVALID`` for a blank ID;
                ``PERSONA_NOT_FOUND`` when the user cannot manage the persona
                (shared readers included), or it does not exist
            LookupUnavailableError: The persona could not be read (503)
        """
        self._ensure_manageable(principal, persona_id)

    def check_knowledge_file_content(self, content: str) -> None:
        """Reject a converted knowledge file longer than one knowledge entry may be.

        The preview runs this so a file that ``add_knowledge`` would refuse is
        refused before the user edits it. Pure length check; no I/O.

        Raises:
            PersonaMemoryManagerError: ``KNOWLEDGE_FILE_CONTENT_TOO_LONG`` with
                ``max_length`` and ``content_length`` in the context
        """
        if len(content) > MAX_KNOWLEDGE_CONTENT_CHARS:
            raise PersonaMemoryManagerError(
                f"converted knowledge length {len(content)} exceeds "
                f"{MAX_KNOWLEDGE_CONTENT_CHARS}",
                code=ErrorCode.KNOWLEDGE_FILE_CONTENT_TOO_LONG,
                context={
                    "max_length": MAX_KNOWLEDGE_CONTENT_CHARS,
                    "content_length": len(content),
                },
            )

    def _ensure_manageable(self, principal: Principal, persona_id: str) -> None:
        self._validate_persona_id(persona_id)
        target = self._resource_lookup.get(ResourceType.PERSONA, persona_id.strip())
        try:
            AccessPolicy.ensure_manageable(
                principal, target, ErrorCode.PERSONA_NOT_FOUND
            )
        except (AccessDeniedError, CodedError) as e:
            if isinstance(e, LookupUnavailableError):
                raise
            # A shared reader gets the same answer as a stranger (AC4.2.4)
            raise PersonaMemoryManagerError(
                "persona is not manageable", code=ErrorCode.PERSONA_NOT_FOUND
            ) from e

    def add_knowledge(
        self,
        principal: Principal,
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
            raise PersonaMemoryManagerError(
                "topic name is blank", code=ErrorCode.MEMORY_TOPIC_NAME_REQUIRED
            )

        if not topic_content or not topic_content.strip():
            raise PersonaMemoryManagerError(
                "topic content is blank", code=ErrorCode.MEMORY_CONTENT_REQUIRED
            )

        if len(topic_name) > 100:
            raise PersonaMemoryManagerError(
                f"topic name length {len(topic_name)} exceeds 100",
                code=ErrorCode.MEMORY_TOPIC_NAME_TOO_LONG,
                context={"max_length": 100},
            )

        if len(topic_content) > MAX_KNOWLEDGE_CONTENT_CHARS:
            raise PersonaMemoryManagerError(
                f"topic content length {len(topic_content)} exceeds "
                f"{MAX_KNOWLEDGE_CONTENT_CHARS}",
                code=ErrorCode.MEMORY_CONTENT_TOO_LONG,
                context={"max_length": MAX_KNOWLEDGE_CONTENT_CHARS},
            )

        self._ensure_manageable(principal, persona_id)

        memory_service = self._get_memory_service()

        if not memory_service.is_semantic_enabled:
            raise PersonaMemoryManagerError(
                "SEMANTIC_MEMORY_STRATEGY_ID is not set",
                code=ErrorCode.MEMORY_STRATEGY_NOT_CONFIGURED,
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

            # 題名は記憶の中身の一部なのでログに出さない（ID だけ、NFR3.11）
            self.logger.info(
                f"Knowledge added for persona {persona_id} (memory_id: {memory_id})"
            )
            return memory_id

        except MemoryServiceError as e:
            self.logger.error("Failed to add knowledge", exc_info=True)
            raise PersonaMemoryManagerError(
                f"memory add failed ({type(e).__name__})",
                code=ErrorCode.MEMORY_OPERATION_FAILED,
            ) from e
        except Exception as e:
            self.logger.error("Failed to add knowledge", exc_info=True)
            raise PersonaMemoryManagerError(
                f"memory add failed ({type(e).__name__})",
                code=ErrorCode.MEMORY_OPERATION_FAILED,
            ) from e

    def get_memories(
        self,
        principal: Principal,
        persona_id: str,
        strategy_type: str = "summary",
        page: int = 1,
        per_page: int = 10,
    ) -> tuple:
        """
        ペルソナの記憶を取得する。

        Args:
            principal: 利用者（ペルソナを管理できる人だけ、BR4.1）
            persona_id: ペルソナID
            strategy_type: 戦略タイプ（"summary" または "semantic"）
            page: ページ番号
            per_page: 1ページあたりの件数

        Returns:
            (memories, current_page, total_pages) のタプル

        Raises:
            PersonaMemoryManagerError: ペルソナ不在（管理できない場合も）、取得失敗時
        """
        self._ensure_manageable(principal, persona_id)

        memory_service = self._resolve_memory_service()
        if not memory_service:
            return ([], 1, 1)

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
            self.logger.error("Failed to retrieve memories", exc_info=True)
            raise PersonaMemoryManagerError(
                f"memory fetch failed ({type(e).__name__})",
                code=ErrorCode.MEMORY_OPERATION_FAILED,
            ) from e
        except Exception as e:
            self.logger.error("Failed to retrieve memories", exc_info=True)
            raise PersonaMemoryManagerError(
                f"memory fetch failed ({type(e).__name__})",
                code=ErrorCode.MEMORY_OPERATION_FAILED,
            ) from e

    def delete_memory(
        self, principal: Principal, persona_id: str, memory_id: str
    ) -> bool:
        """
        ペルソナの特定の記憶を削除する。

        Args:
            principal: 利用者（ペルソナを管理できる人だけ、BR4.1）
            persona_id: ペルソナID
            memory_id: 記憶ID

        Returns:
            削除成功ならTrue。記憶がない、または別のペルソナの記憶ならFalse
            （どちらも同じ扱い、R-01）

        Raises:
            PersonaMemoryManagerError: 管理できない（PERSONA_NOT_FOUND）、機能無効、削除失敗時
        """
        self._ensure_manageable(principal, persona_id)
        memory_service = self._get_memory_service()

        try:
            # R-01: the record must belong to this persona, not just the persona
            # to the user (a record ID of another persona is never deleted)
            success = memory_service.delete_owned_memory(
                actor_id=persona_id, memory_id=memory_id
            )
            if success:
                self.logger.info(f"Memory {memory_id} deleted for persona {persona_id}")
            return success
        except (ConnectionError, TimeoutError) as e:
            raise PersonaMemoryManagerError(
                "memory service connection failed",
                code=ErrorCode.MEMORY_SERVICE_UNAVAILABLE,
            ) from e
        except MemoryServiceError as e:
            self.logger.error("Failed to delete memory", exc_info=True)
            raise PersonaMemoryManagerError(
                f"memory delete failed ({type(e).__name__})",
                code=ErrorCode.MEMORY_OPERATION_FAILED,
            ) from e
        except Exception as e:
            self.logger.error("Failed to delete memory", exc_info=True)
            raise PersonaMemoryManagerError(
                f"memory delete failed ({type(e).__name__})",
                code=ErrorCode.MEMORY_OPERATION_FAILED,
            ) from e

    def delete_all_memories(
        self, principal: Principal, persona_id: str, strategy_type: str = "summary"
    ) -> int:
        """
        ペルソナの指定戦略タイプの全記憶を削除する。

        Args:
            principal: 利用者（ペルソナを管理できる人だけ、BR4.1）
            persona_id: ペルソナID
            strategy_type: 戦略タイプ（"summary" または "semantic"）

        Returns:
            削除件数

        Raises:
            PersonaMemoryManagerError: 管理できない（PERSONA_NOT_FOUND）、機能無効、削除失敗時
        """
        self._ensure_manageable(principal, persona_id)
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
                "memory service connection failed",
                code=ErrorCode.MEMORY_SERVICE_UNAVAILABLE,
            ) from e
        except MemoryServiceError as e:
            self.logger.error("Failed to delete all memories", exc_info=True)
            raise PersonaMemoryManagerError(
                f"memory bulk delete failed ({type(e).__name__})",
                code=ErrorCode.MEMORY_OPERATION_FAILED,
            ) from e
        except Exception as e:
            self.logger.error("Failed to delete all memories", exc_info=True)
            raise PersonaMemoryManagerError(
                f"memory bulk delete failed ({type(e).__name__})",
                code=ErrorCode.MEMORY_OPERATION_FAILED,
            ) from e

    def safe_get_memories(
        self, principal: Principal, persona_id: str, strategy_type: str = "summary"
    ) -> list:
        """
        エラー時に安全に記憶リストを取得するヘルパー。
        取得失敗時（管理できない場合を含む）は空リストを返す（例外を投げない）。
        """
        try:
            self._ensure_manageable(principal, persona_id)
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
            raise PersonaMemoryManagerError(
                "long-term memory feature is disabled",
                code=ErrorCode.MEMORY_FEATURE_DISABLED,
            )

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
            raise PersonaMemoryManagerError(
                "persona id is blank", code=ErrorCode.PERSONA_ID_INVALID
            )

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
