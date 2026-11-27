# Memory services package
"""
Service package for the long-term memory feature
Long-term memory management for personas using AgentCore Memory

Recommended approach: use AgentCoreMemorySessionManager
- STM (short-term memory): automatic persistence of conversation history
- LTM (long-term memory): automatic extraction via the Summary/UserPreference/Semantic strategies
"""

from .session_manager_factory import (
    SessionManagerFactoryError,
    create_agentcore_session_manager,
    is_memory_enabled,
)

# Also export the old modules for backward compatibility (deprecated)
from .memory_service import (
    MemoryConnectionError,
    MemoryService,
    MemoryServiceError,
)
from .retry import (
    RetryExhaustedError,
    with_retry,
)
from .strategy import MemoryStrategy
from .summary_strategy import MemoryOperationError, SummaryStrategy
from .semantic_strategy import SemanticStrategy

__all__ = [
    # Recommended API
    "create_agentcore_session_manager",
    "is_memory_enabled",
    "SessionManagerFactoryError",
    # Backward compatibility (deprecated)
    "MemoryStrategy",
    "SummaryStrategy",
    "SemanticStrategy",
    "MemoryOperationError",
    "MemoryService",
    "MemoryServiceError",
    "MemoryConnectionError",
    "with_retry",
    "RetryExhaustedError",
]
