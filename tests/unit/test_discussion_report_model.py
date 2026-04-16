"""
Unit tests for DiscussionReport model.
"""

from datetime import datetime

import pytest

from src.models.discussion_report import DiscussionReport


class TestDiscussionReport:
    """Test cases for DiscussionReport model."""

    def test_create_new(self):
        report = DiscussionReport.create_new(
            template_type="summary",
            content="# サマリレポート\n\nテスト内容",
        )
        assert report.id is not None
        assert report.template_type == "summary"
        assert report.custom_prompt is None
        assert report.content == "# サマリレポート\n\nテスト内容"
        assert isinstance(report.created_at, datetime)

    def test_create_new_with_custom_prompt(self):
        report = DiscussionReport.create_new(
            template_type="custom",
            content="カスタム出力",
            custom_prompt="箇条書きでまとめて",
        )
        assert report.template_type == "custom"
        assert report.custom_prompt == "箇条書きでまとめて"

    def test_to_dict(self):
        report = DiscussionReport.create_new(
            template_type="review",
            content="# レビュー",
        )
        data = report.to_dict()
        assert data["id"] == report.id
        assert data["template_type"] == "review"
        assert data["custom_prompt"] is None
        assert data["content"] == "# レビュー"
        assert isinstance(data["created_at"], str)

    def test_from_dict(self):
        original = DiscussionReport.create_new(
            template_type="summary",
            content="テスト",
            custom_prompt="プロンプト",
        )
        data = original.to_dict()
        restored = DiscussionReport.from_dict(data)
        assert restored.id == original.id
        assert restored.template_type == original.template_type
        assert restored.custom_prompt == original.custom_prompt
        assert restored.content == original.content
        assert restored.created_at == original.created_at

    def test_from_dict_without_custom_prompt(self):
        data = {
            "id": "test-id",
            "template_type": "summary",
            "content": "テスト",
            "created_at": datetime.now().isoformat(),
        }
        report = DiscussionReport.from_dict(data)
        assert report.custom_prompt is None
