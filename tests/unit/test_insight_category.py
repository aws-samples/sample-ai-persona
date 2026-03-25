"""
Unit tests for InsightCategory model.
"""

import pytest
import json
from src.models.insight_category import InsightCategory


class TestInsightCategory:
    """Test cases for InsightCategory model."""

    def test_create_new_valid(self):
        """Test creating a valid InsightCategory."""
        category = InsightCategory.create_new(
            name="顧客ニーズ", description="議論から読み取れる潜在的・顕在的ニーズ"
        )
        assert category.name == "顧客ニーズ"
        assert category.description == "議論から読み取れる潜在的・顕在的ニーズ"

    def test_create_new_strips_whitespace(self):
        """Test that create_new strips whitespace."""
        category = InsightCategory.create_new(
            name="  顧客ニーズ  ", description="  説明  "
        )
        assert category.name == "顧客ニーズ"
        assert category.description == "説明"

    def test_empty_name_raises_error(self):
        """Test that empty name raises ValueError."""
        with pytest.raises(ValueError, match="Category name cannot be empty"):
            InsightCategory(name="", description="説明")

    def test_whitespace_only_name_raises_error(self):
        """Test that whitespace-only name raises ValueError."""
        with pytest.raises(ValueError, match="Category name cannot be empty"):
            InsightCategory(name="   ", description="説明")

    def test_name_too_long_raises_error(self):
        """Test that name longer than 50 characters raises ValueError."""
        long_name = "a" * 51
        with pytest.raises(
            ValueError, match="Category name must be 50 characters or less"
        ):
            InsightCategory(name=long_name, description="説明")

    def test_description_too_long_raises_error(self):
        """Test that description longer than 500 characters raises ValueError."""
        long_description = "a" * 501
        with pytest.raises(
            ValueError, match="Category description must be 500 characters or less"
        ):
            InsightCategory(name="カテゴリー", description=long_description)

    def test_to_dict(self):
        """Test converting InsightCategory to dictionary."""
        category = InsightCategory(name="顧客ニーズ", description="説明")
        result = category.to_dict()

        assert result == {"name": "顧客ニーズ", "description": "説明"}

    def test_from_dict(self):
        """Test creating InsightCategory from dictionary."""
        data = {"name": "市場機会", "description": "市場の機会を特定"}
        category = InsightCategory.from_dict(data)

        assert category.name == "市場機会"
        assert category.description == "市場の機会を特定"

    def test_to_json(self):
        """Test converting InsightCategory to JSON string."""
        category = InsightCategory(name="顧客ニーズ", description="説明")
        json_str = category.to_json()

        data = json.loads(json_str)
        assert data["name"] == "顧客ニーズ"
        assert data["description"] == "説明"

    def test_from_json(self):
        """Test creating InsightCategory from JSON string."""
        json_str = '{"name": "商品開発", "description": "商品開発への示唆"}'
        category = InsightCategory.from_json(json_str)

        assert category.name == "商品開発"
        assert category.description == "商品開発への示唆"

    def test_round_trip_serialization(self):
        """Test that serialization and deserialization preserves data."""
        original = InsightCategory(
            name="マーケティング", description="マーケティング戦略"
        )

        # Dict round trip
        dict_data = original.to_dict()
        from_dict = InsightCategory.from_dict(dict_data)
        assert from_dict.name == original.name
        assert from_dict.description == original.description

        # JSON round trip
        json_str = original.to_json()
        from_json = InsightCategory.from_json(json_str)
        assert from_json.name == original.name
        assert from_json.description == original.description

    def test_get_default_categories(self):
        """Test getting default insight categories."""
        categories = InsightCategory.get_default_categories()

        assert len(categories) == 5
        assert all(isinstance(cat, InsightCategory) for cat in categories)

        # Check expected category names
        category_names = [cat.name for cat in categories]
        assert "顧客ニーズ" in category_names
        assert "市場機会" in category_names
        assert "商品開発" in category_names
        assert "マーケティング" in category_names
        assert "その他" in category_names

        # Check all have descriptions
        assert all(len(cat.description) > 0 for cat in categories)

    def test_default_categories_are_valid(self):
        """Test that all default categories pass validation."""
        categories = InsightCategory.get_default_categories()

        for category in categories:
            assert len(category.name) <= 50
            assert len(category.description) <= 500
            assert len(category.name.strip()) > 0
