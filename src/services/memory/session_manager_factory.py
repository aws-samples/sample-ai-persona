"""
Session Manager Factory for AgentCore Memory Integration
AgentCoreMemorySessionManagerを作成するファクトリ

Strands Agent SDKの推奨方式に従い、AgentCoreMemorySessionManagerを使用して
STM（短期記憶）とLTM（長期記憶）の両方を自動管理する。

memory_modeパラメータにより、以下のモードをサポート:
- "full": 検索 + 保存（デフォルト）
- "retrieve_only": 検索のみ（保存しない）
- "disabled": メモリ機能無効
"""

import logging
from typing import Optional, Dict, Any, Literal

from src.config import config

logger = logging.getLogger(__name__)


# メモリモードの型定義
MemoryMode = Literal["full", "retrieve_only", "disabled"]


class SessionManagerFactoryError(Exception):
    """Session Manager Factory関連のエラー"""

    pass


def create_agentcore_session_manager(
    actor_id: str,
    session_id: str,
    retrieval_config: Optional[Dict[str, Any]] = None,
    memory_mode: MemoryMode = "full",
):
    """
    AgentCoreMemorySessionManagerを作成

    Args:
        actor_id: ペルソナID（AgentCore MemoryのactorIdとして使用）
        session_id: 議論/インタビューセッションID
        retrieval_config: LTM検索設定（オプション）
            例: {
                "/preferences/{actorId}": {"top_k": 5, "relevance_score": 0.7},
                "/facts/{actorId}": {"top_k": 10, "relevance_score": 0.3}
            }
        memory_mode: メモリモード
            - "full": 検索 + 保存（デフォルト）
            - "retrieve_only": 検索のみ（保存しない）
            - "disabled": メモリ機能無効

    Returns:
        AgentCoreMemorySessionManager または RetrieveOnlySessionManager:
        設定済みのセッションマネージャー

    Raises:
        SessionManagerFactoryError: 作成に失敗した場合
    """
    # メモリモードがdisabledの場合はNoneを返す
    if memory_mode == "disabled":
        logger.debug("Memory mode is 'disabled', returning None")
        return None

    # 長期記憶が無効の場合はNoneを返す
    if not config.ENABLE_LONG_TERM_MEMORY:
        logger.debug("Long-term memory is disabled by configuration")
        return None

    # AGENTCORE_MEMORY_IDが設定されていない場合はNoneを返す
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

        # RetrievalConfigを構築
        retrieval_config_obj = None
        if retrieval_config:
            retrieval_config_obj = {}
            for namespace, settings in retrieval_config.items():
                retrieval_config_obj[namespace] = RetrievalConfig(
                    top_k=settings.get("top_k", 10),
                    relevance_score=settings.get("relevance_score", 0.2),
                )

        # デフォルトのLTM検索設定（有効な戦略に基づいて構築）
        if retrieval_config_obj is None:
            retrieval_config_obj = {}

            # Summary戦略のnamespace（常に有効）
            if config.SUMMARY_MEMORY_STRATEGY_ID:
                retrieval_config_obj[
                    f"/strategies/{config.SUMMARY_MEMORY_STRATEGY_ID}/actors/{{actorId}}/sessions/{{sessionId}}"
                ] = RetrievalConfig(
                    top_k=config.MEMORY_MAX_RESULTS, relevance_score=0.3
                )

            # Semantic戦略のnamespace（設定されている場合のみ）
            if config.SEMANTIC_MEMORY_STRATEGY_ID:
                # Semantic戦略は /facts/{actorId} パターンを使用
                retrieval_config_obj[
                    f"/strategies/{config.SEMANTIC_MEMORY_STRATEGY_ID}/actors/{{actorId}}"
                ] = RetrievalConfig(
                    top_k=config.MEMORY_MAX_RESULTS,
                    relevance_score=0.5,  # Semanticは関連性スコアを高めに設定
                )

        # AgentCoreMemoryConfigを作成
        memory_config = AgentCoreMemoryConfig(
            memory_id=config.AGENTCORE_MEMORY_ID,
            session_id=session_id,
            actor_id=actor_id,
            retrieval_config=retrieval_config_obj,
        )

        # AgentCoreMemorySessionManagerを作成
        base_session_manager = AgentCoreMemorySessionManager(
            agentcore_memory_config=memory_config,
            region_name=config.AGENTCORE_MEMORY_REGION,
        )

        # memory_modeに応じてセッションマネージャーを返す
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
            session_manager = base_session_manager

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
    長期記憶機能が有効かどうかを確認

    Returns:
        bool: 長期記憶が有効な場合True
    """
    return config.ENABLE_LONG_TERM_MEMORY and bool(config.AGENTCORE_MEMORY_ID)
