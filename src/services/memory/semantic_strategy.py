"""
Semantic Memory Strategy Implementation
Semantic戦略の実装

AgentCore MemoryのSemantic戦略を使用して、事実情報を長期記憶として保存・検索する。
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from src.models.memory import MemoryEntry

from .strategy import MemoryStrategy
from .summary_strategy import MemoryOperationError

logger = logging.getLogger(__name__)


class SemanticStrategy(MemoryStrategy):
    """
    Semantic記憶戦略の実装

    AgentCore MemoryのSemantic戦略を使用して、事実情報を長期記憶として保存する。
    namespace pattern: /strategies/{memoryStrategyId}/actors/{actorId}

    Attributes:
        memory_id: AgentCore MemoryリソースID
        region: AWSリージョン
        memory_strategy_id: namespaceで使用するmemoryStrategyId
    """

    # Namespace pattern for semantic strategy
    NAMESPACE_PATTERN = "/strategies/{memoryStrategyId}/actors/{actorId}"

    def __init__(
        self,
        memory_id: str,
        region: str = "us-east-1",
        memory_strategy_id: Optional[str] = None,
    ):
        """
        SemanticStrategyを初期化

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
            self.memory_strategy_id = config.SEMANTIC_MEMORY_STRATEGY_ID
        else:
            self.memory_strategy_id = memory_strategy_id

        self._client = None
        if self.memory_strategy_id:
            self._init_client()

    def _init_client(self) -> None:
        """AgentCore Memory Clientを初期化"""
        try:
            from bedrock_agentcore.memory import MemoryClient

            self._client = MemoryClient(region_name=self.region)
            logger.info(
                "Initialized SemanticStrategy with memory_id=%s, region=%s, strategy_id=%s",
                self.memory_id,
                self.region,
                self.memory_strategy_id,
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

    def _build_namespace(self, actor_id: str, session_id: str = "") -> str:
        """
        namespaceを構築

        Args:
            actor_id: ペルソナID
            session_id: 議論ID（Semantic戦略では使用しない）

        Returns:
            構築されたnamespace文字列
        """
        return self.NAMESPACE_PATTERN.format(
            memoryStrategyId=self.memory_strategy_id, actorId=actor_id
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
        事実情報を長期記憶として保存

        Note: Semantic戦略は通常、会話から自動的に事実を抽出するため、
        直接保存することは少ない。

        Args:
            actor_id: ペルソナID
            session_id: 議論ID
            content: 保存する記憶の内容
            metadata: 追加のメタデータ

        Returns:
            保存されたイベントのID
        """
        if not self._client:
            raise MemoryOperationError(
                "SemanticStrategy not initialized (no strategy_id)"
            )

        try:
            event = self._client.create_event(
                memory_id=self.memory_id,
                actor_id=actor_id,
                session_id=session_id,
                messages=[(content, "ASSISTANT")],
            )

            event_id = event.get("eventId", str(uuid.uuid4()))
            logger.info(
                "Saved semantic memory event for actor=%s, event_id=%s",
                actor_id,
                event_id,
            )

            return event_id

        except Exception as e:
            logger.error("Failed to save semantic memory: %s", e)
            raise MemoryOperationError(f"Failed to save memory: {e}") from e

    def save_directly_to_ltm(
        self, actor_id: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        直接長期記憶（LTM）に保存する（短期記憶を経由しない）

        BatchCreateMemoryRecords APIを使用して、Semantic戦略のnamespaceに
        直接記憶レコードを作成する。

        Args:
            actor_id: ペルソナID
            content: 保存する記憶の内容
            metadata: 追加のメタデータ

        Returns:
            保存された記憶レコードのID
        """
        import boto3

        if not self._client:
            raise MemoryOperationError(
                "SemanticStrategy not initialized (no strategy_id)"
            )

        try:
            # boto3クライアントを直接使用（batch_create_memory_recordsはMemoryClientにない）
            gmdp_client = boto3.client("bedrock-agentcore", region_name=self.region)

            # Semantic戦略のnamespaceを構築
            namespace = self._build_namespace(actor_id)

            # 一意のリクエスト識別子を生成
            request_id = f"manual-{uuid.uuid4().hex[:16]}"

            # タイムスタンプ
            timestamp = datetime.now()

            # BatchCreateMemoryRecords APIを呼び出し
            response = gmdp_client.batch_create_memory_records(
                memoryId=self.memory_id,
                records=[
                    {
                        "content": {"text": content},
                        "namespaces": [namespace],
                        "requestIdentifier": request_id,
                        "timestamp": timestamp,
                        "memoryStrategyId": self.memory_strategy_id,
                    }
                ],
            )

            # 成功したレコードを確認
            successful_records = response.get("successfulRecords", [])
            failed_records = response.get("failedRecords", [])

            if failed_records:
                error_msg = failed_records[0].get("errorMessage", "Unknown error")
                logger.error(f"Failed to create memory record: {error_msg}")
                raise MemoryOperationError(
                    f"Failed to create memory record: {error_msg}"
                )

            if successful_records:
                memory_record_id = successful_records[0].get(
                    "memoryRecordId", request_id
                )
                logger.info(
                    "Directly saved to LTM for actor=%s, memory_record_id=%s, namespace=%s",
                    actor_id,
                    memory_record_id,
                    namespace,
                )
                return memory_record_id

            # 成功も失敗もない場合（通常は発生しない）
            logger.warning("No records returned from batch_create_memory_records")
            return request_id

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            logger.error(
                f"AWS API error saving directly to LTM: {error_code} - {error_msg}"
            )
            raise MemoryOperationError(f"Failed to save memory: {error_msg}") from e
        except Exception as e:
            logger.error(f"Failed to save directly to LTM: {e}")
            raise MemoryOperationError(f"Failed to save memory: {e}") from e

    def retrieve(self, actor_id: str, query: str, top_k: int = 5) -> List[MemoryEntry]:
        """
        関連する事実情報をセマンティック検索で取得

        Args:
            actor_id: ペルソナID
            query: 検索クエリ
            top_k: 取得する最大件数

        Returns:
            関連する記憶エントリのリスト
        """
        if not self._client:
            return []

        try:
            namespace_prefix = self._build_actor_namespace_prefix(actor_id)

            memories = self._client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=namespace_prefix,
                query=query,
                top_k=top_k,
            )

            entries = []
            for memory in memories:
                entry = self._convert_to_memory_entry(memory, actor_id)
                if entry:
                    entries.append(entry)

            entries.sort(key=lambda x: x.relevance_score or 0, reverse=True)

            logger.info(
                "Retrieved %d semantic memories for actor=%s", len(entries), actor_id
            )

            return entries

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "ResourceNotFoundException":
                logger.warning("Semantic namespace not found for actor=%s", actor_id)
                return []
            raise MemoryOperationError(f"Failed to retrieve memories: {e}") from e
        except Exception as e:
            logger.error("Unexpected error retrieving semantic memories: %s", e)
            raise MemoryOperationError(f"Failed to retrieve memories: {e}") from e

    def delete(self, actor_id: str, memory_id: str) -> bool:
        """
        特定の記憶を削除

        Args:
            actor_id: ペルソナID
            memory_id: 削除する記憶のID

        Returns:
            削除に成功した場合True
        """
        if not self._client:
            return False

        try:
            self._client.delete_memory_record(
                memoryId=self.memory_id, memoryRecordId=memory_id
            )

            logger.info(
                "Deleted semantic memory: actor=%s, memory_id=%s", actor_id, memory_id
            )
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "ResourceNotFoundException":
                return False
            raise MemoryOperationError(f"Failed to delete memory: {e}") from e
        except Exception as e:
            logger.error("Unexpected error deleting semantic memory: %s", e)
            raise MemoryOperationError(f"Failed to delete memory: {e}") from e

    def list_all(self, actor_id: str) -> List[MemoryEntry]:
        """
        ペルソナの全Semantic記憶を取得

        Args:
            actor_id: ペルソナID

        Returns:
            ペルソナに関連する全てのSemantic記憶エントリのリスト
        """
        if not self._client:
            return []

        try:
            namespace_prefix = self._build_actor_namespace_prefix(actor_id)

            records = self._client.list_memory_records(
                memoryId=self.memory_id, namespace=namespace_prefix
            )

            all_records = records.get("memoryRecordSummaries", [])

            entries = []
            for record in all_records:
                entry = self._convert_to_memory_entry(record, actor_id)
                if entry:
                    entries.append(entry)

            logger.info(
                "Listed %d semantic memories for actor=%s", len(entries), actor_id
            )

            return entries

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "ResourceNotFoundException":
                logger.warning("Semantic namespace not found for actor=%s", actor_id)
                return []
            raise MemoryOperationError(f"Failed to list memories: {e}") from e
        except Exception as e:
            logger.error("Unexpected error listing semantic memories: %s", e)
            raise MemoryOperationError(f"Failed to list memories: {e}") from e

    def _convert_to_memory_entry(
        self, record: Dict[str, Any], actor_id: str
    ) -> Optional[MemoryEntry]:
        """
        API応答をMemoryEntryに変換
        """
        try:
            memory_record_id = record.get("memoryRecordId", str(uuid.uuid4()))

            content_data = record.get("content", {})
            if isinstance(content_data, dict):
                content = content_data.get("text", "")
            else:
                content = str(content_data) if content_data else ""

            namespace = record.get("namespace", "")

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

            relevance_score = record.get("relevanceScore")

            metadata = {
                "namespace": namespace,
                "strategy_id": self.memory_strategy_id,
                "strategy_type": "semantic",  # 戦略タイプを識別
            }

            return MemoryEntry(
                id=memory_record_id,
                actor_id=actor_id,
                session_id="",  # Semantic戦略はsession_idを持たない
                content=content,
                metadata=metadata,
                created_at=created_at,
                relevance_score=relevance_score,
            )

        except Exception as e:
            logger.warning("Failed to convert record to MemoryEntry: %s", e)
            return None
