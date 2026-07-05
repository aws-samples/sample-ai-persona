"""
shared/agent_integration.py 単体テスト
"""

import pytest
from unittest.mock import Mock

from src.managers.shared.agent_integration import (
    prepare_integration_tools_and_prompt,
)


@pytest.mark.unit
class TestPrepareIntegrationToolsAndPrompt:
    """prepare_integration_tools_and_prompt のテスト"""

    @pytest.fixture
    def mock_agent_service(self):
        return Mock()

    @pytest.fixture
    def mock_database_service(self):
        return Mock()

    def test_no_integrations_enabled(self, mock_agent_service, mock_database_service):
        """KB/データセット両方無効時、プロンプト変更なし・ツールなし"""
        prompt, tools = prepare_integration_tools_and_prompt(
            agent_service=mock_agent_service,
            database_service=mock_database_service,
            persona_id="p-001",
            base_prompt="base",
            enable_kb=False,
            enable_dataset=False,
        )
        assert prompt == "base"
        assert tools is None
        mock_agent_service.get_kb_tools.assert_not_called()
        mock_agent_service.get_dataset_tools.assert_not_called()

    def test_kb_enabled_with_binding(self, mock_agent_service, mock_database_service):
        """KB有効時、ツールとプロンプトが追加される"""
        mock_kb_tool = Mock()
        mock_agent_service.get_kb_tools.return_value = (
            [mock_kb_tool],
            {"name": "商品KB", "description": "商品情報", "metadata_filters": None},
        )

        prompt, tools = prepare_integration_tools_and_prompt(
            agent_service=mock_agent_service,
            database_service=mock_database_service,
            persona_id="p-001",
            base_prompt="base",
            enable_kb=True,
            enable_dataset=False,
        )
        assert "ナレッジベース" in prompt
        assert "商品KB" in prompt
        assert tools == [mock_kb_tool]

    def test_kb_enabled_no_binding(self, mock_agent_service, mock_database_service):
        """KB有効だがバインディング未設定時、プロンプト変更なし"""
        mock_agent_service.get_kb_tools.return_value = ([], None)

        prompt, tools = prepare_integration_tools_and_prompt(
            agent_service=mock_agent_service,
            database_service=mock_database_service,
            persona_id="p-001",
            base_prompt="base",
            enable_kb=True,
            enable_dataset=False,
        )
        assert prompt == "base"
        assert tools is None

    def test_dataset_enabled_with_bindings(
        self, mock_agent_service, mock_database_service
    ):
        """データセット有効時、ツールとプロンプトが追加される"""
        mock_ds_tool = Mock()
        mock_dataset = Mock()
        mock_dataset.id = "ds-001"
        mock_dataset.name = "購買データ"
        mock_dataset.description = "購買履歴"
        mock_dataset.s3_path = "s3://bucket/data.csv"
        col1 = Mock()
        col1.name = "user_id"
        col2 = Mock()
        col2.name = "amount"
        mock_dataset.columns = [col1, col2]
        mock_dataset.row_count = 100

        mock_agent_service.get_dataset_tools.return_value = (
            [mock_ds_tool],
            [{"dataset_id": "ds-001", "binding_keys": {"user_id": "U123"}}],
            [mock_dataset],
        )

        prompt, tools = prepare_integration_tools_and_prompt(
            agent_service=mock_agent_service,
            database_service=mock_database_service,
            persona_id="p-001",
            base_prompt="base",
            enable_kb=False,
            enable_dataset=True,
        )
        assert "データセット" in prompt
        assert tools == [mock_ds_tool]

    def test_dataset_enabled_no_bindings(
        self, mock_agent_service, mock_database_service
    ):
        """データセット有効だがバインディング未設定時、プロンプト変更なし"""
        mock_agent_service.get_dataset_tools.return_value = ([], [], [])

        prompt, tools = prepare_integration_tools_and_prompt(
            agent_service=mock_agent_service,
            database_service=mock_database_service,
            persona_id="p-001",
            base_prompt="base",
            enable_kb=False,
            enable_dataset=True,
        )
        assert prompt == "base"
        assert tools is None

    def test_both_enabled(self, mock_agent_service, mock_database_service):
        """KB+データセット両方有効時、両方のツールとプロンプトが追加される"""
        mock_kb_tool = Mock()
        mock_ds_tool = Mock()
        mock_dataset = Mock()
        mock_dataset.id = "ds-001"
        mock_dataset.name = "購買データ"
        mock_dataset.description = ""
        mock_dataset.s3_path = "s3://bucket/data.csv"
        col = Mock()
        col.name = "id"
        mock_dataset.columns = [col]
        mock_dataset.row_count = 50

        mock_agent_service.get_kb_tools.return_value = (
            [mock_kb_tool],
            {"name": "KB", "description": "desc", "metadata_filters": None},
        )
        mock_agent_service.get_dataset_tools.return_value = (
            [mock_ds_tool],
            [{"dataset_id": "ds-001", "binding_keys": {}}],
            [mock_dataset],
        )

        prompt, tools = prepare_integration_tools_and_prompt(
            agent_service=mock_agent_service,
            database_service=mock_database_service,
            persona_id="p-001",
            base_prompt="base",
            enable_kb=True,
            enable_dataset=True,
        )
        assert "ナレッジベース" in prompt
        assert "データセット" in prompt
        assert mock_kb_tool in tools
        assert mock_ds_tool in tools
