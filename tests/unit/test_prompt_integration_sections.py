"""Unit tests for build_kb_prompt_section / build_dataset_prompt_section."""

import pytest

from src.prompts.discussion_interview_prompts import (
    build_kb_prompt_section,
    build_dataset_prompt_section,
)


@pytest.mark.unit
class TestBuildKbPromptSection:
    """Tests for build_kb_prompt_section."""

    def test_basic_kb_section(self):
        """KB name and description are included."""
        result = build_kb_prompt_section(
            name="商品KB", description="商品情報データベース"
        )
        assert "ナレッジベース" in result
        assert "商品KB" in result
        assert "商品情報データベース" in result
        assert "search_knowledge_base" in result

    def test_with_metadata_filters(self):
        """Metadata filters are reflected."""
        result = build_kb_prompt_section(
            name="KB",
            description="desc",
            metadata_filters={"category": "electronics"},
        )
        assert "category=electronics" in result
        assert "フィルタ" in result

    def test_without_metadata_filters(self):
        """No filter wording appears without a filter."""
        result = build_kb_prompt_section(
            name="KB", description="desc", metadata_filters=None
        )
        assert "フィルタ" not in result

    def test_empty_description(self):
        """An empty description does not cause an error."""
        result = build_kb_prompt_section(
            name="KB", description="", metadata_filters=None
        )
        assert "KB" in result
        assert "内容:" not in result


@pytest.mark.unit
class TestBuildDatasetPromptSection:
    """Tests for build_dataset_prompt_section (takes display descriptors)."""

    @pytest.fixture
    def descriptor(self):
        return {
            "alias": "dataset_1",
            "name": "購買データ",
            "description": "購買履歴データ",
            "row_count": 500,
            "columns": [
                {"name": "user_id", "data_type": "string", "description": "顧客ID"},
                {"name": "amount", "data_type": "integer", "description": "税込金額"},
            ],
        }

    def test_basic_dataset_section(self, descriptor):
        """Display metadata (name, description, alias, columns, row count) is included in the prompt."""
        result = build_dataset_prompt_section([descriptor])

        assert "購買データ" in result
        assert "購買履歴データ" in result
        assert "dataset_1" in result
        assert "amount" in result
        assert "analyze_dataset" in result

    def test_column_type_and_description_shown(self, descriptor):
        """Column types and descriptions are included in the prompt."""
        result = build_dataset_prompt_section([descriptor])
        assert "(integer)" in result
        assert "税込金額" in result
        assert "(string)" in result
        assert "顧客ID" in result

    def test_string_columns_still_supported(self):
        """Columns given as a list of name strings still render (backward compatible)."""
        descriptor = {
            "alias": "dataset_1",
            "name": "d",
            "description": "",
            "row_count": 1,
            "columns": ["a", "b"],
        }
        result = build_dataset_prompt_section([descriptor])
        assert "a" in result and "b" in result

    def test_does_not_leak_backend_path(self, descriptor):
        """backend_path (s3_path) is not exposed in the prompt."""
        result = build_dataset_prompt_section([descriptor])
        assert "s3://" not in result
        assert "read_csv" not in result
        assert "CREATE SECRET" not in result

    def test_does_not_leak_forced_filter_value(self, descriptor):
        """A binding filter value (e.g. U123) is not exposed in the prompt.

        The descriptor carries no forced_filter value, so it cannot reach the output.
        """
        result = build_dataset_prompt_section([descriptor])
        assert "U123" not in result
        # no SQL fragments either
        assert "SELECT" not in result
        assert "WHERE" not in result

    def test_empty_datasets_returns_empty(self):
        """An empty descriptor list returns an empty string."""
        assert build_dataset_prompt_section([]) == ""

    def test_multiple_datasets(self, descriptor):
        """Multiple datasets are included by alias."""
        ds2 = {
            "alias": "dataset_2",
            "name": "行動ログ",
            "description": "行動データ",
            "row_count": 1000,
            "columns": ["action"],
        }
        result = build_dataset_prompt_section([descriptor, ds2])

        assert "購買データ" in result
        assert "行動ログ" in result
        assert "dataset_1" in result
        assert "dataset_2" in result
