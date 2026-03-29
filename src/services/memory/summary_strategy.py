"""
Summary Memory Strategy Implementation
Summary戦略の実装

AgentCore MemoryのSummary戦略を使用して、議論の要約を長期記憶として保存・検索する。
MemoryClientを使用してAgentCore Memoryと連携する。

Requirements:
    - 2.2: SummaryStrategy実装
    - 3.1: AgentCore Memory接続
    - 3.2: 記憶の保存（actor_id, session_id関連付け）
    - 3.3: セマンティック検索
    - 3.4: 記憶の削除
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from src.models.memory import MemoryEntry

from .strategy import MemoryStrategy

logger = logging.getLogger(__name__)


class MemoryOperationError(Exception):
    """メモリ操作エラー"""

    pass


class SummaryStrategy(MemoryStrategy):
    """
    Summary記憶戦略の実装

    AgentCore MemoryのSummary戦略を使用して、議論の要約を長期記憶として保存する。
    namespace pattern: /strategies/{memoryStrategyId}/actors/{actorId}/sessions/{sessionId}

    Attributes:
        memory_id: AgentCore MemoryリソースID
        region: AWSリージョン
        memory_strategy_id: namespaceで使用するmemoryStrategyId
    """

    # Namespace pattern for summary strategy (must start with /)
    NAMESPACE_PATTERN = (
        "/strategies/{memoryStrategyId}/actors/{actorId}/sessions/{sessionId}"
    )

    def __init__(
        self,
        memory_id: str,
        region: str = "us-east-1",
        memory_strategy_id: Optional[str] = None,
    ):
        """
        SummaryStrategyを初期化

        Args:
            memory_id: AgentCore MemoryリソースID
            region: AWSリージョン（デフォルト: us-east-1）
            memory_strategy_id: namespaceで使用するmemoryStrategyId（デフォルト: configから取得）
        """
        from src.config import Config

        self.memory_id = memory_id
        self.region = region

        # memory_strategy_idが指定されていない場合はconfigから取得
        if memory_strategy_id is None:
            config = Config()
            self.memory_strategy_id = config.SUMMARY_MEMORY_STRATEGY_ID
        else:
            self.memory_strategy_id = memory_strategy_id

        self._client: Any = None
        self._init_client()

    def _init_client(self) -> None:
        """AgentCore Memory Clientを初期化"""
        try:
            from bedrock_agentcore.memory import MemoryClient

            self._client = MemoryClient(region_name=self.region)
            logger.info(
                "Initialized SummaryStrategy with memory_id=%s, region=%s",
                self.memory_id,
                self.region,
            )
        except ImportError as e:
            logger.error("Failed to import bedrock_agentcore: %s", e)
            raise MemoryOperationError(
                "bedrock_agentcore package is not installed. "
                "Please install it with: pip install bedrock-agentcore"
            ) from e
        except Exception as e:
            logger.error("Failed to initialize MemoryClient: %s", e)
            raise MemoryOperationError(f"Failed to initialize MemoryClient: {e}") from e

    def _build_namespace(self, actor_id: str, session_id: str) -> str:
        """
        namespaceを構築

        Args:
            actor_id: ペルソナID
            session_id: 議論ID

        Returns:
            構築されたnamespace文字列
        """
        return self.NAMESPACE_PATTERN.format(
            memoryStrategyId=self.memory_strategy_id,
            actorId=actor_id,
            sessionId=session_id,
        )

    def _build_actor_namespace_prefix(self, actor_id: str) -> str:
        """
        actor_idに基づくnamespaceプレフィックスを構築

        Args:
            actor_id: ペルソナID

        Returns:
            namespaceプレフィックス
        """
        return f"/strategies/{self.memory_strategy_id}/actors/{actor_id}"

    def save(
        self,
        actor_id: str,
        session_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        議論の要約を長期記憶として保存

        AgentCore Memoryのcreate_eventを使用してイベントを作成し、
        Summary戦略によって自動的に長期記憶が抽出される。

        Args:
            actor_id: ペルソナID（AgentCore MemoryのactorIdとして使用）
            session_id: 議論ID（AgentCore MemoryのsessionIdとして使用）
            content: 保存する記憶の内容
            metadata: 追加のメタデータ（重要度、タイプ等）

        Returns:
            保存されたイベントのID

        Raises:
            MemoryOperationError: 保存に失敗した場合
        """
        try:
            # Create event with the content as a conversation turn
            # This triggers LTM extraction via the summary strategy
            event = self._client.create_event(
                memory_id=self.memory_id,
                actor_id=actor_id,
                session_id=session_id,
                messages=[
                    (
                        content,
                        "ASSISTANT",
                    )  # Store as assistant message for summary extraction
                ],
            )

            event_id = event.get("eventId", str(uuid.uuid4()))
            logger.info(
                "Saved memory event for actor=%s, session=%s, event_id=%s",
                actor_id,
                session_id,
                event_id,
            )

            return str(event_id)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            logger.error(
                "Failed to save memory: actor=%s, session=%s, error=%s: %s",
                actor_id,
                session_id,
                error_code,
                error_msg,
            )
            raise MemoryOperationError(f"Failed to save memory: {error_msg}") from e
        except Exception as e:
            logger.error("Unexpected error saving memory: %s", e)
            raise MemoryOperationError(f"Failed to save memory: {e}") from e

    def retrieve(self, actor_id: str, query: str, top_k: int = 5) -> List[MemoryEntry]:
        """
        関連する記憶をセマンティック検索で取得

        Args:
            actor_id: ペルソナID
            query: 検索クエリ（トピックやキーワード）
            top_k: 取得する最大件数（デフォルト: 5）

        Returns:
            関連する記憶エントリのリスト（関連度スコア降順）

        Raises:
            MemoryOperationError: 検索に失敗した場合
        """
        try:
            namespace_prefix = self._build_actor_namespace_prefix(actor_id)

            # Use retrieve_memories for semantic search
            memories = self._client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=namespace_prefix,
                query=query,
                top_k=top_k,
            )

            # Convert API response to MemoryEntry objects
            entries = []
            for memory in memories:
                entry = self._convert_to_memory_entry(memory, actor_id)
                if entry:
                    entries.append(entry)

            # Sort by relevance score (descending)
            entries.sort(key=lambda x: x.relevance_score or 0, reverse=True)

            logger.info(
                "Retrieved %d memories for actor=%s, query='%s'",
                len(entries),
                actor_id,
                query[:50],
            )

            return entries

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            # Handle specific error cases gracefully
            if error_code == "ResourceNotFoundException":
                logger.warning(
                    "Memory or namespace not found for actor=%s: %s",
                    actor_id,
                    error_msg,
                )
                return []

            logger.error(
                "Failed to retrieve memories: actor=%s, error=%s: %s",
                actor_id,
                error_code,
                error_msg,
            )
            raise MemoryOperationError(
                f"Failed to retrieve memories: {error_msg}"
            ) from e
        except Exception as e:
            logger.error("Unexpected error retrieving memories: %s", e)
            raise MemoryOperationError(f"Failed to retrieve memories: {e}") from e

    def delete(self, actor_id: str, memory_id: str) -> bool:
        """
        特定の記憶を削除

        Args:
            actor_id: ペルソナID
            memory_id: 削除する記憶のID（memoryRecordId）

        Returns:
            削除に成功した場合True、記憶が見つからない場合False

        Raises:
            MemoryOperationError: 削除に失敗した場合
        """
        try:
            # Use delete_memory_record to delete a specific memory record
            self._client.delete_memory_record(
                memoryId=self.memory_id, memoryRecordId=memory_id
            )

            logger.info(
                "Deleted memory record: actor=%s, memory_id=%s", actor_id, memory_id
            )

            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            if error_code == "ResourceNotFoundException":
                logger.warning(
                    "Memory record not found: actor=%s, memory_id=%s",
                    actor_id,
                    memory_id,
                )
                return False

            logger.error(
                "Failed to delete memory: actor=%s, memory_id=%s, error=%s: %s",
                actor_id,
                memory_id,
                error_code,
                error_msg,
            )
            raise MemoryOperationError(f"Failed to delete memory: {error_msg}") from e
        except Exception as e:
            logger.error("Unexpected error deleting memory: %s", e)
            raise MemoryOperationError(f"Failed to delete memory: {e}") from e

    def list_all(self, actor_id: str) -> List[MemoryEntry]:
        """
        ペルソナの全記憶を取得

        Args:
            actor_id: ペルソナID

        Returns:
            ペルソナに関連する全ての記憶エントリのリスト

        Raises:
            MemoryOperationError: 取得に失敗した場合
        """
        try:
            namespace_prefix = self._build_actor_namespace_prefix(actor_id)

            # Use list_memory_records to get all records without semantic search
            records = self._client.list_memory_records(
                memoryId=self.memory_id, namespace=namespace_prefix
            )

            # Handle pagination if needed
            all_records = records.get("memoryRecordSummaries", [])

            # Convert API response to MemoryEntry objects
            entries = []
            for record in all_records:
                entry = self._convert_to_memory_entry(record, actor_id)
                if entry:
                    entries.append(entry)

            logger.info("Listed %d memories for actor=%s", len(entries), actor_id)

            return entries

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            if error_code == "ResourceNotFoundException":
                logger.warning(
                    "Memory or namespace not found for actor=%s: %s",
                    actor_id,
                    error_msg,
                )
                return []

            logger.error(
                "Failed to list memories: actor=%s, error=%s: %s",
                actor_id,
                error_code,
                error_msg,
            )
            raise MemoryOperationError(f"Failed to list memories: {error_msg}") from e
        except Exception as e:
            logger.error("Unexpected error listing memories: %s", e)
            raise MemoryOperationError(f"Failed to list memories: {e}") from e

    def _convert_to_memory_entry(
        self, record: Dict[str, Any], actor_id: str
    ) -> Optional[MemoryEntry]:
        """
        API応答をMemoryEntryに変換

        Args:
            record: AgentCore Memory APIからの応答レコード
            actor_id: ペルソナID

        Returns:
            MemoryEntryオブジェクト、変換に失敗した場合はNone
        """
        try:
            # Extract memory record ID
            memory_record_id = record.get("memoryRecordId", str(uuid.uuid4()))

            # Extract content from the record
            content_data = record.get("content", {})
            if isinstance(content_data, dict):
                content = content_data.get("text", "")
            else:
                content = str(content_data) if content_data else ""

            # Extract session_id from namespace if available
            namespace = record.get("namespace", "")
            session_id = self._extract_session_id_from_namespace(namespace)

            # Extract timestamp
            created_at_str = record.get("createdAt") or record.get("eventTimestamp")
            if created_at_str:
                if isinstance(created_at_str, datetime):
                    created_at = created_at_str
                else:
                    created_at = datetime.fromisoformat(
                        str(created_at_str).replace("Z", "+00:00")
                    )
            else:
                created_at = datetime.now()

            # Extract relevance score if available
            relevance_score = record.get("relevanceScore")

            # Build metadata
            metadata = {
                "namespace": namespace,
                "strategy_id": record.get("memoryStrategyId"),
                "strategy_type": "summary",  # 戦略タイプを識別
            }

            return MemoryEntry(
                id=memory_record_id,
                actor_id=actor_id,
                session_id=session_id,
                content=content,
                metadata=metadata,
                created_at=created_at,
                relevance_score=relevance_score,
            )

        except Exception as e:
            logger.warning("Failed to convert record to MemoryEntry: %s", e)
            return None

    def _extract_session_id_from_namespace(self, namespace: str) -> str:
        """
        namespaceからsession_idを抽出

        Args:
            namespace: namespace文字列（例: "/strategies/summary/actors/actor123/sessions/session456"）

        Returns:
            抽出されたsession_id、抽出できない場合は空文字列
        """
        if not namespace:
            return ""

        # 新しいパターン: /strategies/{memoryStrategyId}/actors/{actorId}/sessions/{sessionId}
        parts = namespace.strip("/").split("/")
        # parts = ["strategies", "{memoryStrategyId}", "actors", "{actorId}", "sessions", "{sessionId}"]
        if (
            len(parts) >= 6
            and parts[0] == "strategies"
            and parts[2] == "actors"
            and parts[4] == "sessions"
        ):
            return parts[5]

        # 旧パターン（後方互換性）: summaries/{actorId}/{sessionId}
        if len(parts) >= 3 and parts[0] == "summaries":
            return parts[2]

        return ""
