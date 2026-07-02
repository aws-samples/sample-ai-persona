"""
build_kb_prompt_section / build_dataset_prompt_section 単体テスト
"""

import pytest
from unittest.mock import Mock

from src.prompts.discussion_interview_prompts import (
    build_kb_prompt_section,
    build_dataset_prompt_section,
)


@pytest.mark.unit
class TestBuildKbPromptSection:
    """build_kb_prompt_section のテスト"""

    def test_basic_kb_section(self):
        """KB名と説明が含まれる"""
        result = build_kb_prompt_section(
            name="商品KB", description="商品情報データベース"
        )
        assert "ナレッジベース" in result
        assert "商品KB" in result
        assert "商品情報データベース" in result
        assert "search_knowledge_base" in result

    def test_with_metadata_filters(self):
        """メタデータフィルタが反映される"""
        result = build_kb_prompt_section(
            name="KB",
            description="desc",
            metadata_filters={"category": "electronics"},
        )
        assert "category=electronics" in result
        assert "フィルタ" in result

    def test_without_metadata_filters(self):
        """フィルタなしの場合フィルタ文言が出ない"""
        result = build_kb_prompt_section(
            name="KB", description="desc", metadata_filters=None
        )
        assert "フィルタ" not in result

    def test_empty_description(self):
        """説明が空でもエラーにならない"""
        result = build_kb_prompt_section(
            name="KB", description="", metadata_filters=None
        )
        assert "KB" in result
        assert "内容:" not in result


@pytest.mark.unit
class TestBuildDatasetPromptSection:
    """build_dataset_prompt_section のテスト"""

    @pytest.fixture
    def mock_dataset(self):
        ds = Mock()
        ds.id = "ds-001"
        ds.name = "購買データ"
        ds.description = "購買履歴データ"
        ds.s3_path = "s3://bucket/purchases.csv"
        col1 = Mock()
        col1.name = "user_id"
        col2 = Mock()
        col2.name = "amount"
        ds.columns = [col1, col2]
        ds.row_count = 500
        return ds

    def test_basic_dataset_section(self, mock_dataset):
        """データセット情報がプロンプトに含まれる"""
        bindings = [{"dataset_id": "ds-001", "binding_keys": {"user_id": "U123"}}]
        result = build_dataset_prompt_section(bindings, [mock_dataset])

        assert "購買データ" in result
        assert "s3://bucket/purchases.csv" in result
        assert "user_id" in result
        assert "U123" in result
        assert "データセットを参照" in result

    def test_empty_binding_keys(self, mock_dataset):
        """binding_keys空の場合、全行表記"""
        bindings = [{"dataset_id": "ds-001", "binding_keys": {}}]
        result = build_dataset_prompt_section(bindings, [mock_dataset])

        assert "全行がこのペルソナのデータ" in result

    def test_empty_bindings_returns_empty(self):
        """bindingsが空の場合、空文字を返す"""
        result = build_dataset_prompt_section([], [])
        assert result == ""

    def test_none_bindings_returns_empty(self):
        """bindingsがNone相当の空リストで空文字"""
        result = build_dataset_prompt_section([], [Mock()])
        assert result == ""

    def test_dataset_not_found_in_map(self):
        """bindingsのdataset_idに対応するデータセットがない場合"""
        bindings = [{"dataset_id": "nonexistent", "binding_keys": {}}]
        mock_ds = Mock()
        mock_ds.id = "other-id"
        result = build_dataset_prompt_section(bindings, [mock_ds])
        assert result == ""

    def test_multiple_datasets(self, mock_dataset):
        """複数データセットが含まれる"""
        ds2 = Mock()
        ds2.id = "ds-002"
        ds2.name = "行動ログ"
        ds2.description = "行動データ"
        ds2.s3_path = "s3://bucket/actions.parquet"
        col = Mock()
        col.name = "action"
        ds2.columns = [col]
        ds2.row_count = 1000

        bindings = [
            {"dataset_id": "ds-001", "binding_keys": {"user_id": "U1"}},
            {"dataset_id": "ds-002", "binding_keys": {"action": "click"}},
        ]
        result = build_dataset_prompt_section(bindings, [mock_dataset, ds2])

        assert "購買データ" in result
        assert "行動ログ" in result
