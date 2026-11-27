"""
Session Manager Factory for AgentCore Memory Integration
Factory that creates AgentCoreMemorySessionManager

Following the Strands Agent SDK's recommended approach, uses AgentCoreMemorySessionManager
to manage both STM (short-term memory) and LTM (long-term memory) automatically.

The memory_mode parameter supports these modes:
- "full": retrieve + save (default)
- "retrieve_only": retrieve only (no saving)
- "disabled": memory disabled
"""

import logging
from typing import Optional, Dict, Any, Literal

from src.config import config

logger = logging.getLogger(__name__)


# Type definition for memory modes
MemoryMode = Literal["full", "retrieve_only", "disabled"]


class SessionManagerFactoryError(Exception):
    """Errors raised by the Session Manager Factory."""

    pass


def create_agentcore_session_manager(
    actor_id: str,
    session_id: str,
    retrieval_config: Optional[Dict[str, Any]] = None,
    memory_mode: MemoryMode = "full",
) -> Any:
    """
    Create an AgentCoreMemorySessionManager

    Args:
        actor_id: Persona ID (used as the actorId in AgentCore Memory)
        session_id: Discussion/interview session ID
        retrieval_config: LTM retrieval settings (optional)
            Example: {
                "/preferences/{actorId}": {"top_k": 5, "relevance_score": 0.7},
                "/facts/{actorId}": {"top_k": 10, "relevance_score": 0.3}
            }
        memory_mode: Memory mode
            - "full": retrieve + save (default)
            - "retrieve_only": retrieve only (no saving)
            - "disabled": memory disabled

    Returns:
        AgentCoreMemorySessionManager or RetrieveOnlySessionManager:
        The configured session manager

    Raises:
        SessionManagerFactoryError: If creation fails
    """
    # Return None when the memory mode is disabled
    if memory_mode == "disabled":
        logger.debug("Memory mode is 'disabled', returning None")
        return None

    # Return None when long-term memory is disabled
    if not config.ENABLE_LONG_TERM_MEMORY:
        logger.debug("Long-term memory is disabled by configuration")
        return None

    # Return None when AGENTCORE_MEMORY_ID is not set
    if not config.AGENTCORE_MEMORY_ID:
        logger.warning(
            "ENABLE_LONG_TERM_MEMORY is True but AGENTCORE_MEMORY_ID is not set"
        )
        return None

    try:
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
            RetrievalConfig,
        )
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )

        # Build the RetrievalConfig
        # If retrieval_config is passed explicitly (even an empty dict) use it as is,
        # and inject the default strategy only when it is None
        retrieval_config_obj = None
        if retrieval_config is not None:
            retrieval_config_obj = {}
            for namespace, settings in retrieval_config.items():
                retrieval_config_obj[namespace] = RetrievalConfig(
                    top_k=settings.get("top_k", 10),
                    relevance_score=settings.get("relevance_score", 0.2),
                )

        # Default LTM retrieval settings (built from the enabled strategies)
        if retrieval_config_obj is None:
            retrieval_config_obj = {}

            # Summary strategy namespace (always enabled)
            # Including sessions/{sessionId} would pin the search scope to the current session
            # and no memory from past sessions would ever hit, so prefix-search under the
            # actor (persona) to retrieve across all sessions
            if config.SUMMARY_MEMORY_STRATEGY_ID:
                retrieval_config_obj[
                    f"/strategies/{config.SUMMARY_MEMORY_STRATEGY_ID}/actors/{{actorId}}"
                ] = RetrievalConfig(
                    top_k=config.MEMORY_MAX_RESULTS, relevance_score=0.3
                )

            # Semantic strategy namespace (only when configured)
            if config.SEMANTIC_MEMORY_STRATEGY_ID:
                # The Semantic strategy uses the /facts/{actorId} pattern
                retrieval_config_obj[
                    f"/strategies/{config.SEMANTIC_MEMORY_STRATEGY_ID}/actors/{{actorId}}"
                ] = RetrievalConfig(
                    top_k=config.MEMORY_MAX_RESULTS,
                    relevance_score=0.5,  # Set a higher relevance score for Semantic
                )

        # Create the AgentCoreMemoryConfig
        memory_config = AgentCoreMemoryConfig(
            memory_id=config.AGENTCORE_MEMORY_ID,
            session_id=session_id,
            actor_id=actor_id,
            retrieval_config=retrieval_config_obj,
        )

        # Create the AgentCoreMemorySessionManager
        base_session_manager = AgentCoreMemorySessionManager(
            agentcore_memory_config=memory_config,
            region_name=config.AGENTCORE_MEMORY_REGION,
        )

        # Return a session manager according to memory_mode
        if memory_mode == "retrieve_only":
            from .retrieve_only_session_manager import RetrieveOnlySessionManager

            session_manager = RetrieveOnlySessionManager(
                base_manager=base_session_manager,
                actor_id=actor_id,
                session_id=session_id,
            )

            logger.info(
                f"Created RetrieveOnlySessionManager: "
                f"memory_id={config.AGENTCORE_MEMORY_ID}, "
                f"actor_id={actor_id}, session_id={session_id}"
            )
        else:
            # memory_mode == "full"
            session_manager = base_session_manager  # type: ignore[assignment]

            logger.info(
                f"Created AgentCoreMemorySessionManager (full mode): "
                f"memory_id={config.AGENTCORE_MEMORY_ID}, "
                f"actor_id={actor_id}, session_id={session_id}"
            )

        return session_manager

    except ImportError as e:
        logger.error(
            f"Failed to import bedrock_agentcore: {e}. "
            "Please install it with: pip install 'bedrock-agentcore[strands-agents]'"
        )
        raise SessionManagerFactoryError(
            "bedrock-agentcore package is not installed"
        ) from e
    except Exception as e:
        logger.error(f"Failed to create AgentCoreMemorySessionManager: {e}")
        raise SessionManagerFactoryError(
            f"Failed to create session manager: {e}"
        ) from e


def is_memory_enabled() -> bool:
    """
    Check whether the long-term memory feature is enabled

    Returns:
        bool: True if long-term memory is enabled
    """
    return config.ENABLE_LONG_TERM_MEMORY and bool(config.AGENTCORE_MEMORY_ID)
