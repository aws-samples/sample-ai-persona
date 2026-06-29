"""
SettingsManager
システム設定（ナレッジベース管理、MCP制御、データエージェント接続テスト）を管理する。
"""

import logging
from typing import Any, Dict, List, Optional

from ..config import config
from ..models.knowledge_base import KnowledgeBase
from ..services.database_service import DatabaseService
from ..services.service_factory import service_factory

logger = logging.getLogger(__name__)


class SettingsManagerError(Exception):
    pass


class SettingsManager:
    def __init__(
        self,
        database_service: Optional[DatabaseService] = None,
    ) -> None:
        self.db = database_service or service_factory.get_database_service()

    # --- ナレッジベース管理 ---

    def get_all_knowledge_bases(self) -> List[KnowledgeBase]:
        return self.db.get_all_knowledge_bases()

    def create_knowledge_base(
        self, knowledge_base_id: str, name: str, description: str = ""
    ) -> KnowledgeBase:
        kb = KnowledgeBase.create_new(
            knowledge_base_id=knowledge_base_id.strip(),
            name=name.strip(),
            description=description.strip(),
        )
        self.db.save_knowledge_base(kb)
        logger.info(f"Knowledge base registered: {kb.id} ({knowledge_base_id})")
        return kb

    def delete_knowledge_base(self, kb_id: str) -> None:
        self.db.delete_knowledge_base(kb_id)
        logger.info(f"Knowledge base deleted: {kb_id}")

    # --- MCP管理 ---

    def is_mcp_running(self) -> bool:
        from ..services.mcp_server_manager import get_mcp_manager

        return get_mcp_manager().is_running()

    def toggle_mcp(self, enabled: bool) -> bool:
        from ..services.mcp_server_manager import get_mcp_manager

        mcp_manager = get_mcp_manager()
        if enabled:
            mcp_manager.start()
        else:
            mcp_manager.stop()
        return mcp_manager.is_running()

    def get_mcp_status(self) -> Dict[str, Any]:
        from ..services.mcp_server_manager import get_mcp_manager

        mcp_manager = get_mcp_manager()
        return {
            "enabled": mcp_manager.is_running(),
            "servers": mcp_manager.get_server_status()
            if hasattr(mcp_manager, "get_server_status")
            else [],
        }

    # --- データ分析エージェント接続テスト ---

    def test_data_agent_connection(self) -> str:
        """データ分析エージェントへの接続テストを実行する。"""
        if not config.DATA_AGENT_RUNTIME_ARN:
            raise SettingsManagerError("Runtime ARN が設定されていません")

        try:
            data_agent_service = service_factory.get_data_agent_service()
            if not data_agent_service:
                raise SettingsManagerError(
                    "データ分析エージェントサービスを初期化できません"
                )
            result = data_agent_service.query("利用可能なテーブル一覧を教えてください")
            return result.text
        except SettingsManagerError:
            raise
        except Exception as e:
            raise SettingsManagerError(
                f"データ分析エージェント接続テストに失敗しました: {e}"
            ) from e
