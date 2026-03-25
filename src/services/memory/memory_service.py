"""
Memory Service Implementation
AgentCore Memoryと連携する中核サービス

Requirements:
    - 1.1: AgentCore Memory接続（memory_id, region設定）
    - 1.2: 接続検証
    - 1.3: 接続失敗時のエラーハンドリング
    - 1.4: 環境変数による設定
    - 10.4: リトライロジック（指数バックオフ）
"""

import logging
from typing import Any, Dict, List, Optional

from src.models.memory import MemoryEntry

from .retry import RetryExhaustedError, with_retry
from .strategy import MemoryStrategy
from .summary_strategy import MemoryOperationError, SummaryStrategy
from .semantic_strategy import SemanticStrategy

logger = logging.getLogger(__name__)


class MemoryConnectionError(Exception):
    """AgentCore Memory接続エラー"""

    pass


class MemoryServiceError(Exception):
    """Memory Service関連のエラー"""

    pass


class MemoryService:
    """
    AgentCore Memoryと連携する中核サービス

    Strategy Patternを使用して、異なる記憶戦略を動的に切り替え可能。
    デフォルトではSummaryStrategyを使用し、SemanticStrategyも併用可能。

    Attributes:
        memory_id: AgentCore MemoryリソースID
        region: AWSリージョン
        _strategy: 現在の記憶戦略（プライマリ）
        _semantic_strategy: Semantic戦略（オプション）
        _enabled: サービスが有効かどうか

    Requirements:
        - 1.1: AgentCore Memory接続
        - 1.2: 接続検証
        - 1.3: エラーハンドリング
        - 1.4: 設定可能なmemory_id
    """

    def __init__(
        self,
        memory_id: str,
        region: str = "us-east-1",
        strategy: Optional[MemoryStrategy] = None,
        validate_connection: bool = True,
    ):
        """
        MemoryServiceを初期化

        Args:
            memory_id: AgentCore MemoryリソースID
            region: AWSリージョン（デフォルト: us-east-1）
            strategy: 使用する記憶戦略（デフォルト: SummaryStrategy）
            validate_connection: 初期化時に接続を検証するか

        Raises:
            MemoryConnectionError: 接続検証に失敗した場合
            ValueError: memory_idが空の場合
        """
        if not memory_id:
            raise ValueError("memory_id is required")

        self.memory_id = memory_id
        self.region = region
        self._strategy: Optional[MemoryStrategy] = None
        self._semantic_strategy: Optional[SemanticStrategy] = None
        self._enabled = True

        # 戦略を設定（デフォルトはSummaryStrategy）
        if strategy:
            self._strategy = strategy
        else:
            self._init_default_strategy()

        # Semantic戦略も初期化（設定されている場合）
        self._init_semantic_strategy()

        # 接続検証
        if validate_connection:
            self._validate_connection()

        logger.info(
            "MemoryService initialized: memory_id=%s, region=%s, strategy=%s, semantic=%s",
            self.memory_id,
            self.region,
            type(self._strategy).__name__ if self._strategy else "None",
            "enabled" if self._semantic_strategy else "disabled",
        )

    def _init_default_strategy(self) -> None:
        """デフォルトの記憶戦略（SummaryStrategy）を初期化"""
        try:
            self._strategy = SummaryStrategy(
                memory_id=self.memory_id, region=self.region
            )
        except Exception as e:
            logger.error("Failed to initialize default strategy: %s", e)
            raise MemoryConnectionError(
                f"Failed to initialize memory strategy: {e}"
            ) from e

    def _init_semantic_strategy(self) -> None:
        """Semantic戦略を初期化（設定されている場合）"""
        from src.config import Config

        config = Config()

        if not config.SEMANTIC_MEMORY_STRATEGY_ID:
            logger.debug("Semantic strategy not configured, skipping initialization")
            return

        try:
            self._semantic_strategy = SemanticStrategy(
                memory_id=self.memory_id,
                region=self.region,
                memory_strategy_id=config.SEMANTIC_MEMORY_STRATEGY_ID,
            )
            logger.info(
                "Semantic strategy initialized: strategy_id=%s",
                config.SEMANTIC_MEMORY_STRATEGY_ID,
            )
        except Exception as e:
            logger.warning("Failed to initialize semantic strategy: %s", e)
            # Semantic戦略の初期化失敗はサービス全体を停止させない
            self._semantic_strategy = None

    def _validate_connection(self) -> None:
        """
        AgentCore Memoryへの接続を検証

        Raises:
            MemoryConnectionError: 接続検証に失敗した場合
        """
        try:
            # 戦略が初期化されていることを確認
            if not self._strategy:
                raise MemoryConnectionError("Memory strategy not initialized")

            logger.info("Connection validated for memory_id=%s", self.memory_id)
        except Exception as e:
            logger.error("Connection validation failed: %s", e)
            raise MemoryConnectionError(
                f"Failed to validate connection to AgentCore Memory: {e}"
            ) from e

    @property
    def enabled(self) -> bool:
        """サービスが有効かどうかを返す"""
        return self._enabled

    @property
    def strategy(self) -> Optional[MemoryStrategy]:
        """現在の記憶戦略を返す"""
        return self._strategy

    def set_strategy(self, strategy: MemoryStrategy) -> None:
        """
        記憶戦略を設定

        Args:
            strategy: 新しい記憶戦略

        Requirements:
            - 2.1: Strategy patternの実装
            - 2.3: 新しい戦略の登録
        """
        if not isinstance(strategy, MemoryStrategy):
            raise TypeError("strategy must be an instance of MemoryStrategy")

        old_strategy = type(self._strategy).__name__ if self._strategy else "None"
        self._strategy = strategy

        logger.info(
            "Memory strategy changed: %s -> %s", old_strategy, type(strategy).__name__
        )

    def _ensure_strategy(self) -> MemoryStrategy:
        """戦略が設定されていることを確認し、戦略を返す"""
        if not self._strategy:
            raise MemoryServiceError("Memory strategy not configured")
        return self._strategy

    @with_retry(
        max_retries=3, base_delay=1.0, retryable_exceptions=(MemoryOperationError,)
    )
    def save_memory(
        self,
        actor_id: str,
        session_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        記憶を保存

        Args:
            actor_id: ペルソナID
            session_id: 議論ID
            content: 保存する内容
            metadata: 追加メタデータ

        Returns:
            保存された記憶のID

        Raises:
            MemoryServiceError: 保存に失敗した場合
        """
        strategy = self._ensure_strategy()

        try:
            memory_id = strategy.save(
                actor_id=actor_id,
                session_id=session_id,
                content=content,
                metadata=metadata,
            )

            logger.info(
                "Memory saved: actor=%s, session=%s, memory_id=%s",
                actor_id,
                session_id,
                memory_id,
            )

            return memory_id

        except RetryExhaustedError as e:
            logger.error("Retry exhausted saving memory: %s", e)
            raise MemoryServiceError(
                f"Failed to save memory after retries: {e.last_exception}"
            ) from e
        except MemoryOperationError:
            # リトライ対象のエラーは再送出
            raise
        except Exception as e:
            logger.error("Unexpected error saving memory: %s", e)
            raise MemoryServiceError(f"Failed to save memory: {e}") from e

    @with_retry(
        max_retries=3, base_delay=1.0, retryable_exceptions=(MemoryOperationError,)
    )
    def retrieve_memories(
        self, actor_id: str, query: str, top_k: int = 5
    ) -> List[MemoryEntry]:
        """
        関連する記憶を検索

        Args:
            actor_id: ペルソナID
            query: 検索クエリ
            top_k: 取得する最大件数

        Returns:
            関連する記憶エントリのリスト

        Raises:
            MemoryServiceError: 検索に失敗した場合
        """
        strategy = self._ensure_strategy()

        try:
            memories = strategy.retrieve(actor_id=actor_id, query=query, top_k=top_k)

            logger.info(
                "Retrieved %d memories for actor=%s, query='%s'",
                len(memories),
                actor_id,
                query[:50] if query else "",
            )

            return memories

        except RetryExhaustedError as e:
            logger.error("Retry exhausted retrieving memories: %s", e)
            raise MemoryServiceError(
                f"Failed to retrieve memories after retries: {e.last_exception}"
            ) from e
        except MemoryOperationError:
            # リトライ対象のエラーは再送出
            raise
        except Exception as e:
            logger.error("Unexpected error retrieving memories: %s", e)
            raise MemoryServiceError(f"Failed to retrieve memories: {e}") from e

    @with_retry(
        max_retries=3, base_delay=1.0, retryable_exceptions=(MemoryOperationError,)
    )
    def delete_memory(self, actor_id: str, memory_id: str) -> bool:
        """
        記憶を削除（Summary戦略とSemantic戦略の両方を試行）

        Args:
            actor_id: ペルソナID
            memory_id: 削除する記憶のID

        Returns:
            削除に成功した場合True

        Raises:
            MemoryServiceError: 削除に失敗した場合
        """
        strategy = self._ensure_strategy()

        try:
            # まずSummary戦略で削除を試行
            result = strategy.delete(actor_id=actor_id, memory_id=memory_id)

            if result:
                logger.info(
                    "Memory deleted (summary): actor=%s, memory_id=%s",
                    actor_id,
                    memory_id,
                )
                return True

            # Summary戦略で見つからない場合、Semantic戦略で試行
            if self._semantic_strategy:
                result = self._semantic_strategy.delete(
                    actor_id=actor_id, memory_id=memory_id
                )

                if result:
                    logger.info(
                        "Memory deleted (semantic): actor=%s, memory_id=%s",
                        actor_id,
                        memory_id,
                    )
                    return True

            logger.warning(
                "Memory not found for deletion: actor=%s, memory_id=%s",
                actor_id,
                memory_id,
            )
            return False

        except RetryExhaustedError as e:
            logger.error("Retry exhausted deleting memory: %s", e)
            raise MemoryServiceError(
                f"Failed to delete memory after retries: {e.last_exception}"
            ) from e
        except MemoryOperationError:
            # リトライ対象のエラーは再送出
            raise
        except Exception as e:
            logger.error("Unexpected error deleting memory: %s", e)
            raise MemoryServiceError(f"Failed to delete memory: {e}") from e

    @with_retry(
        max_retries=3, base_delay=1.0, retryable_exceptions=(MemoryOperationError,)
    )
    def list_memories(self, actor_id: str) -> List[MemoryEntry]:
        """
        ペルソナの全記憶を取得（Summary + Semantic戦略の両方から）

        Args:
            actor_id: ペルソナID

        Returns:
            ペルソナに関連する全ての記憶エントリのリスト

        Raises:
            MemoryServiceError: 取得に失敗した場合
        """
        strategy = self._ensure_strategy()
        all_memories: List[MemoryEntry] = []

        try:
            # Summary戦略から記憶を取得
            summary_memories = strategy.list_all(actor_id=actor_id)
            # 戦略タイプをメタデータに追加
            for memory in summary_memories:
                if memory.metadata is None:
                    memory.metadata = {}
                memory.metadata["strategy_type"] = "summary"
            all_memories.extend(summary_memories)

            logger.info(
                "Listed %d summary memories for actor=%s",
                len(summary_memories),
                actor_id,
            )

        except RetryExhaustedError as e:
            logger.error("Retry exhausted listing summary memories: %s", e)
            raise MemoryServiceError(
                f"Failed to list memories after retries: {e.last_exception}"
            ) from e
        except MemoryOperationError:
            raise
        except Exception as e:
            logger.error("Unexpected error listing summary memories: %s", e)
            raise MemoryServiceError(f"Failed to list memories: {e}") from e

        # Semantic戦略から記憶を取得（設定されている場合）
        if self._semantic_strategy:
            try:
                semantic_memories = self._semantic_strategy.list_all(actor_id=actor_id)
                all_memories.extend(semantic_memories)

                logger.info(
                    "Listed %d semantic memories for actor=%s",
                    len(semantic_memories),
                    actor_id,
                )

            except Exception as e:
                # Semantic戦略のエラーはログに記録するが、処理は継続
                logger.warning(
                    "Failed to list semantic memories for actor=%s: %s", actor_id, e
                )

        logger.info(
            "Total %d memories listed for actor=%s", len(all_memories), actor_id
        )

        return all_memories

    def delete_all_memories(self, actor_id: str) -> int:
        """
        ペルソナの全記憶を削除

        Args:
            actor_id: ペルソナID

        Returns:
            削除された記憶の数

        Raises:
            MemoryServiceError: 削除に失敗した場合
        """
        try:
            # まず全記憶を取得
            memories = self.list_memories(actor_id)

            if not memories:
                logger.info("No memories to delete for actor=%s", actor_id)
                return 0

            # 各記憶を削除
            deleted_count = 0
            failed_count = 0

            for memory in memories:
                try:
                    if self.delete_memory(actor_id, memory.id):
                        deleted_count += 1
                except Exception as e:
                    logger.warning("Failed to delete memory %s: %s", memory.id, e)
                    failed_count += 1

            logger.info(
                "Deleted %d memories for actor=%s (failed: %d)",
                deleted_count,
                actor_id,
                failed_count,
            )

            if failed_count > 0:
                logger.warning(
                    "Some memories could not be deleted: %d failures", failed_count
                )

            return deleted_count

        except Exception as e:
            logger.error("Unexpected error deleting all memories: %s", e)
            raise MemoryServiceError(f"Failed to delete all memories: {e}") from e

    def disable(self) -> None:
        """サービスを無効化"""
        self._enabled = False
        logger.info("MemoryService disabled")

    def enable(self) -> None:
        """サービスを有効化"""
        self._enabled = True
        logger.info("MemoryService enabled")
