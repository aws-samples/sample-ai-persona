"""SettingsManager のユニットテスト。"""

import pytest
from unittest.mock import Mock, patch

from src.managers.settings_manager import SettingsManager, SettingsManagerError
from src.models.knowledge_base import KnowledgeBase


@pytest.mark.unit
class TestSettingsManagerKnowledgeBase:
    """ナレッジベース管理のテスト。"""

    def setup_method(self):
        self.mock_db = Mock()
        self.manager = SettingsManager(database_service=self.mock_db)

    def test_get_all_knowledge_bases(self):
        """全ナレッジベース取得が委譲されること"""
        expected = [Mock(spec=KnowledgeBase), Mock(spec=KnowledgeBase)]
        self.mock_db.get_all_knowledge_bases.return_value = expected

        result = self.manager.get_all_knowledge_bases()

        assert result == expected
        self.mock_db.get_all_knowledge_bases.assert_called_once()

    def test_create_knowledge_base(self):
        """ナレッジベース作成が正しく動作すること"""
        kb = self.manager.create_knowledge_base(
            knowledge_base_id="KB12345678",
            name="テストKB",
            description="説明文",
        )

        assert kb.knowledge_base_id == "KB12345678"
        assert kb.name == "テストKB"
        assert kb.description == "説明文"
        self.mock_db.save_knowledge_base.assert_called_once_with(kb)

    def test_create_knowledge_base_strips_whitespace(self):
        """作成時に前後空白がstripされること"""
        kb = self.manager.create_knowledge_base(
            knowledge_base_id="  KB99999  ",
            name="  名前  ",
            description="  説明  ",
        )

        assert kb.knowledge_base_id == "KB99999"
        assert kb.name == "名前"
        assert kb.description == "説明"

    def test_delete_knowledge_base(self):
        """ナレッジベース削除がDBに委譲されること"""
        self.manager.delete_knowledge_base("kb-001")

        self.mock_db.delete_knowledge_base.assert_called_once_with("kb-001")


@pytest.mark.unit
class TestSettingsManagerMCP:
    """MCP管理のテスト。"""

    def setup_method(self):
        self.mock_db = Mock()
        self.manager = SettingsManager(database_service=self.mock_db)

    @patch("src.services.mcp_server_manager.get_mcp_manager")
    def test_is_mcp_running_true(self, mock_get_mcp_manager):
        """MCP実行中の場合Trueを返すこと"""
        mock_mcp = Mock()
        mock_mcp.is_running.return_value = True
        mock_get_mcp_manager.return_value = mock_mcp

        assert self.manager.is_mcp_running() is True

    @patch("src.services.mcp_server_manager.get_mcp_manager")
    def test_is_mcp_running_false(self, mock_get_mcp_manager):
        """MCP停止中の場合Falseを返すこと"""
        mock_mcp = Mock()
        mock_mcp.is_running.return_value = False
        mock_get_mcp_manager.return_value = mock_mcp

        assert self.manager.is_mcp_running() is False

    @patch("src.services.mcp_server_manager.get_mcp_manager")
    def test_toggle_mcp_enable(self, mock_get_mcp_manager):
        """MCP有効化でstartが呼ばれること"""
        mock_mcp = Mock()
        mock_mcp.is_running.return_value = True
        mock_get_mcp_manager.return_value = mock_mcp

        result = self.manager.toggle_mcp(enabled=True)

        mock_mcp.start.assert_called_once()
        mock_mcp.stop.assert_not_called()
        assert result is True

    @patch("src.services.mcp_server_manager.get_mcp_manager")
    def test_toggle_mcp_disable(self, mock_get_mcp_manager):
        """MCP無効化でstopが呼ばれること"""
        mock_mcp = Mock()
        mock_mcp.is_running.return_value = False
        mock_get_mcp_manager.return_value = mock_mcp

        result = self.manager.toggle_mcp(enabled=False)

        mock_mcp.stop.assert_called_once()
        mock_mcp.start.assert_not_called()
        assert result is False

    @patch("src.services.mcp_server_manager.get_mcp_manager")
    def test_get_mcp_status(self, mock_get_mcp_manager):
        """MCPステータス取得が正しい構造を返すこと"""
        mock_mcp = Mock()
        mock_mcp.is_running.return_value = True
        mock_mcp.get_server_status.return_value = [{"name": "server1", "status": "ok"}]
        mock_get_mcp_manager.return_value = mock_mcp

        result = self.manager.get_mcp_status()

        assert result["enabled"] is True
        assert result["servers"] == [{"name": "server1", "status": "ok"}]

    @patch("src.services.mcp_server_manager.get_mcp_manager")
    def test_get_mcp_status_no_get_server_status(self, mock_get_mcp_manager):
        """get_server_statusメソッドがない場合空リストを返すこと"""
        mock_mcp = Mock(spec=[])
        mock_mcp.is_running = Mock(return_value=False)
        mock_get_mcp_manager.return_value = mock_mcp

        result = self.manager.get_mcp_status()

        assert result["enabled"] is False
        assert result["servers"] == []


