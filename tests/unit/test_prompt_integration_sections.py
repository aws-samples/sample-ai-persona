"""
build_kb_prompt_section / build_dataset_prompt_section 単体テスト
"""

import pytest

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
    """build_dataset_prompt_section のテスト（表示用記述子を受け取る新契約）"""

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
        """表示メタデータ（名前・説明・別名・列・行数）がプロンプトに含まれる"""
        result = build_dataset_prompt_section([descriptor])

        assert "購買データ" in result
        assert "購買履歴データ" in result
        assert "dataset_1" in result
        assert "amount" in result
        assert "analyze_dataset" in result

    def test_column_type_and_description_shown(self, descriptor):
        """列の型と説明文がプロンプトに含まれる"""
        result = build_dataset_prompt_section([descriptor])
        assert "(integer)" in result
        assert "税込金額" in result
        assert "(string)" in result
        assert "顧客ID" in result

    def test_string_columns_still_supported(self):
        """列が名前文字列のリストでも描画できる（後方互換）"""
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
        """backend_path（s3_path）はプロンプトへ露出しない"""
        result = build_dataset_prompt_section([descriptor])
        assert "s3://" not in result
        assert "read_csv" not in result
        assert "CREATE SECRET" not in result

    def test_does_not_leak_forced_filter_value(self, descriptor):
        """binding フィルタ値（例: U123）はプロンプトへ露出しない。

        記述子には forced_filter 値が含まれないため、生成物にも出ない。
        """
        result = build_dataset_prompt_section([descriptor])
        assert "U123" not in result
        # SQL 断片も無いこと
        assert "SELECT" not in result
        assert "WHERE" not in result

    def test_empty_datasets_returns_empty(self):
        """記述子が空の場合、空文字を返す"""
        assert build_dataset_prompt_section([]) == ""

    def test_multiple_datasets(self, descriptor):
        """複数データセットが別名で含まれる"""
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
