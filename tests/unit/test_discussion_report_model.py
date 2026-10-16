"""
Unit tests for DiscussionReport model (U6: one item of DiscussionReports).
"""

from datetime import datetime

from src.models.discussion_report import DiscussionReport


class TestDiscussionReport:
    """Test cases for DiscussionReport model."""

    def test_create_new(self):
        report = DiscussionReport.create_new(
            discussion_id="d1",
            template_type="summary",
            content="# サマリレポート\n\nテスト内容",
        )
        assert report.id is not None
        assert report.discussion_id == "d1"
        assert report.template_type == "summary"
        assert report.custom_prompt is None
        assert report.content == "# サマリレポート\n\nテスト内容"
        assert isinstance(report.created_at, datetime)

    def test_create_new_with_custom_prompt(self):
        report = DiscussionReport.create_new(
            discussion_id="d1",
            template_type="custom",
            content="カスタム出力",
            custom_prompt="箇条書きでまとめて",
        )
        assert report.template_type == "custom"
        assert report.custom_prompt == "箇条書きでまとめて"

    def test_to_dict(self):
        report = DiscussionReport.create_new(
            discussion_id="d1",
            template_type="review",
            content="# レビュー",
        )
        data = report.to_dict()
        assert data["report_id"] == report.id
        assert data["discussion_id"] == "d1"
        assert data["template_type"] == "review"
        assert "custom_prompt" not in data
        assert data["content"] == "# レビュー"
        assert isinstance(data["created_at"], str)

    def test_from_dict(self):
        original = DiscussionReport.create_new(
            discussion_id="d1",
            template_type="summary",
            content="テスト",
            custom_prompt="プロンプト",
        )
        data = original.to_dict()
        restored = DiscussionReport.from_dict(data)
        assert restored == original

    def test_from_dict_without_custom_prompt(self):
        data = {
            "id": "test-id",
            "template_type": "summary",
            "content": "テスト",
            "created_at": datetime.now().isoformat(),
        }
        report = DiscussionReport.from_dict(data)
        assert report.custom_prompt is None
        assert report.report_id == "test-id"
