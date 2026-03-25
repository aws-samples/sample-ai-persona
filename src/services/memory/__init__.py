# Memory services package
"""
長期記憶機能のサービスパッケージ
AgentCore Memoryを使用したペルソナの長期記憶管理

推奨方式: AgentCoreMemorySessionManagerを使用
- STM（短期記憶）: 会話履歴の自動永続化
- LTM（長期記憶）: Summary/UserPreference/Semantic戦略による自動抽出
"""

from .session_manager_factory import (
    SessionManagerFactoryError,
    create_agentcore_session_manager,
    is_memory_enabled,
)

# 後方互換性のため旧モジュールもエクスポート（非推奨）
from .memory_service import (
    MemoryConnectionError,
    MemoryService,
    MemoryServiceError,
)
from .retry import (
    RetryContext,
    RetryExhaustedError,
    calculate_backoff_delay,
    is_transient_error,
    with_retry,
)
from .strategy import MemoryStrategy
from .summary_strategy import MemoryOperationError, SummaryStrategy
from .semantic_strategy import SemanticStrategy

__all__ = [
    # 推奨API
    "create_agentcore_session_manager",
    "is_memory_enabled",
    "SessionManagerFactoryError",
    # 後方互換性（非推奨）
    "MemoryStrategy",
    "SummaryStrategy",
    "SemanticStrategy",
    "MemoryOperationError",
    "MemoryService",
    "MemoryServiceError",
    "MemoryConnectionError",
    "with_retry",
    "RetryExhaustedError",
    "RetryContext",
    "is_transient_error",
    "calculate_backoff_delay",
]
