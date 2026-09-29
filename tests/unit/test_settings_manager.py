"""SettingsManager のユニットテスト。"""

import pytest
from unittest.mock import Mock, patch

from src.managers.settings_manager import SettingsManager, SettingsManagerError
from src.models.errors import ErrorCode
from src.models.knowledge_base import KnowledgeBase
from tests.error_helpers import raises_code


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
class TestSettingsManagerDataAgent:
    """データ分析エージェント接続テストのテスト。"""

    def setup_method(self):
        self.mock_db = Mock()
        self.manager = SettingsManager(database_service=self.mock_db)

    @patch("src.managers.settings_manager.config")
    def test_no_runtime_arn_raises_error(self, mock_config):
        """Runtime ARN未設定時にエラーを返すこと"""
        mock_config.DATA_AGENT_RUNTIME_ARN = None

        with raises_code(SettingsManagerError, ErrorCode.DATA_AGENT_NOT_CONFIGURED):
            self.manager.test_data_agent_connection()

    @patch("src.managers.settings_manager.service_factory")
    @patch("src.managers.settings_manager.config")
    def test_service_returns_none_raises_error(self, mock_config, mock_sf):
        """サービスがNoneを返した場合エラーになること"""
        mock_config.DATA_AGENT_RUNTIME_ARN = (
            "arn:aws:bedrock:us-east-1:123:agent-runtime/xxx"
        )
        mock_sf.get_data_agent_service.return_value = None

        with raises_code(SettingsManagerError, ErrorCode.DATA_AGENT_CONNECTION_FAILED):
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

        with raises_code(
            SettingsManagerError, ErrorCode.DATA_AGENT_CONNECTION_FAILED
        ) as exc_info:
            self.manager.test_data_agent_connection()

        assert isinstance(exc_info.value.__cause__, RuntimeError)