@pytest.mark.unit
class TestSettingsManagerDataAgent:
    """データ分析エージェント接続テストのテスト。"""

    def setup_method(self):
        self.mock_db = Mock()
        self.manager = SettingsManager(database_service=self.mock_db)

    @patch("src.managers.settings_manager.config")
    def test_no_runtime_arn_raises_error(self, mock_config):
        """Runtime ARN未設定時にエラーを返すこと"""
        mock_config.DATA_AGENT_RUNTIME_ARN = None

        with pytest.raises(
            SettingsManagerError, match="Runtime ARN が設定されていません"
        ):
            self.manager.test_data_agent_connection()

    @patch("src.managers.settings_manager.service_factory")
    @patch("src.managers.settings_manager.config")
    def test_service_returns_none_raises_error(self, mock_config, mock_sf):
        """サービスがNoneを返した場合エラーになること"""
        mock_config.DATA_AGENT_RUNTIME_ARN = (
            "arn:aws:bedrock:us-east-1:123:agent-runtime/xxx"
        )
        mock_sf.get_data_agent_service.return_value = None

        with pytest.raises(
            SettingsManagerError,
            match="データ分析エージェントサービスを初期化できません",
        ):
            self.manager.test_data_agent_connection()

    @patch("src.managers.settings_manager.service_factory")
    @patch("src.managers.settings_manager.config")
    def test_success(self, mock_config, mock_sf):
        """正常系でクエリ結果テキストを返すこと"""
        mock_config.DATA_AGENT_RUNTIME_ARN = (
            "arn:aws:bedrock:us-east-1:123:agent-runtime/xxx"
        )
        mock_data_agent = Mock()
        mock_result = Mock()
        mock_result.text = "テーブル一覧: users, orders"
        mock_data_agent.query.return_value = mock_result
        mock_sf.get_data_agent_service.return_value = mock_data_agent

        result = self.manager.test_data_agent_connection()

        assert result == "テーブル一覧: users, orders"
        mock_data_agent.query.assert_called_once_with(
            "利用可能なテーブル一覧を教えてください"
        )

    @patch("src.managers.settings_manager.service_factory")
    @patch("src.managers.settings_manager.config")
    def test_exception_wrapping(self, mock_config, mock_sf):
        """内部例外がSettingsManagerErrorにラップされること"""
        mock_config.DATA_AGENT_RUNTIME_ARN = (
            "arn:aws:bedrock:us-east-1:123:agent-runtime/xxx"
        )
        mock_data_agent = Mock()
        mock_data_agent.query.side_effect = RuntimeError("接続タイムアウト")
        mock_sf.get_data_agent_service.return_value = mock_data_agent

        with pytest.raises(
            SettingsManagerError, match="データ分析エージェント接続テストに失敗しました"
        ) as exc_info:
            self.manager.test_data_agent_connection()

        assert isinstance(exc_info.value.__cause__, RuntimeError)
